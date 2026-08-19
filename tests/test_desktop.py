"""Wiring the Claude Desktop app, and what a blocker raised there looks like.

    uv run python tests/test_desktop.py

Works on a temporary claude_desktop_config.json and a temporary database, so it
never reads or writes the real ones.

WHAT WENT WRONG. The Desktop app speaks the same stdio MCP protocol as Claude
Code, so pointing it at this server "just worked" -- except for the part that did
not. Desktop exports no CLAUDE_CODE_SESSION_ID and spawns the server in the
user's home folder rather than in whatever the conversation is about. So
context.describe() fell through to os.getcwd() and filed every Desktop blocker
under the home folder's name, with whatever git repository happened to sit above
it stapled on -- a whole board grouped under whatever the home folder is
called. A surface
with no working directory of its own does not get to guess one, and the tests for
that are at the bottom of this file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="claude-blockers-desktop-"))
HOME = TMP / "home"
DESKTOP = TMP / "Claude" / "claude_desktop_config.json"
DESKTOP.parent.mkdir(parents=True, exist_ok=True)

os.environ["CLAUDE_BLOCKERS_HOME"] = str(HOME)
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude")
os.environ["CLAUDE_DESKTOP_CONFIG"] = str(DESKTOP)
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
os.environ.pop("CLAUDE_BLOCKERS_SURFACE", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_blockers import cli, config, context, db  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


def read() -> dict:
    return json.loads(DESKTOP.read_text(encoding="utf-8"))


BAR = "=" * 58

# --------------------------------------------------------------------------- #
print("Where the config lives")

check("the env var overrides the platform path", config.desktop_config_path() == DESKTOP)
check("a present directory means Desktop is installed", config.desktop_installed())

# The Windows build of Claude Desktop ships as an MSIX package -- including the
# download from Anthropic's own site, which registers with SignatureKind
# "Developer" rather than "Store" -- and Windows redirects a packaged app's
# %APPDATA% writes into a per-package sandbox. Looking only at %APPDATA%\Claude
# reports "no Claude Desktop found" on a machine that plainly has it, which is
# exactly what happened on the first real install.
if os.name == "nt":
    del os.environ["CLAUDE_DESKTOP_CONFIG"]
    fake = TMP / "winroot"
    os.environ["APPDATA"] = str(fake / "Roaming")
    os.environ["LOCALAPPDATA"] = str(fake / "Local")
    packaged = fake / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    packaged.mkdir(parents=True)
    check("a Store install is found in its package sandbox",
          config.desktop_config_path().parent == packaged,
          f"got {config.desktop_config_path()}")
    check("and counts as installed", config.desktop_installed())

    # An unpackaged install owns the plain path, and wins when both exist.
    classic = fake / "Roaming" / "Claude"
    classic.mkdir(parents=True)
    check("a classic install still wins when it is there",
          config.desktop_config_path().parent == classic,
          f"got {config.desktop_config_path()}")

    # Two package registrations, only one of which the app actually uses.
    other = fake / "Local" / "Packages" / "Claude_otherpkg000" / "LocalCache" / "Roaming" / "Claude"
    other.mkdir(parents=True)
    (packaged / "claude_desktop_config.json").write_text("{}", encoding="utf-8")
    import shutil
    shutil.rmtree(classic)
    check("the package holding a config wins over one that is merely there",
          config.desktop_config_path().parent == packaged,
          f"got {config.desktop_config_path()}")

    os.environ["CLAUDE_DESKTOP_CONFIG"] = str(DESKTOP)

# --------------------------------------------------------------------------- #
print("\nMerging into a config the app already owns")

# The app writes its own keys here. Anything we do has to survive them, and they
# have to survive us -- clobbering someone's other MCP servers would be the kind
# of bug that gets this uninstalled.
DESKTOP.write_text(json.dumps({
    "globalShortcut": "Alt+Space",
    "mcpServers": {"someone-elses": {"command": "/usr/bin/other", "args": ["x"]}},
}), encoding="utf-8")

cli._install_desktop(HOME)
cfg = read()
check("an unrelated top-level key survives", cfg.get("globalShortcut") == "Alt+Space")
check("another server survives", "someone-elses" in cfg["mcpServers"], f"got {cfg}")
check("our server is there", cli.MCP_KEY in cfg["mcpServers"])

entry = cfg["mcpServers"][cli.MCP_KEY]
check("it runs this interpreter", entry["command"] == sys.executable, f"got {entry}")
check("it runs the mcp subcommand", entry["args"] == ["-m", "claude_blockers", "mcp"])
check("it names the database", entry["env"]["CLAUDE_BLOCKERS_HOME"] == str(HOME))
check("it declares the surface",
      entry["env"]["CLAUDE_BLOCKERS_SURFACE"] == context.CLAUDE_DESKTOP,
      f"got {entry['env']}")
# --remote exists for sandboxes that cannot reach the host's database. Desktop
# runs on the host, where the database already is, so it never posts over HTTP.
check("it never carries a remote url", "CLAUDE_BLOCKERS_URL" not in entry["env"])
check("it never carries a token", "CLAUDE_BLOCKERS_TOKEN" not in entry["env"])

backup = DESKTOP.with_suffix(DESKTOP.suffix + ".claude-blockers.bak")
check("the original was backed up", backup.is_file())
pristine = json.loads(backup.read_text(encoding="utf-8"))
check("the backup is the pristine copy", cli.MCP_KEY not in pristine.get("mcpServers", {}))

# --------------------------------------------------------------------------- #
print("\nRe-running install (the documented way to upgrade)")

cli._install_desktop(HOME)
cfg = read()
check("still exactly one of ours", list(cfg["mcpServers"]).count(cli.MCP_KEY) == 1)
check("the other server is still there", "someone-elses" in cfg["mcpServers"])
still = json.loads(backup.read_text(encoding="utf-8"))
check("the pristine backup was not overwritten",
      cli.MCP_KEY not in still.get("mcpServers", {}), f"got {still}")

# --------------------------------------------------------------------------- #
print("\nThe old name")

cfg["mcpServers"][cli.LEGACY_MCP_NAME] = {"command": "old", "args": []}
DESKTOP.write_text(json.dumps(cfg), encoding="utf-8")
cli._install_desktop(HOME)
check("the pre-rename entry is dropped",
      cli.LEGACY_MCP_NAME not in read()["mcpServers"],
      "two entries would start two servers and double every tool in the picker")

# --------------------------------------------------------------------------- #
print("\nStatus")

lines = cli._desktop_status()
check("reports it as wired", lines[0] == "wired", f"got {lines}")

cfg = read()
cfg["mcpServers"][cli.MCP_KEY]["command"] = str(TMP / "gone" / "python.exe")
DESKTOP.write_text(json.dumps(cfg), encoding="utf-8")
lines = cli._desktop_status()
check("an interpreter that is not there is called out",
      any("is not there" in line for line in lines), f"got {lines}")
check("and says how to fix it", any("install" in line for line in lines), f"got {lines}")

cli._install_desktop(HOME)  # put it back

# --------------------------------------------------------------------------- #
print("\nWriting underneath a running app")

# The failure this guards against: the app keeps this file in memory and saves
# the whole object back over itself, so an entry written while it is up is
# discarded with no error anywhere. install reported success, status reported
# "wired", and the connector was simply absent from the app. Saying nothing here
# sends someone hunting through the app's settings for a bug that is not there.
_real_running = config.desktop_running

config.desktop_running = lambda: True
notes = cli._install_desktop(HOME)
check("a running app is called out", any("RUNNING" in n for n in notes), f"got {notes}")
check("and it says the write will be lost",
      any("discard" in n for n in notes), f"got {notes}")
check("and that closing the window is not quitting",
      any("closing the window" in n for n in notes), f"got {notes}")
check("and to run install again afterwards",
      any("install` again" in n for n in notes), f"got {notes}")
check("the entry is still written anyway", cli.MCP_KEY in read()["mcpServers"],
      "refusing to write would leave nothing for the re-run to find")

# Status is where someone lands when the connector did not appear, so the same
# explanation has to be reachable from there.
cfg = read()
del cfg["mcpServers"][cli.MCP_KEY]
DESKTOP.write_text(json.dumps(cfg), encoding="utf-8")
lines = cli._desktop_status()
check("status says to quit the app first",
      any("quit Claude Desktop" in line for line in lines), f"got {lines}")

config.desktop_running = lambda: False
notes = cli._install_desktop(HOME)
check("a quit app is not warned about", not any("RUNNING" in n for n in notes), f"got {notes}")
lines = cli._desktop_status()
check("and status stays quiet about quitting",
      not any("quit Claude Desktop" in line for line in lines), f"got {lines}")

config.desktop_running = _real_running

# Telling the app apart from the CLI. On Windows both are claude.exe, so a check
# on the image name alone answers "running" whenever any Claude Code session is
# open -- which for anyone this tool is built for is always, making the warning
# something you learn to ignore, and sending you to quit an app you already quit.
# These are the real shapes of the paths involved.
APP = r"C:\Program Files\WindowsApps\Claude_1.32885.1.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
CLI = r"C:\Users\me\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
# The CLI also installs itself under a folder called Claude, so "is it under a
# Claude directory" is not the question -- excluding claude-code by name is.
CLI_UNDER_CLAUDE = r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.234\claude.exe"

check("the packaged app counts", config._is_desktop_path(APP))
check("the CLI does not", not config._is_desktop_path(CLI))
check("nor the CLI living under a Claude folder", not config._is_desktop_path(CLI_UNDER_CLAUDE),
      "a path-prefix test would have called this one the app")
check("nor an empty line from the process listing", not config._is_desktop_path("   "))

_real_paths = config._windows_desktop_paths
config._windows_desktop_paths = lambda: [CLI, CLI_UNDER_CLAUDE]
check("sessions alone do not read as a running app",
      not config.desktop_running() if os.name == "nt" else True,
      "this is the false positive that would make the warning useless")
config._windows_desktop_paths = lambda: [CLI, APP, CLI_UNDER_CLAUDE]
check("the app among them does",
      config.desktop_running() if os.name == "nt" else True)
config._windows_desktop_paths = lambda: []
check("and nothing running reads as not running",
      not config.desktop_running() if os.name == "nt" else True)
config._windows_desktop_paths = _real_paths
# The real check shells out to the platform's process list. It has to answer for
# this machine without raising, whatever the answer is.
check("the real check returns a bool without raising",
      isinstance(config.desktop_running(), bool))

# --------------------------------------------------------------------------- #
print("\nUninstall")

note = cli._remove_desktop(DESKTOP)
cfg = read()
check("our entry is gone", cli.MCP_KEY not in cfg["mcpServers"], f"got {cfg}")
check("the other server survives", "someone-elses" in cfg["mcpServers"], f"got {cfg}")
check("an unrelated top-level key survives", cfg.get("globalShortcut") == "Alt+Space")
check("it says what it did", "Claude Desktop" in note, f"got {note!r}")

note = cli._remove_desktop(DESKTOP)
check("removing twice is not an error", "not wired" in note, f"got {note!r}")

# --------------------------------------------------------------------------- #
print("\nWhat the surface says about itself")

os.environ.pop("CLAUDE_BLOCKERS_SURFACE", None)
check("no session and no declaration is 'unknown'", context.surface() == "unknown")

os.environ["CLAUDE_CODE_SESSION_ID"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
check("a session id alone means Claude Code", context.surface() == context.CLAUDE_CODE)
check("and it has a working directory", context.has_working_directory())
del os.environ["CLAUDE_CODE_SESSION_ID"]

os.environ["CLAUDE_BLOCKERS_SURFACE"] = context.CLAUDE_DESKTOP
check("a declaration is taken at its word", context.surface() == context.CLAUDE_DESKTOP)
check("Desktop has no working directory", not context.has_working_directory())

# --------------------------------------------------------------------------- #
print("\nWhat a Desktop blocker is stamped with")
# The regression this file exists for: describe() must not reach for os.getcwd()
# here. The test runner's cwd is this repo, which is a git checkout -- so if
# describe() guesses, it guesses "claude-blockers", and these catch it.

origin = context.describe()
check("no directory is invented", origin["cwd"] == "", f"got {origin['cwd']!r}")
check("no repository is invented", origin["repo"] is None, f"got {origin['repo']!r}")
check("no branch is invented", origin["branch"] is None, f"got {origin['branch']!r}")
check("no session is invented", origin["session_id"] is None)
check("no transcript is invented", origin["transcript_path"] is None)
check("no pid is invented", origin["claude_pid"] is None)
check("the project names the app, not the folder",
      origin["project"] == context.DESKTOP_PROJECT, f"got {origin['project']!r}")
check("the surface is recorded", origin["surface"] == context.CLAUDE_DESKTOP)

named = context.describe(project="wii")
check("an explicit project is kept", named["project"] == "wii")

# An explicit cwd is the one way a Desktop chat can file against real work.
here = str(Path(__file__).resolve().parent.parent)
located = context.describe(explicit_cwd=here)
check("an explicit directory is honoured", located["cwd"] == str(Path(here)))
check("and its repository is read", located["repo"] == "claude-blockers", f"got {located}")

# --------------------------------------------------------------------------- #
print("\nThe surface column")

db.init()
desktop_id = db.create_blocker(
    title="from the app", detail=None, how_to=None, project="wii", cwd="",
    surface=context.CLAUDE_DESKTOP, host="testbox",
)
code_id = db.create_blocker(
    title="from the cli", detail=None, how_to=None, project="wii", cwd="/src/wii",
    surface=context.CLAUDE_CODE, host="testbox",
)
check("it round-trips", db.get_blocker(desktop_id)["surface"] == context.CLAUDE_DESKTOP)

ids = [r["id"] for r in db.list_blockers(surface=context.CLAUDE_DESKTOP)]
check("filtering by surface finds it", desktop_id in ids, f"got {ids}")
check("and excludes the other surface", code_id not in ids, f"got {ids}")

rows = db.list_blockers(surface=context.CLAUDE_DESKTOP, host="nowhere")
check("the host narrows it further", not rows, f"got {rows}")

# --------------------------------------------------------------------------- #
print("\nUpgrading a board that predates the column")
# A board that has been collecting blockers for weeks is upgraded in place, not
# rebuilt. Simulated by dropping the column back off and re-running init().

legacy = TMP / "legacy"
legacy.mkdir()
os.environ["CLAUDE_BLOCKERS_HOME"] = str(legacy)
db.init()
with db.connect() as conn:
    conn.execute("ALTER TABLE blockers DROP COLUMN surface")
    conn.execute(
        "INSERT INTO blockers (created_at, updated_at, status, kind, urgency, "
        "title, project, cwd) VALUES ('t','t','open','blocker','normal','old','p','/w')"
    )
db.init()  # the migration runs again over the existing file
old = db.list_blockers()
check("the old row survived the migration", len(old) == 1, f"got {old}")
check("and reads as no surface recorded", old[0]["surface"] is None, f"got {old[0]}")
os.environ["CLAUDE_BLOCKERS_HOME"] = str(HOME)

print(f"\n{BAR}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{BAR}\n")
sys.exit(1 if FAIL else 0)
