"""Figuring out which Claude we are attached to, and what it is working on.

Claude Code exports CLAUDE_CODE_SESSION_ID and CLAUDE_PID into the environment
of processes it spawns. A stdio MCP server is spawned once per session, so it
inherits exactly one session's identity -- which is the whole reason this
project uses stdio rather than an HTTP MCP server (an HTTP server would be
shared by every session and could not tell them apart).

The Claude Desktop app also speaks stdio, but gives none of that: no session id,
no pid, and a working directory that is wherever the app happened to launch --
your home folder, not your work. So identity here is conditional on the surface,
and the Desktop half of it is mostly about refusing to guess. See surface().
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import backend, config

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


# What kind of Claude is running this server. The installer writes this into the
# env block of whichever config it wires, so it is told to us rather than
# guessed: a missing CLAUDE_CODE_SESSION_ID would also describe a server someone
# started by hand, and mislabelling that as Desktop would be worse than saying
# nothing.
CLAUDE_CODE = "claude-code"
CLAUDE_DESKTOP = "claude-desktop"


def surface() -> str:
    """Which Claude this server was spawned by."""
    declared = (os.environ.get("CLAUDE_BLOCKERS_SURFACE") or "").strip()
    if declared:
        return declared
    return CLAUDE_CODE if session_id() else "unknown"


def has_working_directory() -> bool:
    """Does this surface's cwd mean anything?

    Claude Code spawns its MCP server in the folder you are working in, so
    os.getcwd() is the project. Claude Desktop spawns it in your home folder
    regardless of what the conversation is about, so the same call returns
    something that merely looks like an answer -- and a blocker filed under your
    home folder's name, with whatever git repo happens to be above it, is worse
    than one that admits it has no directory.
    """
    return surface() == CLAUDE_CODE


def session_id() -> str | None:
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def claude_pid() -> int | None:
    raw = os.environ.get("CLAUDE_PID")
    if raw and raw.isdigit():
        return int(raw)
    return None


def project_label(cwd: str | os.PathLike[str]) -> str:
    """Human label for a working directory -- just the folder name."""
    name = Path(cwd).name
    return name or str(cwd)


def git_info(cwd: str | os.PathLike[str]) -> dict[str, str | None]:
    """Repository name and branch for a directory, by reading .git directly.

    Deliberately not shelling out to `git`: this runs inside raise_blocker, on
    the critical path of a session that is already stuck, and spawning a
    process per call is both slower and one more thing that can hang. Reading
    the files covers the normal cases and simply returns None otherwise.
    """
    result: dict[str, str | None] = {"repo": None, "branch": None}
    try:
        current = Path(cwd).resolve()
    except OSError:
        return result

    for candidate in (current, *current.parents):
        dot_git = candidate / ".git"
        if not dot_git.exists():
            continue

        # A worktree or submodule has .git as a file pointing elsewhere. The
        # repository's identity is still the directory that contains it.
        result["repo"] = candidate.name
        git_dir = dot_git
        if dot_git.is_file():
            try:
                pointer = dot_git.read_text(encoding="utf-8").strip()
                if pointer.startswith("gitdir:"):
                    git_dir = Path(pointer.split(":", 1)[1].strip())
            except OSError:
                return result

        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return result
        if head.startswith("ref: refs/heads/"):
            result["branch"] = head[len("ref: refs/heads/"):]
        elif head:
            result["branch"] = f"detached at {head[:8]}"
        return result

    return result


def encode_project_dir(cwd: str | os.PathLike[str]) -> str:
    """Mirror Claude Code's transcript folder naming (every non-alphanumeric -> '-').

    C:\\src\\my-project  ->  C--src-my-project
    """
    return _NON_ALNUM.sub("-", str(cwd))


def transcript_path(cwd: str | os.PathLike[str], sid: str | None) -> str | None:
    """Best-effort path to the session's .jsonl transcript.

    Tries the derived location first, then falls back to searching every project
    folder for the session id -- which covers sessions that moved with /cd.
    """
    if not sid:
        return None
    projects_root = config.claude_dir() / "projects"
    derived = projects_root / encode_project_dir(cwd) / f"{sid}.jsonl"
    if derived.exists():
        return str(derived)
    try:
        for match in projects_root.glob(f"*/{sid}.jsonl"):
            return str(match)
    except OSError:
        pass
    return str(derived)


def _session_row(sid: str) -> dict | None:
    """The sessions row for this session, whichever board holds it.

    This used to call db.get_session directly, which opens the local SQLite
    file. In a --remote install there is no local board and there is not meant
    to be one, so that call created an empty database inside the sandbox and
    then swallowed the "no such table" that followed -- the exact stray second
    board the remote transport exists to prevent, arriving silently.

    Going through backend means a remote session asks the host over HTTP, and a
    failure is still not worth interrupting anyone for: the caller falls back to
    the ambient environment, which is only less precise.
    """
    try:
        return backend.get_session(sid)
    except Exception:
        return None


def resolve_pid() -> int | None:
    """Pid of the Claude process that owns this session.

    CLAUDE_PID is not trustworthy on its own: a `claude` launched from inside
    another Claude session inherits the parent's CLAUDE_PID, so an MCP server
    can read a pid belonging to a completely different terminal. Acting on that
    would focus the wrong window.

    Hooks do get an accurate pid (they are spawned fresh by the owning session),
    so a pid recorded against this session id wins over the ambient env var.
    """
    sid = session_id()
    if sid:
        row = _session_row(sid)
        if row and row.get("pid"):
            return int(row["pid"])
    return claude_pid()


def resolve_cwd(explicit: str | None = None) -> str:
    """Working directory of the calling session, most trustworthy source first.

    The MCP server process keeps whatever cwd it was spawned with, so a session
    that later runs /cd would otherwise report a stale directory. Hooks always
    report the live cwd, so the sessions table wins over os.getcwd().
    """
    if explicit:
        return str(Path(explicit))
    sid = session_id()
    if sid:
        row = _session_row(sid)
        if row and row.get("cwd"):
            return str(row["cwd"])
    return os.getcwd()


def host_label() -> str:
    """Which machine or sandbox this session is running in.

    Once a board collects blockers from a host and from the containers on it,
    the folder alone stops identifying anything: `/home/u/api` and `C:\\src\\api`
    are different projects, but two sandboxes can both be `/workspace`. Recording
    where a session ran is what keeps a shared board readable -- and it is the
    difference between "test this on the USB interface" meaning your machine or
    meaning a container with no USB at all.
    """
    import platform
    import socket

    name = socket.gethostname().split(".")[0]
    if sys.platform == "win32":
        return name
    # WSL reports itself as Linux; the marker for it is in the kernel string,
    # which is the only place the interop layer names itself.
    release = platform.uname().release.lower()
    if "microsoft" in release or "wsl" in release:
        distro = os.environ.get("WSL_DISTRO_NAME")
        return f"WSL:{distro}" if distro else f"WSL:{name}"
    if os.path.exists("/.dockerenv"):
        return f"docker:{name}"
    if os.environ.get("REMOTE_CONTAINERS") or os.environ.get("CODESPACES"):
        return f"container:{name}"
    return name


# What a card says it belongs to when nothing on this surface can tell us. Better
# a label the user recognises as "the app, not a project" than the name of their
# home folder, which the board would then group other work under.
DESKTOP_PROJECT = "Claude Desktop"


def describe(explicit_cwd: str | None = None, project: str | None = None) -> dict[str, object]:
    """Everything needed to stamp a blocker with its origin.

    The project label prefers, in order: what the caller explicitly named, the
    enclosing git repository, then the folder name. The repository matters
    because a session working in a subdirectory would otherwise be labelled
    with that subdirectory ("src", "api") rather than the project it belongs to.

    On a surface with no working directory of its own -- the Desktop app -- only
    the first of those is available, and the rest are left empty rather than
    filled in from a directory that describes the app instead of the work.
    """
    where = surface()
    if not has_working_directory() and not explicit_cwd:
        # Nothing here is guessed. cwd is "" rather than None because the column
        # is NOT NULL, and an empty string is what the board already treats as
        # "not recorded".
        return {
            "cwd": "",
            "project": project or DESKTOP_PROJECT,
            "repo": None,
            "branch": None,
            "session_id": None,
            "transcript_path": None,
            "claude_pid": None,
            "host": host_label(),
            "surface": where,
        }

    cwd = resolve_cwd(explicit_cwd)
    sid = session_id()
    git = git_info(cwd)
    return {
        "cwd": cwd,
        "project": project or git["repo"] or project_label(cwd),
        "repo": git["repo"],
        "branch": git["branch"],
        "session_id": sid,
        "transcript_path": transcript_path(cwd, sid),
        "claude_pid": resolve_pid(),
        "host": host_label(),
        "surface": where,
    }
