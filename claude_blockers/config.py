"""Where claude-blockers keeps its state, and how it is reached.

Everything is overridable by environment variable so the installer can point a
machine at a custom location (say, D:\\boards\\blockers)
while a fresh clone still works with zero configuration.
"""

from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path

DEFAULT_PORT = 4317


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Is this Linux running inside WSL rather than on its own machine?

    Two user-visible things depend on the answer: the terminal that resumes a
    session lives on the Windows side, and so does the browser that would show
    the board. A plain Linux box has neither problem.
    """
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="replace").lower()
    except OSError:
        return False


def wsl_distro() -> str:
    """Name of the distro we are in, for telling the user where to look."""
    return os.environ.get("WSL_DISTRO_NAME", "")


# The project was called blocker-board before it was called claude-blockers.
# Anyone who installed it then has the old variable written into their Claude
# Code settings and a database sitting at the path it points to, so the old name
# keeps working -- losing someone's board to a rename would be unforgivable.
_LEGACY_HOME_VAR = "BLOCKER_BOARD_HOME"
_LEGACY_PORT_VAR = "BLOCKER_BOARD_PORT"


def _usable_here(value: str) -> bool:
    """Is this recorded path meaningful on the platform reading it?

    A WSL container or a devcontainer can be pointed at a Windows-side config
    directory, and then `settings.json` hands it a path like `C:\\Users\\you\\.claude-blockers`.
    Linux does not reject that -- it quietly treats it as a *relative* directory
    named `C:\\Users\\you\\.claude-blockers` and creates it inside whatever the working
    directory happens to be, scattering half-boards through the filesystem. The
    cheap structural check is worth it because the failure is silent.
    """
    value = value.strip()
    if not value:
        return False
    looks_windows = (len(value) > 1 and value[1] == ":" and value[0].isalpha()) \
        or value.startswith("\\\\")
    if os.name == "nt":
        return True  # Windows understands POSIX-style separators too.
    return not looks_windows


def _home_from_settings() -> str | None:
    """The home `install` wrote into Claude Code's settings.

    Claude Code injects that `env` block into the processes it spawns, so the MCP
    server and the hooks see CLAUDE_BLOCKERS_HOME and agree on a database. A
    terminal you opened yourself does not: run `claude-blockers serve` from an
    ordinary shell and, without this, it would resolve to the default path and
    show you an empty board while every session was writing somewhere else.
    Reading the same file the installer wrote is what makes the command find the
    real board from anywhere.
    """
    try:
        settings = json.loads((claude_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    env = settings.get("env")
    if not isinstance(env, dict):
        return None
    for var in ("CLAUDE_BLOCKERS_HOME", _LEGACY_HOME_VAR):
        value = env.get(var)
        if isinstance(value, str) and _usable_here(value):
            return value
    return None


def home() -> Path:
    """Directory holding the database. Created on demand."""
    override = (os.environ.get("CLAUDE_BLOCKERS_HOME")
                or os.environ.get(_LEGACY_HOME_VAR)
                or _home_from_settings())
    if override:
        root = Path(override).expanduser()
    else:
        root = Path.home() / ".claude-blockers"
        legacy = Path.home() / ".blocker-board"
        # Only adopt the old default if it actually holds a board and the new
        # location does not -- otherwise a fresh install would silently latch
        # onto a stale directory.
        if not (root / "blockers.db").exists() and (legacy / "blockers.db").exists():
            root = legacy
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return home() / "blockers.db"


def port() -> int:
    raw = os.environ.get("CLAUDE_BLOCKERS_PORT") or os.environ.get(_LEGACY_PORT_VAR)
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


def board_url() -> str:
    return remote_url() or f"http://127.0.0.1:{port()}/"


# --------------------------------------------------------------------------- #
# Reaching a board that lives outside this machine's filesystem
# --------------------------------------------------------------------------- #
# A session inside WSL, a devcontainer or Docker cannot share the host's SQLite
# file: the database sits on a Windows drive, and the moment two sides write at
# once the crossing filesystem fails the lock outright with "disk I/O error"
# rather than blocking and retrying. So a sandboxed session does not open the
# database at all -- it posts to the board over HTTP instead, and the host stays
# the only process that touches the file.

def remote_url() -> str | None:
    """Board to talk to over HTTP, instead of opening the database directly."""
    raw = (os.environ.get("CLAUDE_BLOCKERS_URL") or "").strip()
    return raw.rstrip("/") + "/" if raw else None


def token() -> str | None:
    """Shared secret for requests that do not come from this machine."""
    raw = (os.environ.get("CLAUDE_BLOCKERS_TOKEN") or "").strip()
    if raw:
        return raw
    try:
        return (home() / "token").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def ensure_token() -> str:
    """The board's token, minting one on first use."""
    existing = token()
    if existing:
        return existing
    import secrets
    # token_urlsafe draws from an alphabet that includes '-', so roughly one
    # token in 64 starts with one. Every documented way of passing it is a
    # command line -- `--token <value>`, and argparse reads a leading dash as
    # the start of another flag: "argument --token: expected one argument",
    # which names neither the token nor the dash. The board it was minted for
    # then looks broken. Nothing anywhere needs tokens to begin with a dash, so
    # they do not.
    while True:
        value = secrets.token_urlsafe(32)
        if not value.startswith("-"):
            break
    path = home() / "token"
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows ACLs, not POSIX modes
        pass
    return value


def _packaged_desktop_dirs() -> list[Path]:
    """Where an MSIX-packaged Claude Desktop keeps its redirected roaming data.

    This is the normal Windows case, not an exotic one: the installer from the
    website registers a package the same way, and `Get-AppxPackage Claude`
    reports it with SignatureKind "Developer" rather than "Store".

    Sorted so a directory that already holds the config wins over one that merely
    exists -- a machine can carry more than one package registration, and only
    one of them is the app being run.
    """
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    try:
        found = [
            path for path in root.glob("Packages/Claude_*/LocalCache/Roaming/Claude")
            if path.is_dir()
        ]
    except OSError:
        return []
    found.sort(key=lambda path: (path / "claude_desktop_config.json").is_file(), reverse=True)
    return found


def desktop_config_path() -> Path:
    """Claude Desktop's MCP config file.

    The app owns this file and writes its own keys into it, so everything that
    touches it merges rather than replaces. The env var exists so the tests can
    point somewhere disposable -- there is no other reason to override it.
    """
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override).expanduser()
    name = "claude_desktop_config.json"
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        classic = root / "Claude" / name
        if classic.parent.is_dir():
            return classic
        # The Windows build of Claude Desktop ships as an MSIX package -- that is
        # what the download from Anthropic's own site installs, not only a copy
        # from the Store -- and Windows redirects a packaged app's %APPDATA%
        # writes into a per-package sandbox. The file the app actually reads is
        # the one down there; the path above is a folder that simply never
        # appears, so looking only at it reports "no Claude Desktop found" on a
        # perfectly ordinary install.
        for found in _packaged_desktop_dirs():
            return found / name
        return classic
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / name
    # Desktop does not ship for plain Linux today. Point at where an Electron app
    # following XDG would put it, so that if it ever does, this is already right.
    return Path.home() / ".config" / "Claude" / name


def desktop_installed() -> bool:
    """Is Claude Desktop on this machine?

    The config file itself is written on first use and may not exist yet, so the
    directory is the honest test -- the app creates it on install.
    """
    return desktop_config_path().parent.is_dir()


# The Claude Code CLI installs itself as claude.exe too, so on Windows the app
# and the terminal sessions share an image name. Matching on the name alone
# reports the app as running whenever any session is open -- which, for the
# person this tool is written for, is always. The CLI is the one under
# node_modules/@anthropic-ai/claude-code; excluding it by path rather than
# requiring the app's own path keeps working if the app ever ships unpackaged.
_CLI_MARKER = "claude-code"


def _is_desktop_path(path: str) -> bool:
    """Does this running Claude.exe belong to the app rather than the CLI?

    Kept apart from the process listing so the rule can be tested without a
    Windows machine and without any Claude running on it.
    """
    return bool(path.strip()) and _CLI_MARKER not in path.lower()


def _windows_desktop_paths() -> list[str]:
    """Full paths of every Claude.exe running, so the CLI can be told apart.

    tasklist cannot report paths and wmic is gone from current Windows, so this
    goes through PowerShell. It costs a few hundred milliseconds, which is fine
    somewhere only `install` and `status` reach.
    """
    import subprocess
    script = ("Get-CimInstance Win32_Process -Filter \"Name='Claude.exe'\" | "
              "ForEach-Object { $_.ExecutablePath }")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def desktop_running() -> bool:
    """Is Claude Desktop up right now?

    This matters because the app does not merge with its config file, it
    serialises over it: it holds the config in memory and writes the whole object
    back when something changes. An entry added while it is running is discarded
    the next time it writes -- silently, with no error and no trace in the app.
    That is what happened on the first real install here, and the symptom is the
    worst kind: `install` reports success, `status` reports "wired", and then the
    connector simply is not in the app.

    Nothing depends on the answer being right, so a machine whose process list
    cannot be read is treated as "not running" rather than made to fail. A false
    negative costs a confusing afternoon; a false positive tells someone to quit
    an app they already quit, which is worse, because it makes the warning
    something to learn to ignore.
    """
    if os.name == "nt":
        return any(_is_desktop_path(path) for path in _windows_desktop_paths())
    if sys.platform == "darwin":
        # The app's binary is `Claude`, the CLI is `claude`, and pgrep -x is
        # case-sensitive and exact -- so unlike Windows there is nothing to
        # disambiguate here.
        import subprocess
        try:
            result = subprocess.run(["pgrep", "-x", "Claude"],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and bool((result.stdout or "").strip())
    return False  # Desktop does not ship for plain Linux today.


def claude_dir() -> Path:
    """Claude Code's config directory (~/.claude, unless relocated)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def static_dir() -> Path:
    return Path(__file__).parent / "static"
