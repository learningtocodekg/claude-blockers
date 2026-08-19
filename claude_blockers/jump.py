"""Getting the user back to the session that raised a blocker.

Two different situations, and conflating them is the usual mistake:

  * The session is still running. The right move is to focus the terminal
    window it already lives in. Resuming it in a second window would fork the
    conversation and leave the original sitting there still waiting.
  * The session has exited. Then `claude --resume <id>` in its directory is
    exactly right.

So this module checks liveness first (via the pid Claude Code exported at the
time the blocker was raised) and only falls back to spawning.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config

IS_WINDOWS = sys.platform == "win32"
# A constant rather than sys.platform inline, for the same reason IS_WINDOWS is
# one: the tests fake the platform to walk branches that do not belong to the
# machine running them, and a comparison buried in a function cannot be faked.
IS_MACOS = sys.platform == "darwin"


# --------------------------------------------------------------------------- #
# process liveness / ancestry (Windows)
# --------------------------------------------------------------------------- #

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

def pid_alive(pid: int | None) -> bool:
    """Is this pid still running?

    Deliberately not os.kill(pid, 0): on Windows Python's os.kill ignores the
    signal and calls TerminateProcess, which would kill the user's session.
    """
    if not pid or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_image_name(pid: int | None) -> str | None:
    """Executable name for a pid, or None if it cannot be read."""
    if not pid or pid <= 0 or not IS_WINDOWS:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return None
    finally:
        kernel32.CloseHandle(handle)


def pid_is_claude(pid: int | None) -> bool:
    """Is this pid actually a live Claude Code process?

    Guards two ways of focusing the wrong terminal: a pid that leaked in from a
    different session, and a pid the OS has since recycled onto some unrelated
    program. On platforms where the image name cannot be read, fall back to mere
    liveness rather than refusing to jump at all.
    """
    if not pid_alive(pid):
        return False
    name = process_image_name(pid)
    if name is None:
        return True
    return "claude" in name or "node" in name


# --------------------------------------------------------------------------- #
# resuming a finished session
# --------------------------------------------------------------------------- #

# Variables Claude Code sets to describe the session a process belongs to.
# A resumed session must not inherit them. The board server is very often
# started from inside a Claude session ("start the board for me"), so without
# this the new terminal comes up believing it is that other session: it inherits
# the CLAUDE_CODE_CHILD_SESSION marker and silently stops saving its transcript,
# which is exactly what the board needs later to show recent turns.
_SESSION_ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID",
    "CLAUDE_JOB_DIR",
    "CLAUDE_CODE_AGENT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_EFFORT",
    "CLAUDECODE",
    "AI_AGENT",
)


def spawn_env() -> dict[str, str]:
    """Environment for a newly launched session: ours, minus session identity."""
    env = dict(os.environ)
    for name in _SESSION_ENV_VARS:
        env.pop(name, None)
    return env


def claude_executable() -> str:
    found = shutil.which("claude")
    if found:
        return found
    env_path = os.environ.get("CLAUDE_CODE_EXECPATH")
    if env_path and Path(env_path).exists():
        return env_path
    return "claude"


def resume_command(session_id: str) -> str:
    return f'claude --resume {session_id}'


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def applescript_quote(value: str) -> str:
    """Escape for inside an AppleScript double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def valid_session_id(session_id: str) -> bool:
    """Is this something Claude Code would have issued?

    Session ids are uuids. This matters because the id is not only displayed --
    it is put on a command line to resume the session, and a blocker's fields can
    come from a sandboxed session over the relay. Anything outside this alphabet
    has no legitimate reason to be here and every reason to be refused.
    """
    return bool(_SAFE_SESSION_ID.match(session_id or ""))


def wsl_resume(cwd: str, session_id: str, exe: str, env: dict[str, str]) -> tuple[bool, str]:
    """Open a Windows terminal that comes straight back into this distro.

    There is no terminal on the Linux side of a WSL install to open -- the window
    belongs to Windows, so the launch goes out through interop and back in:
    wt.exe (or plain `start`) runs wsl.exe, which lands in this distro at the
    session's directory.

    Interop can be switched off (`[interop] enabled=false` in /etc/wsl.conf, or
    appendWindowsPath=false, which is enough to hide wt.exe from PATH). Then
    nothing on this side can open a window at all, and saying so beats failing
    silently.
    """
    distro = config.wsl_distro()
    launcher = shutil.which("wt.exe") or shutil.which("cmd.exe")
    if not launcher:
        where = f"a {distro} shell" if distro else "a WSL shell"
        return False, ("Windows interop is off in this distro, so nothing on this side "
                       f"can open a terminal window. Open {where} yourself and run the "
                       "command below.")

    inner = ["wsl.exe"] + (["-d", distro] if distro else [])
    inner += ["--cd", cwd, "--", exe, "--resume", session_id]
    command = inner if launcher.lower().endswith("wt.exe") else ["/c", "start", "", *inner]
    try:
        subprocess.Popen([launcher, *command], close_fds=True, env=env)
    except OSError as exc:
        return False, f"Could not reach Windows to open a terminal: {exc}"
    return True, "Opened a Windows terminal resuming that session in WSL."


def spawn_resume(cwd: str, session_id: str) -> tuple[bool, str]:
    """Open a new terminal in `cwd` resuming `session_id`."""
    if not valid_session_id(session_id):
        return False, "That session id is not a well-formed id; refusing to run it."
    if not Path(cwd).is_dir():
        return False, f"Directory no longer exists: {cwd}"
    exe = claude_executable()
    env = spawn_env()

    # Tried before the Linux terminals below, not instead of them: a WSL install
    # with a desktop session can genuinely have gnome-terminal, and that is a
    # better answer than the "open one yourself" message interop-off ends at.
    fallback = None
    if not IS_WINDOWS and config.is_wsl():
        ok, message = wsl_resume(cwd, session_id, exe, env)
        if ok:
            return True, message
        fallback = message

    if IS_WINDOWS:
        wt = shutil.which("wt")
        try:
            if wt:
                subprocess.Popen(
                    [wt, "-d", cwd, exe, "--resume", session_id],
                    close_fds=True, env=env,
                )
            else:
                # A list, not a string, and no shell. The previous form built one
                # command line with the folder and id pasted in and handed it to
                # cmd, which would run whatever they happened to contain.
                subprocess.Popen(
                    ["cmd", "/c", "start", f"Claude {session_id[:8]}",
                     "cmd", "/k", f"cd /d {cwd} && \"{exe}\" --resume {session_id}"],
                    close_fds=True, env=env,
                )
            return True, "Opened a new terminal resuming that session."
        except OSError as exc:
            return False, f"Could not launch a terminal: {exc}"

    if IS_MACOS:
        # AppleScript string literals are double-quoted and understand their own
        # backslash escapes, so POSIX single-quoting does nothing for them.
        script = (
            f'tell application "Terminal" to do script '
            f'"cd {applescript_quote(json_quote(cwd))} && '
            f'{applescript_quote(exe)} --resume {session_id}"\n'
            f'tell application "Terminal" to activate'
        )
        try:
            subprocess.Popen(["osascript", "-e", script], close_fds=True, env=env)
            return True, "Opened Terminal.app resuming that session."
        except OSError as exc:
            return False, f"Could not launch Terminal: {exc}"

    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        path = shutil.which(term)
        if path:
            try:
                subprocess.Popen(
                    [path, "-e", "bash", "-lc", f'cd {json_quote(cwd)} && {exe} --resume {session_id}'],
                    close_fds=True, env=env,
                )
                return True, f"Opened {term} resuming that session."
            except OSError:
                continue
    return False, fallback or "No terminal emulator found. Copy the command instead."


def json_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# entrypoint used by the web API
# --------------------------------------------------------------------------- #

def jump(blocker: dict[str, Any]) -> dict[str, Any]:
    session_id = blocker.get("session_id")
    cwd = blocker.get("cwd") or ""
    pid = blocker.get("claude_pid")

    if not session_id:
        return {
            "ok": False,
            "action": "none",
            "message": "This blocker has no session id recorded.",
            "command": None,
        }

    command = resume_command(session_id)
    live = pid_is_claude(int(pid)) if pid else False

    # There used to be a "focus the existing terminal" branch here. It is gone:
    # Windows Terminal keeps every session in tabs and exposes no way to
    # activate one from outside the process, so the best it could ever manage
    # was raising a window and naming a tab -- while its ShowWindow call pulled
    # a maximised terminal out of fullscreen on the way. Showing the session id
    # and letting the user switch tabs themselves is faster and less invasive.
    ok, message = spawn_resume(cwd, session_id)
    if ok and live:
        message = ("That session is still running -- this opened a SECOND copy of the "
                   "conversation. Close it and switch to the original tab if you did "
                   "not want to fork it.")
    return {
        "ok": ok,
        "action": "resume",
        "message": message,
        "command": command,
        "cwd": cwd,
    }
