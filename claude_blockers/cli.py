"""Command line surface.

    claude-blockers serve       run the board UI
    claude-blockers install     wire into Claude Code globally (all projects)
    claude-blockers uninstall   remove that wiring
    claude-blockers status      what is configured and what is pending
    claude-blockers show 42     print blocker #42 in full
    claude-blockers answer 42   record the decision on blocker #42
    claude-blockers edit 42     revise blocker #42's text
    claude-blockers delete 42   erase blocker #42 for good
    claude-blockers demo        insert sample blockers so you can see the UI work
    claude-blockers mcp         stdio MCP server (Claude Code runs this, not you)
    claude-blockers hook        hook receiver  (Claude Code runs this, not you)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__, backend, config, context, guidance

MARK_START = "<!-- claude-blockers:start -->"
MARK_END = "<!-- claude-blockers:end -->"

# Traces of the old name, cleaned up on install and uninstall. A hook left
# pointing at the vanished blocker_board module would fail on every session
# event, so removing these is not cosmetic.
LEGACY_MARK_START = "<!-- blocker-board:start -->"
LEGACY_MARK_END = "<!-- blocker-board:end -->"
LEGACY_MCP_NAME = "blocker-board"
LEGACY_MODULE = "blocker_board"

# The body lives in guidance.py, because the MCP server declares the same text
# to clients that never read CLAUDE.md. Two copies drifted once already.
GUIDANCE = guidance.for_claude_md(MARK_START, MARK_END)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _claude_exe() -> str | None:
    return shutil.which("claude")


def _hook_command() -> str:
    return f'"{sys.executable}" -m claude_blockers hook'


def _hook_interpreter(command: str) -> str:
    """The interpreter a wired hook runs, read back out of its command line."""
    command = command.strip()
    if command.startswith('"'):
        return command[1:].partition('"')[0]
    return command.partition(" ")[0]


class ConfigNotJSON(Exception):
    """A config file is there but cannot be parsed, so it must not be rewritten.

    This used to return `{}` and carry on, which meant `install` and `uninstall`
    wrote that empty object back over the file. A settings.json with a `//`
    comment or a trailing comma in it -- both ordinary results of editing one by
    hand -- came out the far side containing nothing but our hooks, and the
    permissions, model and statusLine that had been in it were gone. `uninstall`
    did it without even taking a backup, and both printed success.

    A file we cannot read is not a file we know how to edit. Everything that
    would write now says so and leaves it alone instead.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} is not valid JSON ({reason})")
        self.path = path
        self.reason = reason


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ConfigNotJSON(path, str(exc)) from exc


def _backup(path: Path) -> Path | None:
    """Copy a config aside before editing it, once.

    Re-running install is the documented way to upgrade, so this gets called
    again on a file that already carries our changes. Overwriting the backup then
    would replace the pristine copy with a modified one -- and the only moment
    the backup is worth anything is when someone wants the pristine copy back.
    """
    if not path.is_file():
        return None
    backup = path.with_suffix(path.suffix + ".claude-blockers.bak")
    if backup.exists():
        return None
    shutil.copy2(path, backup)
    return backup


HOOK_EVENTS = ["SessionStart", "Notification", "UserPromptSubmit", "Stop", "SessionEnd"]


# --------------------------------------------------------------------------- #
# Claude Desktop
# --------------------------------------------------------------------------- #
# The Desktop app speaks the same stdio MCP protocol as Claude Code, so the
# server needs no changes -- only an entry in a different config file. What it
# does not give us is a session: no CLAUDE_CODE_SESSION_ID, no pid, and a working
# directory that is wherever the app launched rather than wherever the work is.
# CLAUDE_BLOCKERS_SURFACE is how the server finds that out, rather than inferring
# it from a missing variable that a hand-run server would also be missing.
MCP_KEY = "claude-blockers"


def _desktop_entry(home: Path) -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "claude_blockers", "mcp"],
        # Deliberately never CLAUDE_BLOCKERS_URL/_TOKEN, even on a --remote
        # install. --remote exists for sandboxes that cannot reach the host's
        # database; Desktop runs on the host, where the database already is.
        "env": {
            "CLAUDE_BLOCKERS_HOME": str(home),
            "CLAUDE_BLOCKERS_SURFACE": context.CLAUDE_DESKTOP,
        },
    }


def _write_desktop_config(path: Path, config_obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config_obj, indent=2) + "\n", encoding="utf-8")


def _install_desktop(home: Path) -> list[str]:
    """Merge our server into Claude Desktop's config, leaving everything else."""
    path = config.desktop_config_path()
    notes = []
    _load_json(path)  # refuse early, before a backup implies we are committed
    backup = _backup(path)
    if backup:
        notes.append(f"backed up {path.name} -> {backup.name}")

    existing = _load_json(path)
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    # The old name, same as the Claude Code path does -- two entries would start
    # two servers on one database and double every tool in the app's picker.
    servers.pop(LEGACY_MCP_NAME, None)
    servers[MCP_KEY] = _desktop_entry(home)
    existing["mcpServers"] = servers
    _write_desktop_config(path, existing)

    notes.append(f"wired Claude Desktop ({path})")
    if config.desktop_running():
        # Writing this file under a running app is not an upgrade that takes
        # effect on restart -- it is a write that gets thrown away. See
        # config.desktop_running() for what the app does to the file.
        notes.append("! Claude Desktop is RUNNING, and will discard what was just written.")
        notes.append("  It holds this file in memory and saves the whole thing back over")
        notes.append("  itself, so an entry added underneath it does not survive. Do this:")
        notes.append("    1. quit the app fully -- tray icon -> Quit; closing the window")
        notes.append("       leaves it running")
        notes.append("    2. run `claude-blockers install` again")
        notes.append("    3. reopen the app")
    else:
        notes.append("start Claude Desktop -- it reads this file once, at launch")
    return notes


def _remove_desktop(path: Path) -> str:
    if not path.is_file():
        return "Claude Desktop was not wired"
    try:
        existing = _load_json(path)
    except ConfigNotJSON as exc:
        # The app owns this file. Not being able to read it is a reason to keep
        # our hands off it, not a reason to report it as already clean.
        return (f"! left Claude Desktop's config alone -- {exc.reason}.\n"
                f'    Remove the "{MCP_KEY}" entry from {path} by hand.')
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict) or not any(
        k in servers for k in (MCP_KEY, LEGACY_MCP_NAME)
    ):
        return "Claude Desktop was not wired"
    _backup(path)
    for key in (MCP_KEY, LEGACY_MCP_NAME):
        servers.pop(key, None)
    # Leave the (possibly now empty) mcpServers object in place. Removing a key
    # the app wrote is not ours to do, and an empty object is what it starts with.
    existing["mcpServers"] = servers
    _write_desktop_config(path, existing)
    return f"removed our entry from Claude Desktop ({path})"


def _split_brain_warning() -> list[str]:
    """A WSL session keeping its own database, which is nobody's board.

    Nothing about this looks broken from the inside. The server starts, cards
    are written, ids come back -- into a SQLite file on the distro's own disk,
    while the board being watched is a different file over on Windows. It took
    three databases and a card numbered #169 landing on a board whose highest id
    was 15 before anyone noticed, because every individual piece was working.

    The condition is exact rather than a guess: inside WSL, with no relay
    configured, there is no arrangement in which the host's board is the one
    being written to. Plain Linux is not warned, because there is no other side.
    """
    if not config.is_wsl() or backend.is_remote():
        return []
    distro = config.wsl_distro() or "this distro"
    return [
        "! this is WSL, and blockers are going into a database inside "
        f"{distro}:",
        f"    {config.db_path()}",
        "  A board opened on Windows reads a different file and will never show "
        "them.",
        "  Point this side at the host's board instead -- on Windows:",
        "      claude-blockers serve --host 0.0.0.0",
        "  then in here, with the token that printed:",
        "      claude-blockers install --remote http://$(hostname).mshome.net:4317/ "
        "--token=<token>",
        "  ($(hostname).mshome.net is the host, and unlike the gateway address it "
        "survives a reboot.)",
    ]


def _desktop_status() -> list[str]:
    path = config.desktop_config_path()
    if not config.desktop_installed() and not path.is_file():
        return ["not installed on this machine"]
    try:
        servers = _load_json(path).get("mcpServers")
    except ConfigNotJSON as exc:
        return [f"cannot tell -- {path.name} is not valid JSON ({exc.reason})"]
    entry = servers.get(MCP_KEY) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        lines = ["installed, but not wired -- run `claude-blockers install`"]
        if config.desktop_running():
            # The likeliest reason a wired install reads as unwired later: the
            # app was up when it was written, and has since saved over it.
            lines.append("! quit Claude Desktop before running that, or it will")
            lines.append("  overwrite the entry the way it may already have done")
        return lines
    lines = ["wired"]
    command = entry.get("command")
    # Same failure the hooks have: an install whose interpreter was removed or
    # relocated leaves an entry that fails every time the app starts it, and the
    # app's error names the path without saying what put it there.
    if isinstance(command, str) and command and not Path(command).exists():
        lines.append(f"! its interpreter is not there: {command}")
        lines.append("  the app's tools will fail until you re-run `claude-blockers install`")
    elif isinstance(command, str) and command and Path(command) != Path(sys.executable):
        lines.append(f"wired to another install: {command}")
    return lines


def _install_hooks(settings_path: Path, home: Path,
                   remote: str = "", token: str = "") -> list[str]:
    notes: list[str] = []
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    command = _hook_command()

    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        already = any(
            h.get("command") == command
            for entry in entries
            for h in entry.get("hooks", [])
        )
        if already:
            continue
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        notes.append(f"hook: {event}")

    # Making the database location part of the session environment means both the
    # hooks and the MCP server agree on it without either being told twice.
    env = settings.setdefault("env", {})
    if env.get("CLAUDE_BLOCKERS_HOME") != str(home):
        env["CLAUDE_BLOCKERS_HOME"] = str(home)
        notes.append(f"env: CLAUDE_BLOCKERS_HOME={home}")

    # Same reasoning for a sandboxed install: the hooks, the MCP server and a
    # `claude-blockers` you run by hand in here all have to reach the same board,
    # and that board is somewhere else.
    # Only ever set these, never clear them. Re-running `install` is the
    # documented way to upgrade, and a bare re-run says nothing about the relay
    # -- silently reverting a working sandbox to a local database because the
    # user did not repeat --remote would be a very quiet way to lose their
    # blockers. `uninstall` is what removes them.
    for var, value in (("CLAUDE_BLOCKERS_URL", remote), ("CLAUDE_BLOCKERS_TOKEN", token)):
        if value and env.get(var) != value:
            env[var] = value
            notes.append(f"env: {var}=" + (value if var.endswith("URL") else "(set)"))

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return notes


def _is_ours(command: str) -> bool:
    """Does this hook command belong to us, under either name?"""
    return "claude_blockers hook" in command or f"{LEGACY_MODULE} hook" in command


def _wired_hooks(settings: dict) -> dict[str, list[str]]:
    """Our hook commands in this settings file, keyed by event.

    Deliberately not filtered down to the command this install would write: a
    hook naming an interpreter we no longer have is the thing worth reporting,
    and matching on the exact string is how it stayed invisible.
    """
    found: dict[str, list[str]] = {}
    for event, entries in (settings.get("hooks") or {}).items():
        commands = [
            h.get("command", "")
            for entry in entries
            for h in entry.get("hooks", [])
            if _is_ours(h.get("command", ""))
        ]
        if commands:
            found[event] = commands
    return found


def _foreign_interpreters(settings: dict) -> list[str]:
    """Interpreters our wired hooks name, other than the one running now."""
    return sorted({
        _hook_interpreter(command)
        for commands in _wired_hooks(settings).values()
        for command in commands
        if command != _hook_command()
    } - {""})


def _remove_hooks(settings_path: Path, keep_current: bool = False) -> list[str]:
    """Strip our hooks from settings.

    With keep_current set, only entries from the old name are removed -- that is
    the upgrade path, where the freshly written hooks must survive.
    """
    notes: list[str] = []
    settings = _load_json(settings_path)
    hooks = settings.get("hooks", {})
    current = _hook_command()

    def drop(entry_command: str) -> bool:
        if not _is_ours(entry_command):
            return False
        if keep_current and entry_command == current:
            return False
        return True

    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            inner = [h for h in entry.get("hooks", []) if not drop(h.get("command", ""))]
            if len(inner) != len(entry.get("hooks", [])):
                notes.append(f"removed hook: {event}")
            if inner:
                kept.append({**entry, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        settings.pop("hooks", None)

    env = settings.get("env", {})
    # Uninstalling has to take the relay settings with it. Leaving a stale
    # CLAUDE_BLOCKERS_URL behind would point every future session at a board that
    # may no longer be listening, and leaving the token behind writes a secret
    # into a config file nothing reads any more.
    removable = (["BLOCKER_BOARD_HOME"] if keep_current
                 else ["CLAUDE_BLOCKERS_HOME", "BLOCKER_BOARD_HOME",
                       "CLAUDE_BLOCKERS_URL", "CLAUDE_BLOCKERS_TOKEN"])
    for var in removable:
        if var in env:
            env.pop(var)
            notes.append(f"removed env: {var}")
    if not env:
        settings.pop("env", None)

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return notes


def _strip_block(text: str, start: str, end: str) -> str:
    if start not in text:
        return text
    head, _, rest = text.partition(start)
    _, _, tail = rest.partition(end)
    return head + tail


def _install_guidance(memory_path: Path) -> str:
    existing = memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""
    # A block left over from the old name would otherwise sit alongside the new
    # one, telling Claude about an MCP server that no longer answers.
    existing = _strip_block(existing, LEGACY_MARK_START, LEGACY_MARK_END).rstrip() + "\n"
    if existing.strip() == "":
        existing = ""
    if MARK_START in existing:
        head, _, rest = existing.partition(MARK_START)
        _, _, tail = rest.partition(MARK_END)
        updated = f"{head}{GUIDANCE}{tail}"
        action = "updated"
    else:
        joiner = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        updated = f"{existing}{joiner}{GUIDANCE}\n"
        action = "added"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(updated, encoding="utf-8")
    return action


def _remove_guidance(memory_path: Path) -> bool:
    if not memory_path.is_file():
        return False
    existing = memory_path.read_text(encoding="utf-8")
    if MARK_START not in existing and LEGACY_MARK_START not in existing:
        return False
    updated = _strip_block(existing, MARK_START, MARK_END)
    updated = _strip_block(updated, LEGACY_MARK_START, LEGACY_MARK_END)
    memory_path.write_text(updated.strip() + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def _refuse_unreadable(path: Path) -> int | None:
    """Stop before touching a config we cannot parse. Returns an exit code.

    Checked up front rather than at the write, so nothing has been changed and
    nothing has been half-changed by the time we say so.
    """
    try:
        _load_json(path)
    except ConfigNotJSON as exc:
        print(f"! {exc.path} is not valid JSON ({exc.reason}),")
        print("  so nothing here has been changed -- editing it would mean")
        print("  writing over whatever else is in it.")
        print("  A trailing comma or a // comment is the usual cause. Fix that")
        print("  and run this again.")
        return 1
    return None


def cmd_install(args: argparse.Namespace) -> int:
    bad = _refuse_unreadable(config.claude_dir() / "settings.json")
    if bad is not None:
        return bad
    remote = (getattr(args, "remote", None) or "").strip().rstrip("/")
    if remote:
        remote += "/"
    token = (getattr(args, "token", None) or "").strip()
    if token and not remote:
        # Rotating a token is a legitimate reason to run install again, so keep
        # it if this machine is already pointed at a board.
        remote = (config.remote_url() or "").strip()
        if not remote:
            print("  ! --token only means something with --remote; ignoring it.")
            token = ""

    home = Path(args.home).expanduser().resolve() if args.home else config.home()
    home.mkdir(parents=True, exist_ok=True)
    claude_dir = config.claude_dir()
    settings_path = claude_dir / "settings.json"
    memory_path = claude_dir / "CLAUDE.md"

    print(f"claude-blockers {__version__}")
    print(f"  python:   {sys.executable}")
    if remote:
        print(f"  board:    {remote} (over HTTP -- no database is kept here)")
        if not token:
            print("  ! no --token given. The board will refuse this session unless it")
            print("    is bound to loopback and you are on the same machine.")
    else:
        print(f"  database: {home / 'blockers.db'}")
    print()

    for path in (settings_path, memory_path):
        backup = _backup(path)
        if backup:
            print(f"  backed up {path.name} -> {backup.name}")

    # 1. MCP server, user scope, so every project gets it with no per-repo setup.
    exe = _claude_exe()
    if exe:
        for name in (LEGACY_MCP_NAME, "claude-blockers"):
            subprocess.run([exe, "mcp", "remove", "-s", "user", name],
                           capture_output=True, text=True)
        env_flags: list[str] = ["-e", f"CLAUDE_BLOCKERS_HOME={home}"]
        if remote:
            env_flags += ["-e", f"CLAUDE_BLOCKERS_URL={remote}"]
            if token:
                env_flags += ["-e", f"CLAUDE_BLOCKERS_TOKEN={token}"]
        result = subprocess.run(
            [exe, "mcp", "add", "-s", "user", "claude-blockers", *env_flags,
             "--", sys.executable, "-m", "claude_blockers", "mcp"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("  registered MCP server (user scope)")
        else:
            print(f"  ! MCP registration failed: {(result.stderr or result.stdout).strip()}")
            print(f"    run manually: claude mcp add -s user claude-blockers -- "
                  f'"{sys.executable}" -m claude_blockers mcp')
    else:
        print("  ! 'claude' not on PATH -- register the MCP server manually:")
        print(f'    claude mcp add -s user claude-blockers -- "{sys.executable}" -m claude_blockers mcp')

    # 1b. Claude Desktop, if it is here. Same server, same database, same board --
    # only a different config file to be named in. Nothing is exposed off this
    # machine by doing so: Desktop opens the SQLite file directly, exactly as a
    # Claude Code session on this host does.
    want_desktop = getattr(args, "desktop", False)
    if getattr(args, "no_desktop", False):
        print("  skipped Claude Desktop (--no-desktop)")
    elif remote:
        # A --remote install is running inside a sandbox. Claude Desktop is a
        # host application; there is nothing here for it to be wired into.
        print("  skipped Claude Desktop (this is a --remote install, so it runs in a sandbox)")
    elif config.desktop_installed() or want_desktop:
        try:
            for note in _install_desktop(home):
                print(f"  {note}")
        except OSError as exc:
            print(f"  ! could not write Claude Desktop's config: {exc}")
            print(f"    add this yourself to {config.desktop_config_path()}:")
            print(f"      {json.dumps({MCP_KEY: _desktop_entry(home)}, indent=6)}")
    else:
        print("  no Claude Desktop found (use --desktop to wire it anyway)")

    # Installing inside WSL without a relay is the arrangement that silently
    # builds a second board. Say so while the person is still standing here,
    # rather than leaving it for `status` to be asked later.
    for line in _split_brain_warning():
        print(f"  {line}")

    # 2. Hooks, so stalled sessions get captured even without Claude's help.
    # Name whatever was wired before as it goes: a hook pointing at a vanished
    # interpreter fails on every session event, and the error Claude Code shows
    # names the path without saying which install put it there.
    replacing = _foreign_interpreters(_load_json(settings_path))
    for note in _install_hooks(settings_path, home, remote, token):
        print(f"  {note}")
    # Anything still pointing at the old module would fail on every session
    # event now that the module is gone.
    if _remove_hooks(settings_path, keep_current=True):
        for old in replacing:
            print(f"  replaced hooks that ran {old}")
        if not replacing:
            print("  removed hooks from the previous name")

    # 3. Guidance, so Claude knows the tool exists and when to reach for it.
    print(f"  {_install_guidance(memory_path)} guidance in {memory_path}")

    backend.init()
    print(f"\nInstalled. Restart any running Claude Code sessions to pick this up.")
    print(f"Then run:  claude-blockers serve")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    claude_dir = config.claude_dir()
    bad = _refuse_unreadable(claude_dir / "settings.json")
    if bad is not None:
        return bad
    # Resolve the board *before* the wiring goes, not after. settings.json is
    # one of the places config.home() looks, so once the env block is removed
    # this would re-resolve to the default and name a path that was never the
    # user's board -- at the one moment they are most likely to act on it.
    board = config.db_path()
    exe = _claude_exe()
    if exe:
        removed = [
            name for name in (LEGACY_MCP_NAME, "claude-blockers")
            if subprocess.run([exe, "mcp", "remove", "-s", "user", name],
                              capture_output=True, text=True).returncode == 0
        ]
        print(f"  removed MCP server ({', '.join(removed)})" if removed
              else "  MCP server was not registered")
    print(f"  {_remove_desktop(config.desktop_config_path())}")
    for note in _remove_hooks(claude_dir / "settings.json"):
        print(f"  {note}")
    print("  removed guidance" if _remove_guidance(claude_dir / "CLAUDE.md")
          else "  no guidance block found")
    print(f"\nUninstalled. Your database is untouched at {board}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    claude_dir = config.claude_dir()
    # status only reads, so it reports the problem and carries on rather than
    # refusing -- the blocker counts below are still worth showing.
    unreadable = ""
    try:
        settings = _load_json(claude_dir / "settings.json")
    except ConfigNotJSON as exc:
        settings, unreadable = {}, exc.reason
    wired = [event for event in HOOK_EVENTS if event in _wired_hooks(settings)]
    memory = claude_dir / "CLAUDE.md"
    guidance = memory.is_file() and MARK_START in memory.read_text(encoding="utf-8")

    backend.init()
    s = backend.stats()

    print(f"claude-blockers {__version__}")
    # Say which board these numbers came from. Printing a local database path
    # beside a remote board url, as this used to, invites you to read one and
    # believe the other.
    if backend.is_remote():
        print(f"  board:     {config.remote_url()} (over HTTP; no database here)")
    else:
        print(f"  database:  {config.db_path()}")
    print(f"  board url: {config.board_url()}")
    for line in _split_brain_warning():
        print(f"  {line}")
    if unreadable:
        print(f"  hooks:     unknown -- settings.json is not valid JSON ({unreadable})")
        print("             nothing can be read from it, and install will refuse")
        print("             to edit it until that is fixed")
    else:
        print(f"  hooks:     {', '.join(wired) if wired else 'none configured'}")
    for other in _foreign_interpreters(settings):
        if Path(other).exists():
            print(f"             wired to another install: {other}")
        else:
            print(f"             ! wired to an interpreter that is not there: {other}")
            print("               every session event fails until you re-run "
                  "`claude-blockers install`")
    desktop = _desktop_status()
    print(f"  desktop:   {desktop[0]}")
    for extra in desktop[1:]:
        print(f"             {extra}")
    print(f"  guidance:  {'installed' if guidance else 'not installed'}")
    print(f"  blockers:  {s['open']} open ({s['high']} high, {s['unseen']} unseen), {s['total']} total")
    for p in backend.projects():
        if not p["open_count"]:
            continue
        print(f"    {p['project']}: {p['open_count']} open")
        for b in backend.list_blockers(status="open", project=p["project"]):
            print(f"      #{b['id']}  {b['title']}")
    if s["open"]:
        print("\n  `claude-blockers show <id>` prints one in full.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print one blocker in full, by id.

    The terminal twin of the read_blocker MCP tool: same text, so the number on
    a card is enough to pick the work up from anywhere.
    """
    from . import render

    backend.init()
    blocker = backend.get_blocker(args.id)
    if blocker is None:
        print(f"No blocker #{args.id}. Run `claude-blockers status` to see what exists.")
        return 1
    print(render.blocker_card(blocker, board_url=config.board_url()), end="")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    backend.init()
    blocker = backend.get_blocker(args.id)
    if blocker is None:
        print(f"No blocker #{args.id}.")
        return 1
    answer = " ".join(args.text).strip()
    if not answer:
        print("Nothing to record -- pass the decision as an argument.")
        return 1
    backend.record_answer(args.id, answer, answered_by="user",
                     resolve=not args.keep_open, resolution="Answered by you")
    print(f"Recorded on #{args.id}: {blocker['title']}")
    print("  still open" if args.keep_open else "  closed")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Revise a blocker's text, the terminal twin of the update_blocker tool."""
    backend.init()
    blocker = backend.get_blocker(args.id)
    if blocker is None:
        print(f"No blocker #{args.id}.")
        return 1
    changed = backend.update_blocker(
        args.id, title=args.title, detail=args.detail, how_to=args.how_to,
        urgency=args.urgency, kind=args.kind, project=args.project,
    )
    if not changed:
        print("Nothing to change -- pass at least one of --title, --detail, "
              "--how-to, --urgency, --kind, --project.")
        return 1
    print(f"Updated {', '.join(changed)} on #{args.id}: {backend.get_blocker(args.id)['title']}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Erase one blocker. Unlike the MCP tool this deletes answered cards too --
    it is your own board and you are naming the number by hand."""
    backend.init()
    blocker = backend.get_blocker(args.id)
    if blocker is None:
        print(f"No blocker #{args.id}.")
        return 1
    backend.delete_blocker(args.id)
    print(f"Deleted #{args.id}: {blocker['title']}")
    return 0


def cmd_demo(_: argparse.Namespace) -> int:
    backend.init()
    samples = [
        dict(
            title="Test mic capture on the USB interface",
            detail="The recorder now writes 48kHz WAV, but I cannot verify the "
                   "hardware path from here. Everything downstream is written and "
                   "unit-tested; this is the only unverified link.",
            how_to="1. Plug in the USB audio interface.\n"
                   "2. Run `python -m recorder.check --seconds 5`\n"
                   "3. It should print a peak level between -18 and -3 dBFS.\n"
                   "   Silence (-inf) means the wrong input device was picked.",
            urgency="high", kind="blocker", project="acme-audio",
            cwd="/home/you/code/acme-audio",
        ),
        dict(
            title="Decide: keep fp16 or move to int8 for the English model",
            detail="fp16 fits in VRAM with 1.2GB spare. int8 would free 3GB and let "
                   "us batch 4x, but I measured a small quality drop on sibilants.",
            how_to="Listen to `samples/fp16_vs_int8.html` (both clips, same text) "
                   "and reply with which one you can live with.",
            urgency="normal", kind="question", project="acme-audio",
            cwd="/home/you/code/acme-audio",
        ),
        dict(
            title="Review the new landing page copy before I wire it up",
            detail="Draft is in `src/content/landing.mdx`. I did not touch pricing "
                   "claims since those need your sign-off.",
            how_to="Open the file, or run `npm run dev` and visit "
                   "http://localhost:5173. Edit directly if you want changes.",
            urgency="low", kind="review", project="acme-site",
            cwd="/home/you/code/acme-site",
        ),
    ]
    # Two closed rows, so the demo board can actually show the Resolved and
    # Dismissed tabs -- and the "clear past blockers" button that empties them.
    closed = [
        (dict(
            title="Confirm the staging deploy went out",
            detail="Waited on the pipeline finishing before I could smoke-test it.",
            how_to="Open https://staging.example.test and check the footer build hash.",
            urgency="normal", kind="review", project="acme-site",
            cwd="/home/you/code/acme-site",
        ), "resolved", "Deployed and checked."),
        (dict(
            title="Pick a name for the CLI entry point",
            detail="Went with the obvious one in the end, so this stopped mattering.",
            how_to="Reply with the name you want on PATH.",
            urgency="low", kind="question", project="acme-audio",
            cwd="/home/you/code/acme-audio",
        ), "dismissed", "Not worth a decision."),
    ]

    for sample in samples:
        blocker_id = backend.create_blocker(source="mcp", session_id=None,
                                       transcript_path=None, claude_pid=None, **sample)
        print(f"  #{blocker_id}  {sample['project']}: {sample['title']}")
    for sample, status, resolution in closed:
        blocker_id = backend.create_blocker(source="mcp", session_id=None,
                                       transcript_path=None, claude_pid=None, **sample)
        backend.set_status(blocker_id, status, resolution)
        print(f"  #{blocker_id}  {sample['project']}: {sample['title']} ({status})")
    print(f"\nSeeded {len(samples) + len(closed)} demo blockers. "
          "Run `claude-blockers serve` to view.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import web
    web.serve(port=args.port, open_browser=not args.no_open, host=args.host)
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from . import mcp_server
    mcp_server.main()
    return 0


def cmd_hook(_: argparse.Namespace) -> int:
    from . import hook
    return hook.main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-blockers",
        description="A local board for everything your Claude Code sessions need you to do.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the board UI")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="address to bind (use 0.0.0.0 to reach it from outside WSL)")
    p_serve.add_argument("--no-open", action="store_true", help="do not open a browser")
    p_serve.set_defaults(func=cmd_serve)

    p_install = sub.add_parser("install", help="wire into Claude Code and Claude Desktop globally")
    p_install.add_argument("--home", default=None, help="where to keep the database")
    p_install.add_argument("--remote", default=None, metavar="URL",
                           help="post to a board running elsewhere over HTTP instead of "
                                "opening a database here -- use this inside WSL, a "
                                "devcontainer or Docker, where the host's SQLite file "
                                "cannot be shared safely")
    p_install.add_argument("--token", default=None,
                           help="token the remote board printed when it started")
    p_install.add_argument("--desktop", action="store_true",
                           help="wire the Claude Desktop app even if it was not detected")
    p_install.add_argument("--no-desktop", action="store_true",
                           help="leave the Claude Desktop app alone")
    p_install.set_defaults(func=cmd_install)

    p_show = sub.add_parser("show", help="print one blocker in full, by id")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_answer = sub.add_parser("answer", help="record the decision on a blocker")
    p_answer.add_argument("id", type=int)
    p_answer.add_argument("text", nargs="+", help="what was decided")
    p_answer.add_argument("--keep-open", action="store_true",
                          help="record the answer but leave the blocker open")
    p_answer.set_defaults(func=cmd_answer)

    p_edit = sub.add_parser("edit", help="revise a blocker's text")
    p_edit.add_argument("id", type=int)
    p_edit.add_argument("--title")
    p_edit.add_argument("--detail")
    p_edit.add_argument("--how-to", dest="how_to")
    p_edit.add_argument("--urgency", choices=["low", "normal", "high"])
    p_edit.add_argument("--kind", choices=["blocker", "question", "review"])
    p_edit.add_argument("--project")
    p_edit.set_defaults(func=cmd_edit)

    p_delete = sub.add_parser("delete", help="erase a blocker for good")
    p_delete.add_argument("id", type=int)
    p_delete.set_defaults(func=cmd_delete)

    sub.add_parser("uninstall", help="remove the wiring").set_defaults(func=cmd_uninstall)
    sub.add_parser("status", help="show configuration and pending blockers").set_defaults(func=cmd_status)
    sub.add_parser("demo", help="insert sample blockers").set_defaults(func=cmd_demo)
    sub.add_parser("mcp", help="stdio MCP server (run by Claude Code)").set_defaults(func=cmd_mcp)
    sub.add_parser("hook", help="hook receiver (run by Claude Code)").set_defaults(func=cmd_hook)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except backend.RemoteError as exc:
        # These messages are written to be read -- they name the board, the OS
        # reason, and the command to run on the other machine. Twenty lines of
        # traceback above one is how you get someone to stop reading it. The
        # host board not being started yet is the most ordinary WSL failure
        # there is, and it is not a crash.
        print(f"! {exc}")
        return 1
    except sqlite3.Error as exc:
        print(f"! could not use the board database at {config.db_path()}: {exc}")
        return 1
    except ConfigNotJSON as exc:
        print(f"! {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
