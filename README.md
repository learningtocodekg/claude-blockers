# Claude Blockers

A local board for everything your Claude Code sessions need **you** to do.

If you run several sessions at once, the bottleneck is rarely Claude's work. It
is that one session stopped two hours ago waiting for you to plug in a device,
paste an API key, or pick between two options — and nothing told you.

Claude Blockers gives every session a way to raise its hand, and gives you one
page showing every raised hand across every project.

```
┌─ Claude Blockers ──────────────────── 3 open · 1 high ─┐
│ ● Test mic capture on the USB interface              │
│   acme-audio · blocker · 4m ago                      │
│ ● Decide: keep fp16 or move to int8                  │
│   acme-audio · question · 20m ago                    │
│ ● Review the landing page copy                       │
│   acme-site · review · 1h ago                        │
└──────────────────────────────────────────────────────┘
```

Everything runs on your machine: one SQLite file, one loopback HTTP server. No
account, no telemetry, no outbound requests.

> **Tested on Windows.** Claude Code in Windows Terminal, the Claude Desktop app,
> and Claude Code inside WSL2 (Ubuntu) all work and are exercised by hand as well
> as by CI. **macOS and plain Linux are untested by a human.** The code paths for
> both exist and CI runs the test suite on `macos-latest` and `ubuntu-latest`, but
> nobody has installed it there and used it for real — expect rough edges, and
> please open an issue rather than assuming it is meant to be that way.

**Jump to:** [Install](#install) · [Commands](#commands) ·
[MCP tools](#the-mcp-tools) · [Claude Desktop](#the-claude-desktop-app) ·
[WSL and containers](#sessions-in-wsl-containers-or-on-another-machine) ·
[Configuration](#configuration) · [Limits](#notes-and-limits)

---

## Install

Requires Python 3.11+. The script installs [uv](https://docs.astral.sh/uv/) if
you do not have it.

```bash
git clone https://github.com/learningtocodekg/claude-blockers
cd claude-blockers
./setup.sh
```

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

That is the whole setup. It installs `claude-blockers` onto your PATH in its own
isolated environment, then wires it into Claude Code:

1. Registers the MCP server at **user scope**, so every project gets it.
2. Adds hooks to `~/.claude/settings.json` (backed up first).
3. Appends a short guidance block to `~/.claude/CLAUDE.md`.
4. Wires the **Claude Desktop app** too, if it is installed.

**Claude Code in a terminal is finished at this point** — nothing else to do,
on Windows Terminal or anywhere else.

**The Desktop app needs one more thing**, which is why it has its own section:
it must be **quit** while `install` runs, or the wiring is silently discarded.
See [Claude Desktop](#the-claude-desktop-app) before you run it.

Steps 2 and 3 are Claude Code's alone either way — Desktop has neither hooks nor
a `CLAUDE.md`, and gets the same guidance over MCP instead.

Restart any running sessions, then open the board:

```bash
claude-blockers serve
```

It serves `http://127.0.0.1:4317` and opens your browser.

<details>
<summary>If something goes wrong</summary>

**`claude-blockers: command not found` right after setup.** uv installs the
command into `~/.local/bin` (`%USERPROFILE%\.local\bin` on Windows), and your
shell read its PATH before that existed. The setup script adds it for new
terminals, so open one. In the current terminal the full path the script printed
still works.

**`uv: command not found`, or the script cannot install uv.** uv is the only
dependency and the script fetches it for you, but a proxy, a TLS-inspecting
firewall, or a locked-down machine can stop that. Install it yourself and re-run:

```powershell
winget install --id=astral-sh.uv -e          # Windows
```
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS / Linux / WSL
```

Or see <https://docs.astral.sh/uv/getting-started/installation/>. Either way,
**open a new terminal afterwards** — the installer changes PATH, and an existing
shell will not see it.

**PowerShell refuses to run the script.** That is the execution policy, not the
script:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

**The board opens someone else's blockers.** You have two boards on one port —
usually one inside WSL, since WSL shares localhost with Windows. `serve` detects
this and suggests a free port.
</details>

`setup.sh` and `setup.ps1` install [uv](https://docs.astral.sh/uv/) by piping
Astral's official installer to a shell if you do not already have it, which is
how uv documents installing itself. Install uv yourself first and the scripts
skip that step entirely.

**Upgrading:** `git pull && ./setup.sh`

On Windows, **stop the board first** (`Ctrl+C` in the terminal running `serve`).
Windows will not let the installer overwrite `claude-blockers.exe` while it is
executing, and `setup.ps1` stops up front and says so rather than failing
mid-install. If you use Claude Desktop, quit it before upgrading too, for the
reason in its section below.

**Uninstalling:**

```bash
claude-blockers uninstall           # remove the Claude Code and Desktop wiring
uv tool uninstall claude-blockers   # remove the command
```

Your database is left alone by both.

---

## Try it first

```bash
claude-blockers demo    # five sample blockers
claude-blockers serve
```

---

## Commands

| Command | Purpose |
| --- | --- |
| `serve` | Run the board. `--port` moves it, `--no-open` skips the browser, `--host` exposes it (see below) |
| `install` | Wire into Claude Code and Claude Desktop. `--home DIR` sets where the database lives; `--desktop`/`--no-desktop` force or skip the app |
| `uninstall` | Remove that wiring |
| `status` | What is configured, and what is pending, with ids |
| `show <id>` | Print one blocker in full |
| `answer <id> <text>` | Record the decision and close it. `--keep-open` records without closing |
| `edit <id>` | `--title/--detail/--how-to/--urgency/--kind/--project` |
| `delete <id>` | Erase one for good |
| `demo` | Insert five sample blockers |

`mcp` and `hook` also exist; Claude Code runs those, not you.

---

## The MCP tools

Claude calls these. You do not need to.

| Tool | What it does |
| --- | --- |
| `raise_blocker(title, detail, how_to, urgency, kind, project, cwd)` | Post something you need done |
| `read_blocker(id)` | Open any blocker by number, from any session |
| `answer_blocker(id, answer, resolve)` | Record a decision onto the card |
| `resolve_blocker(id, resolution)` | Close it |
| `update_blocker(id, ...)` | Revise a card, keeping its number |
| `delete_blocker(id)` | Erase one that should not have existed |
| `list_my_blockers()` | What this session has pending |
| `board_status(project)` | What is open across the board, with ids |

`project` and `cwd` are worth knowing about: normally both are taken from the
session's working directory, but the Desktop app has no working directory to
take them from, so a card raised there lands under "Claude Desktop" unless
`project` says otherwise.

`update_blocker` changes only the fields it is given and marks the card unread.
`delete_blocker` refuses a card that carries a recorded answer — that answer is
someone's decision. From your own terminal, `claude-blockers delete 42` has no
such scruples.

---

## Handing a blocker to another session

Every card has a number, and any session can pick one up — including one that has
never seen the original conversation. Paste this anywhere:

```
Read blocker #42 with the claude-blockers MCP server (read_blocker),
work out what to do, then record it with answer_blocker.
```

That session gets the whole card, decides, and writes the decision back with
`answer_blocker(42, "...")`. Add `resolve=False` if the answer does not actually
settle it.

The same thing from a terminal:

```bash
claude-blockers status        # every open blocker, with ids
claude-blockers show 42
claude-blockers answer 42 "Going with fp16 — int8 hurt sibilants."
```

---

## The Claude Desktop app

The Desktop app speaks the same stdio MCP protocol as Claude Code, so it runs the
same server against the same database and the same board. `install` wires it
automatically when it finds the app; `--desktop` forces it, `--no-desktop` skips
it.

### Setting it up

**Quit Desktop before you run `install`, not after** — this is the one step that
catches everyone. The app does not merge with its config file, it serialises over
it: it holds the config in memory and writes the whole object back whenever
anything changes. An entry added underneath a running app is discarded the next
time it saves, silently, so `install` reports success and the connector is simply
absent from the app.

1. **Quit Desktop fully.** Closing the window leaves it running — use the tray
   icon, then Quit. On Windows:

   ```powershell
   Get-Process Claude | Where-Object { $_.Path -like '*WindowsApps*' } | Stop-Process -Force
   ```

   The `Path` filter is not optional: the Claude Code CLI is *also* `claude.exe`,
   and a bare `Get-Process Claude | Stop-Process` kills your terminal sessions
   along with the app.

2. **Wire it, with the app still closed:**

   ```bash
   claude-blockers install
   claude-blockers status      # the desktop line should read "wired"
   ```

   `status` reads the config file itself, so it is the check to trust.

3. **Reopen Desktop** and look for `claude-blockers` in the connector menu by the
   message box. All eight tools should be listed.

**Nothing leaves your machine.** Desktop opens the local SQLite file directly,
exactly as a Claude Code session on this host does. There is no server to expose
and no port involved.

### What is different about it

All eight tools work. What changes is what the app can tell the board about
itself:

- **No session to jump back to.** Desktop has no resumable session, so cards
  raised there carry no session id and the board shows no Jump button. It says
  "Claude Desktop" where a Claude Code card shows the session.
- **No working directory.** The app starts the server in your home folder, not in
  whatever the conversation is about. Rather than file every card under your home
  folder's name, blockers raised from Desktop land under the project **you** name
  -- so tell it the project, or pass an absolute path, if you want the card to sit
  with the rest of that project's work. Unnamed, it goes to "Claude Desktop".
- **No hooks.** Desktop has no hook system, so the automatic "this session has
  gone quiet" capture is Claude Code only. `raise_blocker` still works normally.
- **`list_my_blockers` is per-app, not per-chat.** One server serves every
  conversation in the app, so there is no per-chat identity to filter on. It
  widens to everything raised from Desktop on this machine, and says so.
- **No `CLAUDE.md` -- but not no guidance.** That block is Claude Code's only.
  Desktop is told the same things through the channel the protocol provides for
  it: the server declares an `instructions` string in the MCP `initialize`
  handshake, and the client puts it in the system prompt. Both channels are
  built from one shared body, so they cannot drift apart. What the app then does
  with it is the app's choice and nothing here can force it -- if it does not
  reach for the board unprompted, say "raise a blocker" or "check my blockers"
  and it works from there.

Answering is where it earns its place: `board_status`, `read_blocker` and
`answer_blocker` all work, so you can triage from the app what your Claude Code
sessions are stuck on, and the decision lands on the card they are watching.

<details>
<summary>Wiring it by hand</summary>

If `install` could not find the app, add this to `claude_desktop_config.json`,
keeping any servers already in there. On macOS it is in
`~/Library/Application Support/Claude/`.

On Windows, look in `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`
first. The Windows app ships as an MSIX package -- the installer from Anthropic's
site as much as a copy from the Store -- and Windows redirects a packaged app's
`%APPDATA%` writes into that per-package sandbox, so `%APPDATA%\Claude\` may not
exist at all. `install` checks both, and `claude-blockers status` prints which one
it found.

```json
{
  "mcpServers": {
    "claude-blockers": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "claude_blockers", "mcp"],
      "env": {
        "CLAUDE_BLOCKERS_HOME": "/absolute/path/to/.claude-blockers",
        "CLAUDE_BLOCKERS_SURFACE": "claude-desktop"
      }
    }
  }
}
```

`claude-blockers status` prints the interpreter path to use, and warns if the one
already wired has gone missing.
</details>

---

## Sessions in WSL, containers, or on another machine

**Never point two environments at one database file.** A database on a Windows
drive read from WSL — or on any network mount — loses the file locking SQLite
depends on. Concurrent writes do not queue; the second fails instantly with
`disk I/O error` and that blocker is gone. Every journal mode behaves this way.
Sharing the *folder* does not help: it is the locking that breaks, not the path.

So a sandboxed session posts over HTTP, and the host stays the only process that
ever opens the file.

### Setting it up

**1. On the host, bind the board wider than loopback:**

```bash
claude-blockers serve --host 0.0.0.0
```

`--host 0.0.0.0` is required, not optional. Without it the board binds
`127.0.0.1` only, and a sandbox connects from a different interface — so it
cannot reach the board at all, and no token is printed either. Your browser is
unaffected: `0.0.0.0` includes loopback, so `http://127.0.0.1:4317/` works
exactly as before.

**2. In the sandbox, point an install at it** with the token step 1 printed:

```bash
./setup.sh --remote http://$(hostname).mshome.net:4317/ --token=<token>
```

Outside WSL, replace the host with whatever address reaches the machine.

Write `--token=<value>` with the equals sign. A token can begin with a dash, and
`--token <value>` then fails with `argument --token: expected one argument`,
which names neither the token nor the dash.

**3. Restart any Claude Code sessions already running in there.** Claude Code
reads this configuration once, at session start.

### Why the host is named rather than numbered

`$(hostname).mshome.net` is the name Windows registers for itself on the WSL
network. Use it rather than the gateway address from
`ip route | awk '/^default/ {print $3}'`.

The gateway moves. WSL runs behind a NAT whose subnet is picked afresh every
time the WSL *utility VM* cold-starts — a Windows restart, a `wsl --shutdown`,
or every distro stopping and the VM idling out. The distro staying open is what
keeps it looking stable. A URL with last week's number in it does not fail when
the address changes; it fails later, at the moment someone needed to raise a
blocker.

The name does not move, and the client resolves it to an address on every call,
so a reassigned subnet is picked up the first time it is used. The board itself
still only ever sees a numeric address, which is what its rebinding defence
requires — see [Notes and limits](#notes-and-limits).

### Checking it works

```bash
curl -s -X POST http://$(hostname).mshome.net:4317/api/rpc \
  -H 'Content-Type: application/json' -H "Authorization: Bearer <token>" \
  -d '{"op":"stats","kwargs":{}}'
```

Good: a JSON object of counts, matching the board on the host.

**Do not test with a plain `curl http://...:4317/`.** The board serves its UI to
local callers only, so from a sandbox that correctly returns 401 (addressed by
number) or 403 (by name) even when everything is working. It looks like a
failure and is not one.

### When it is not set up

A sandbox with no relay configured is not broken in any way you can see. The
server starts, cards are written, ids come back — into a SQLite file on the
distro's own disk, while the board being watched is a different file on the
host. Nothing errors.

`claude-blockers status` and `install` say so when they are run inside WSL with
no relay. If a board has been running that way for a while, the cards are in
`~/.blockers` or `~/.claude-blockers` **inside the distro**, not on the host.

### What the token can do

It reaches `/api/rpc` and nothing else — a sandboxed session can post blockers
and read them back, but cannot list your projects, read transcript excerpts,
clear the board, or ask the host to open a terminal. Those require a request
from the machine itself, so your own browser is unaffected.

Binding beyond loopback still exposes the board to anything that can reach that
address. The token is not a substitute for a trusted network.

---

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_BLOCKERS_HOME` | `~/.claude-blockers` | Directory holding `blockers.db` |
| `CLAUDE_BLOCKERS_PORT` | `4317` | Port for the board |
| `CLAUDE_BLOCKERS_URL` | unset | Post to this board over HTTP instead of opening a database |
| `CLAUDE_BLOCKERS_TOKEN` | unset | Secret for reaching a board on another machine |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Where Claude Code keeps its config |
| `CLAUDE_DESKTOP_CONFIG` | platform default | Claude Desktop's `claude_desktop_config.json` |
| `CLAUDE_BLOCKERS_SURFACE` | unset | Which Claude is running the server; `install` sets it to `claude-desktop` in the app's config |

`install` writes the first, and the URL and token when given, into the `env`
block of `~/.claude/settings.json`. The CLI reads that same block, which is why
`serve` finds the board your sessions write to even though your shell has none of
these set.

---

## Notes and limits

- **Anything running as you on this machine can drive the board.** Loopback
  needs no token by design — that is what lets your browser work — so any local
  process can read every card, clear the board, or ask it to open a terminal.
  For a single-user laptop that is the right trade; on a shared machine it is
  not. The board is loopback-only unless you pass `--host`, and it exposes
  project paths and transcript excerpts.
- **macOS and Linux are unverified.** Only Windows has been used in anger:
  Windows Terminal, the Desktop app, and WSL2. The mac and Linux paths — the
  `Library/Application Support` Desktop config, `pgrep`/`osascript`, the Linux
  terminal emulators `jump` walks — are written and unit-tested but have never
  run on the real thing.
- **The board must be addressed by number.** It refuses any request whose `Host`
  header is a name; that is the DNS-rebinding defence, and rebinding needs a name
  to work at all. A relay configured with a hostname resolves it before
  connecting, so a sandbox can be given a name that lasts while the board still
  only ever sees an address.
- **Two boards on one port.** WSL forwards its localhost to Windows, so a board
  inside a distro also holds `127.0.0.1:4317` on the host. `serve` checks before
  binding and tells you rather than starting a second board that never answers.
- **Resuming a live session** forks the conversation rather than returning to the
  original. The board says so before you click.
- **No "focus that window" button.** Windows Terminal keeps sessions in tabs and
  offers no way to activate one from outside the process.
- **Why stdio and not HTTP.** Claude Code exports `CLAUDE_CODE_SESSION_ID` and
  `CLAUDE_PID` into the processes it spawns, so a stdio server gets exactly one
  session's identity — which is how the board knows which terminal to send you
  back to. Desktop speaks stdio but exports neither.
- **Claude chats on the web cannot reach this.** claude.ai only connects to
  remote MCP servers on a public HTTPS URL, which would mean exposing a board
  holding your project paths and transcript excerpts to the internet. Claude Code
  and the Desktop app are the two local surfaces, and both are covered.

---

## Development

```bash
git clone https://github.com/learningtocodekg/claude-blockers
cd claude-blockers
uv sync
uv run claude-blockers serve    # run your checkout without installing it
```

Tests are plain scripts, not a pytest suite — each exits non-zero on failure:

```bash
uv run python tests/test_smoke.py
uv run --with quickjs python tests/test_sidebar_order.py
```

The two checks that need a live Claude Code session skip themselves when there is
not one, so the suite runs anywhere.

To point your own Claude Code at a checkout, run `./setup.sh` from inside it —
`install` registers whichever interpreter it runs under.

---

## License

MIT
