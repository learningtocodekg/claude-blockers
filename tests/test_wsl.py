"""What changes when Linux turns out to be WSL.

    uv run python tests/test_wsl.py

Runs anywhere -- WSL is faked with the environment variable that marks it, so
this is the same test on Windows, on WSL, and on a real Linux box.

WHAT WENT WRONG. Nothing crashed; it just quietly did nothing. `spawn_resume`
walked the Linux terminal emulators, found no gnome-terminal or xterm in a distro
that has no desktop, and reported "No terminal emulator found" -- true, and no
help at all, because the terminal that could resume the session was never going
to be on this side. Same shape for the board: `webbrowser.open` has nothing to
open under WSL, so `serve` claimed to have opened a browser that never appeared.
Both paths now go out through interop, and when interop is off they say which
distro to open a shell in rather than failing mute.
"""

from __future__ import annotations

import os
import shutil as real_shutil
import subprocess as real_subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["CLAUDE_BLOCKERS_HOME"] = tempfile.mkdtemp(prefix="claude-blockers-wsl-")

from claude_blockers import backend, cli, config, jump, web  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


def as_wsl(distro: str = "Ubuntu-24.04") -> None:
    os.environ["WSL_DISTRO_NAME"] = distro
    config.is_wsl.cache_clear()


def as_linux() -> None:
    os.environ.pop("WSL_DISTRO_NAME", None)
    os.environ.pop("WSL_INTEROP", None)
    config.is_wsl.cache_clear()


class Interop:
    """Stands in for what interop exposes, and records what we asked it to run.

    Doubles as both `shutil` and `subprocess` for the module under test, so which
    launchers exist is a dict rather than the state of this machine.
    """

    DEVNULL = real_subprocess.DEVNULL

    def __init__(self, available: dict[str, str]) -> None:
        self.available = available
        self.calls: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return self.available.get(name)

    def Popen(self, command, **_kwargs):  # noqa: N802 -- stands in for subprocess
        self.calls.append(list(command))
        return None


@contextmanager
def interop(module, available: dict[str, str]):
    fake = Interop(available)
    module.shutil, module.subprocess = fake, fake
    try:
        yield fake
    finally:
        module.shutil, module.subprocess = real_shutil, real_subprocess


CWD = str(Path(__file__).resolve().parent)
CLAUDE = "/home/me/.local/bin/claude"

print("\nKnowing where we are")

as_wsl()
check("the distro variable is enough", config.is_wsl())
check("and the distro is named", config.wsl_distro() == "Ubuntu-24.04")

as_linux()
os.environ["WSL_INTEROP"] = "/run/WSL/8_interop"
config.is_wsl.cache_clear()
check("so is the interop socket", config.is_wsl())
as_linux()

print("\nResuming a session with Windows Terminal available")

as_wsl()
with interop(jump, {"wt.exe": "/mnt/c/wt.exe"}) as fake:
    ok, message = jump.wsl_resume(CWD, "abc123", CLAUDE, {})
argv = fake.calls[0] if fake.calls else []

check("it reports success", ok, message)
check("wt.exe is what runs", argv[:1] == ["/mnt/c/wt.exe"], f"got {argv}")
check("it comes back into this distro",
      argv[1:4] == ["wsl.exe", "-d", "Ubuntu-24.04"], f"got {argv}")
check("in the session's own directory", argv[4:6] == ["--cd", CWD], f"got {argv}")
check("resuming that session id",
      argv[6:] == ["--", CLAUDE, "--resume", "abc123"], f"got {argv}")

print("\nResuming with only cmd.exe")

with interop(jump, {"cmd.exe": "/mnt/c/Windows/System32/cmd.exe"}) as fake:
    ok, message = jump.wsl_resume(CWD, "abc123", CLAUDE, {})
argv = fake.calls[0] if fake.calls else []

check("it still opens something", ok, message)
# The empty string is the window title `start` would otherwise take from the
# first quoted argument it sees, leaving wsl.exe unrun.
check("start gets a title before the command", argv[1:4] == ["/c", "start", ""],
      f"got {argv}")
check("and wsl.exe follows it", argv[4] == "wsl.exe" if len(argv) > 4 else False,
      f"got {argv}")

print("\nResuming with interop switched off")

with interop(jump, {}) as fake:
    ok, message = jump.wsl_resume(CWD, "abc123", CLAUDE, {})

check("it does not claim to have opened anything", not ok)
check("nothing was launched", fake.calls == [], f"got {fake.calls}")
check("the message names the distro", "Ubuntu-24.04" in message, f"got {message!r}")
check("and says interop is the reason", "interop" in message.lower(), f"got {message!r}")

print("\nAnd the message survives the walk through the Linux terminals")

# spawn_resume tries the emulators after this, because a WSL install with a
# desktop can have one. What it must not do is bury the WSL message when it
# comes back empty-handed. IS_WINDOWS goes with the faked platform: this test
# runs on Windows too, where that branch would otherwise be the one taken.
was_windows, jump.IS_WINDOWS = jump.IS_WINDOWS, False
was_macos, jump.IS_MACOS = jump.IS_MACOS, False
try:
    with interop(jump, {}):
        ok, message = jump.spawn_resume(CWD, "abc123")
finally:
    jump.IS_WINDOWS, jump.IS_MACOS = was_windows, was_macos
check("no terminal, and the reason is still the WSL one", not ok, message)
check("not the generic line", "interop" in message.lower(), f"got {message!r}")

print("\nThe macOS terminal, from any machine")

# is_wsl() and darwin can never both be true on a real machine, so this is the
# ordinary Mac case: no WSL, and Terminal.app is what opens.
as_linux()
was_windows, jump.IS_WINDOWS = jump.IS_WINDOWS, False
was_macos, jump.IS_MACOS = jump.IS_MACOS, True
try:
    with interop(jump, {"osascript": "/usr/bin/osascript"}) as fake:
        ok, message = jump.spawn_resume(CWD, "abc123")
finally:
    jump.IS_WINDOWS, jump.IS_MACOS = was_windows, was_macos

check("it opens Terminal.app", ok, message)
check("and says so", "Terminal" in message, f"got {message!r}")
check("through osascript", bool(fake.calls) and fake.calls[0][0] == "osascript",
      f"got {fake.calls}")
check("carrying the resume command",
      bool(fake.calls) and "--resume abc123" in " ".join(fake.calls[0]),
      f"got {fake.calls}")
as_wsl()

print("\nShowing the board")

URL = "http://127.0.0.1:4317/"

with interop(web, {"wslview": "/usr/bin/wslview"}) as fake:
    web._open_board(URL)
check("wslview carries the URL to Windows", fake.calls == [["/usr/bin/wslview", URL]],
      f"got {fake.calls}")

with interop(web, {"explorer.exe": "/mnt/c/Windows/explorer.exe"}) as fake:
    web._open_board(URL)
check("explorer.exe will do as well",
      fake.calls == [["/mnt/c/Windows/explorer.exe", URL]], f"got {fake.calls}")

with interop(web, {}) as fake:
    web._open_board(URL)
check("with no way across, nothing is launched", fake.calls == [], f"got {fake.calls}")

print("\nWith no marker set")

# Not "so this is not WSL": on real WSL the kernel still says microsoft with the
# variables cleared, and this test runs there too. What is being checked is that
# the fallback reads /proc/version rather than assuming either answer.
as_linux()
proc = Path("/proc/version")
kernel_says_wsl = (proc.is_file()
                   and "microsoft" in proc.read_text(errors="replace").lower())
check("the kernel decides, and a plain Linux box is left to webbrowser",
      config.is_wsl() == kernel_says_wsl,
      f"is_wsl()={config.is_wsl()}, /proc/version says {kernel_says_wsl}")

# --------------------------------------------------------------------------- #
print("\nA distro quietly keeping its own board")

# The failure this exists for. Nothing errors: the server starts, cards are
# written, ids come back -- into a database on the distro's own disk, while the
# board being watched is a different file on Windows. It went unnoticed until a
# card numbered #169 was raised onto a board whose highest id was 15. Every
# individual piece was working, which is exactly why it needs saying out loud.
os.environ.pop("CLAUDE_BLOCKERS_URL", None)

as_linux()
# as_linux() drops the env vars, but it cannot make is_wsl() false when this
# suite is itself running inside WSL -- /proc/version still says microsoft, and
# that is the fallback. Which is a fair place to run it, being where anyone
# debugging the WSL paths would be. So say what is meant instead of arranging
# for it: on something that is not WSL, there is nothing to warn about.
_real_is_wsl = config.is_wsl
config.is_wsl = lambda: False
check("a plain Linux box is not warned", cli._split_brain_warning() == [],
      "there is no other side for it to be split from")
config.is_wsl = _real_is_wsl

as_wsl()
warning = cli._split_brain_warning()
check("WSL with a local database is warned", bool(warning))
check("it names the distro", any("Ubuntu-24.04" in line for line in warning), f"got {warning}")
check("and the file being written", any(str(config.db_path()) in line for line in warning),
      f"got {warning}")
check("and says a Windows board will not show them",
      any("never show" in line for line in warning), f"got {warning}")
check("and gives both halves of the fix",
      any("--host 0.0.0.0" in line for line in warning)
      and any("--remote" in line for line in warning), f"got {warning}")
check("and a host name that survives a reboot",
      any("mshome.net" in line for line in warning),
      "the gateway address is reassigned on every boot")
# --token <value> with a leading dash is read by argparse as another flag, so
# the documented form here has to be the one that always works.
check("the install line uses --token=, not --token ",
      any("--token=" in line for line in warning), f"got {warning}")

os.environ["CLAUDE_BLOCKERS_URL"] = "http://host.mshome.net:4317/"
check("a relayed WSL session is not warned", cli._split_brain_warning() == [],
      "it is already posting to the host's board")
os.environ.pop("CLAUDE_BLOCKERS_URL", None)
as_linux()

# --------------------------------------------------------------------------- #
print("\nReaching a board whose address keeps moving")

# The board refuses any request whose Host header is a name -- that is its
# DNS-rebinding defence and it stays. But WSL sits behind a NAT whose subnet is
# picked afresh every time the utility VM cold-starts, so the gateway is a
# different 172.x each time and a URL with last week's number in it fails at the
# moment someone needed to raise a blocker. So the name is what gets configured
# and the number is resolved per call, and neither side has to give anything up.
import socket as _socket  # noqa: E402

_real_getaddrinfo = _socket.getaddrinfo


def resolving_to(address):
    def fake(host, port, *a, **kw):
        if host == "board.example":
            family = _socket.AF_INET6 if ":" in address else _socket.AF_INET
            return [(family, _socket.SOCK_STREAM, _socket.IPPROTO_TCP, "", (address, port))]
        raise OSError(f"unknown host {host}")
    return fake


_socket.getaddrinfo = resolving_to("172.25.224.1")
check("a named host becomes the address it resolves to",
      backend._numeric_url("http://board.example:4317/") == "http://172.25.224.1:4317/",
      backend._numeric_url("http://board.example:4317/"))

# The whole point: the same configured URL follows the address across a restart.
_socket.getaddrinfo = resolving_to("172.19.16.1")
check("and follows it when the subnet is reassigned",
      backend._numeric_url("http://board.example:4317/") == "http://172.19.16.1:4317/",
      backend._numeric_url("http://board.example:4317/"))

_socket.getaddrinfo = resolving_to("fd00::1")
check("an IPv6 answer is bracketed so the URL still parses",
      backend._numeric_url("http://board.example:4317/") == "http://[fd00::1]:4317/",
      backend._numeric_url("http://board.example:4317/"))

_socket.getaddrinfo = _real_getaddrinfo
check("an address is left exactly as it is",
      backend._numeric_url("http://172.25.224.1:4317/") == "http://172.25.224.1:4317/")
check("localhost is left alone, the board takes that one by name",
      backend._numeric_url("http://localhost:4317/") == "http://localhost:4317/")
# Rewriting the host under https would break certificate validation, which is a
# worse trade than a board that has to be addressed numerically.
check("https is never rewritten",
      backend._numeric_url("https://board.example:4317/") == "https://board.example:4317/")


def _explodes(*a, **kw):
    raise OSError("no resolver here")


_socket.getaddrinfo = _explodes
check("a name that will not resolve is left to fail by name",
      backend._numeric_url("http://board.example:4317/") == "http://board.example:4317/",
      "the connection error then names the host someone actually configured")
_socket.getaddrinfo = _real_getaddrinfo

# --------------------------------------------------------------------------- #
print("\nTokens that can be passed on a command line")

# secrets.token_urlsafe draws from an alphabet including '-', so about one token
# in 64 used to start with one. Every documented way of passing it is a command
# line, and argparse reads a leading dash as the start of another flag: the
# error is "argument --token: expected one argument", which names neither the
# token nor the dash, and the board looks broken instead.
minted = []
for _ in range(200):
    (config.home() / "token").unlink(missing_ok=True)
    minted.append(config.ensure_token())
check("no minted token starts with a dash",
      not any(t.startswith("-") for t in minted),
      f"got {[t for t in minted if t.startswith('-')][:3]}")
check("they are still long enough to be secrets",
      all(len(t) >= 32 for t in minted), f"shortest {min(len(t) for t in minted)}")
check("and they are still different every time", len(set(minted)) == len(minted))

print(f"\n{'=' * 58}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{'=' * 58}\n")
sys.exit(1 if FAIL else 0)
