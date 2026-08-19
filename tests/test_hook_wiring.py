"""What the hook wiring in settings.json says, and what we say about it.

    uv run python tests/test_hook_wiring.py

Works on a temporary settings.json, so it never reads or writes the real one.

WHAT WENT WRONG. The hooks are written with the absolute path of whichever
interpreter ran `install`. That path is not guaranteed to keep working: this repo
sits on a drive both Windows and WSL can see, so a `uv sync` from the other side
rebuilds `.venv` for the other platform and the interpreter the hooks name stops
existing. Claude Code then failed on every session event --

    SessionStart:startup hook error
    ...\\.venv\\Scripts\\python.exe: No such file or directory

-- and `claude-blockers status` said "hooks: none configured", because it only
counted hooks whose command matched the interpreter running status, character for
character. The one piece of state that explained the errors was the one thing
status hid. So the reporting is by module now, not by exact command, and an
interpreter that is not there is called out with the fix.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="claude-blockers-wiring-"))
os.environ["CLAUDE_BLOCKERS_HOME"] = str(TMP / "home")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_blockers import backend, cli, config  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


SETTINGS = TMP / "claude" / "settings.json"
HOME = TMP / "home"

# The interpreter a WSL `uv sync` leaves behind, from Windows' point of view: the
# hooks still name it, nothing is there to run.
GONE = str(TMP / "phantom" / "Scripts" / "python.exe")


def read() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def commands(settings: dict) -> list[str]:
    return [c for cs in cli._wired_hooks(settings).values() for c in cs]


print("\nReading an interpreter back out of a hook command")

check("quoted path with spaces survives the round trip",
      cli._hook_interpreter('"C:\\Program Files\\py\\python.exe" -m claude_blockers hook')
      == "C:\\Program Files\\py\\python.exe")
check("an unquoted path works too",
      cli._hook_interpreter("/usr/bin/python3 -m claude_blockers hook") == "/usr/bin/python3")
check("our own command reports our own interpreter",
      cli._hook_interpreter(cli._hook_command()) == sys.executable)

print("\nA fresh install")

notes = cli._install_hooks(SETTINGS, HOME)
settings = read()
check("every event is wired", sorted(cli._wired_hooks(settings)) == sorted(cli.HOOK_EVENTS),
      f"got {sorted(cli._wired_hooks(settings))}")
check("one hook per event", len(commands(settings)) == len(cli.HOOK_EVENTS),
      f"got {commands(settings)}")
check("nothing looks foreign", cli._foreign_interpreters(settings) == [],
      f"got {cli._foreign_interpreters(settings)}")
check("the database location is exported",
      settings["env"]["CLAUDE_BLOCKERS_HOME"] == str(HOME))

check("installing twice does not stack hooks",
      cli._install_hooks(SETTINGS, HOME) == [] and
      len(commands(read())) == len(cli.HOOK_EVENTS), f"got {commands(read())}")

print("\nAfter the other platform rebuilds the venv")

# Exactly what the machine looked like: our hooks, naming an interpreter that no
# longer exists, with nothing else in the file changed.
settings = read()
for entries in settings["hooks"].values():
    for entry in entries:
        for h in entry["hooks"]:
            h["command"] = f'"{GONE}" -m claude_blockers hook'
SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")

settings = read()
check("the hooks are still recognised as ours",
      sorted(cli._wired_hooks(settings)) == sorted(cli.HOOK_EVENTS),
      f"got {sorted(cli._wired_hooks(settings))}")
check("the dead interpreter is named", cli._foreign_interpreters(settings) == [GONE],
      f"got {cli._foreign_interpreters(settings)}")
check("and it really is not there", not Path(GONE).exists())

print("\nRe-installing over it")

cli._install_hooks(SETTINGS, HOME)
removed = cli._remove_hooks(SETTINGS, keep_current=True)
settings = read()
check("the stale hooks are gone", cli._foreign_interpreters(settings) == [],
      f"got {cli._foreign_interpreters(settings)}")
check("the removal is reported, not silent", removed != [], f"got {removed}")
check("one hook per event again, not two", len(commands(settings)) == len(cli.HOOK_EVENTS),
      f"got {commands(settings)}")
check("every event still fires", sorted(cli._wired_hooks(settings)) == sorted(cli.HOOK_EVENTS),
      f"got {sorted(cli._wired_hooks(settings))}")

print("\nA hook from an install that is still there")

# Same module, different interpreter, and that interpreter exists -- a second
# clone, not a broken one. Worth mentioning, not worth alarming about.
other = sys.executable
settings = read()
settings["hooks"]["SessionStart"].append(
    {"hooks": [{"type": "command", "command": f'"{other}" -m blocker_board hook', "timeout": 10}]})
SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
foreign = cli._foreign_interpreters(read())
check("the other install is listed", foreign == [other], f"got {foreign}")
check("its interpreter is present, so it is not an error", Path(foreign[0]).exists())

print("\nWhich board a command run by hand reads")

# The one that actually happened. settings.json named a board; the shell running
# install did not, because Claude Code exports that variable to the sessions it
# starts and not to your terminal. So the default won: install rewrote the wiring
# to an empty database and left 18 open blockers with nothing pointed at them,
# and reported a normal success. `status` had been quietly reading that same
# wrong board all along.
KEPT = TMP / "chosen-board"
settings = read()
settings["env"]["CLAUDE_BLOCKERS_HOME"] = str(KEPT)
SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")

os.environ.pop("CLAUDE_BLOCKERS_HOME", None)
check("settings.json is read when the environment says nothing",
      Path(config._home_from_settings() or "") == KEPT,
      f"got {config._home_from_settings()}")
check("so that is the board every command uses", config.home() == KEPT,
      f"got {config.home()}")
check("and the database sits inside it", config.db_path() == KEPT / "blockers.db")

os.environ["CLAUDE_BLOCKERS_HOME"] = str(TMP / "from-env")
check("an environment that does say something still wins",
      config.home() == TMP / "from-env", f"got {config.home()}")

os.environ.pop("CLAUDE_BLOCKERS_HOME", None)
settings = read()
settings["env"] = {config._LEGACY_HOME_VAR: str(TMP / "old-name-board")}
SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
check("a board stored under the old variable name is honoured too",
      config.home() == TMP / "old-name-board", f"got {config.home()}")

settings["env"] = {}
SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
check("with nothing named anywhere, the default stands",
      config.home() == Path.home() / ".claude-blockers"
      or config.home() == Path.home() / ".blocker-board",  # the adopted old default
      f"got {config.home()}")

os.environ["CLAUDE_BLOCKERS_HOME"] = str(HOME)
cli._install_hooks(SETTINGS, HOME)  # put the wiring back for the next section

print("\nUninstall")

cli._remove_hooks(SETTINGS)
settings = read()
check("no hooks of ours are left", cli._wired_hooks(settings) == {}, f"got {settings}")
check("the hooks key is dropped entirely", "hooks" not in settings, f"got {settings}")

# --------------------------------------------------------------------------- #
print("\nA settings.json we cannot read")

# The worst bug this file guards. _load_json returned {} on any parse error and
# the caller wrote that back, so a settings.json with a // comment or a trailing
# comma -- both ordinary results of editing one by hand -- came out the far side
# holding nothing but our hooks. uninstall did it without even taking a backup,
# and both printed success. A file we cannot read is not a file we know how to
# edit.
BROKEN = TMP / "broken" / "settings.json"
BROKEN.parent.mkdir(parents=True, exist_ok=True)
ORIGINAL = '{\n  // my notes\n  "permissions": {"allow": ["Bash(ls:*)"]},\n}\n'
BROKEN.write_text(ORIGINAL, encoding="utf-8")

raised = None
try:
    cli._load_json(BROKEN)
except cli.ConfigNotJSON as exc:
    raised = exc
check("reading it raises rather than pretending it is empty", raised is not None)
check("and the complaint names the file", raised is not None and str(BROKEN) in str(raised),
      str(raised))

code = cli._refuse_unreadable(BROKEN)
check("install and uninstall refuse to run against it", code == 1, f"got {code}")
check("and the file is untouched", BROKEN.read_text(encoding="utf-8") == ORIGINAL,
      "this is the whole point -- their permissions and model must survive")
check("with no backup taken, because nothing was changed",
      not BROKEN.with_suffix(".json.claude-blockers.bak").exists())

# A file that is merely absent or empty is not a failure -- that is a first run.
check("a missing file still reads as empty", cli._load_json(TMP / "nope.json") == {})
(TMP / "empty.json").write_text("   \n", encoding="utf-8")
check("and so does an empty one", cli._load_json(TMP / "empty.json") == {})

# --------------------------------------------------------------------------- #
print("\nWhat a sandboxed session may send over the relay")

# The hooks always send transcript_path, and upsert_session forwarded it
# unfiltered -- so the board rejected every SessionStart and Notification from a
# sandboxed session with a 400, the hook swallowed it (a hook must never wedge a
# session), and no card was ever created. Silently, for exactly the sessions the
# relay was built to serve.
sent = {}


def _fake_call(op, **kwargs):
    sent[op] = kwargs
    return None


_real_call, _real_remote = backend._call, backend.is_remote
backend._call, backend.is_remote = _fake_call, lambda: True
backend.upsert_session(session_id="s1", cwd="/w", pid=5,
                       transcript_path="/inside/the/sandbox.jsonl")
backend.create_blocker(title="t", detail=None, how_to=None, project="p", cwd="/w",
                       transcript_path="/inside/the/sandbox.jsonl", claude_pid=7)
backend._call, backend.is_remote = _real_call, _real_remote

for op in ("upsert_session", "create_blocker"):
    check(f"{op} drops a path only the sandbox can open",
          "transcript_path" not in sent.get(op, {}), f"got {sent.get(op)}")
check("create_blocker drops a pid from another process table",
      "claude_pid" not in sent.get("create_blocker", {}), f"got {sent.get('create_blocker')}")
check("but keeps what the board can actually use",
      sent.get("upsert_session", {}).get("cwd") == "/w"
      and sent.get("upsert_session", {}).get("pid") == 5,
      f"got {sent.get('upsert_session')}")

print(f"\n{'=' * 58}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{'=' * 58}\n")
sys.exit(1 if FAIL else 0)
