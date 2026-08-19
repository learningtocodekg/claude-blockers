"""How Claude is told what this board is for, on each surface.

    uv run python tests/test_guidance.py

WHAT WENT WRONG. `install` writes a guidance block into ~/.claude/CLAUDE.md, and
that file is Claude Code's alone -- the Desktop app never reads it. So Desktop
had no standing instructions at all: the only thing describing the board was the
tool descriptions, which are not read until the model is already reaching for a
tool. They can explain how to call raise_blocker properly, but they cannot
produce the behaviour the whole project exists for, which is posting *before*
going idle rather than after the user comes asking.

MCP has an answer built in. The server declares an `instructions` string in the
`initialize` handshake and the client puts it in the system prompt -- no file, no
per-client format, and it reaches anything that speaks the protocol. The tests at
the bottom drive a real handshake, because an attribute set on an object proves
nothing about what actually goes out on the wire.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="claude-blockers-guidance-"))

os.environ["CLAUDE_BLOCKERS_HOME"] = str(TMP / "home")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude")
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
os.environ.pop("CLAUDE_BLOCKERS_SURFACE", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_blockers import cli, context, guidance  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


BAR = "=" * 58

# --------------------------------------------------------------------------- #
print("One body, two channels")

# The two used to be separate strings, and the one nobody was editing went
# stale. Whatever else changes, they have to keep saying the same thing.
check("CLAUDE.md carries the shared body", guidance.CORE in cli.GUIDANCE)
check("and is fenced by the markers install looks for",
      cli.GUIDANCE.startswith(cli.MARK_START) and cli.GUIDANCE.endswith(cli.MARK_END))
check("the body names the tool that matters", "raise_blocker" in guidance.CORE)
check("and the rule that makes it useful", "before" in guidance.CORE)

# The body has to read correctly on a surface that is not Claude Code, so it
# cannot talk about Claude Code specifically.
check("the body does not assume Claude Code",
      "Claude Code" not in guidance.CORE, "it is declared to Desktop too")

# --------------------------------------------------------------------------- #
print("\nWhat each surface is told")

desktop = guidance.for_mcp(context.CLAUDE_DESKTOP)
check("Desktop gets instructions", isinstance(desktop, str) and bool(desktop))
check("including the shared body", guidance.CORE in desktop)
check("and that it must name the project itself", "project" in desktop,
      "nothing else can work it out there -- no working directory")
check("and that there is no session to come back to",
      "resume" in desktop or "no way back" in desktop, f"got {desktop[-400:]}")
check("and that answering is what it is good for", "answer_blocker" in desktop)

# Claude Code already loads all of this from CLAUDE.md, and honours
# `instructions` as well -- declaring it would put the same text in the system
# prompt twice.
check("Claude Code is told nothing here", guidance.for_mcp(context.CLAUDE_CODE) is None,
      "CLAUDE.md already covers it")

unknown = guidance.for_mcp("unknown")
check("an unrecognised surface still gets the body", unknown == guidance.CORE,
      "a server someone ran by hand is better informed than not")
check("but not the Desktop-only part", "Claude Desktop app" not in (unknown or ""))

# --------------------------------------------------------------------------- #
print("\nWhat actually goes out on the wire")

# An attribute on the server object proves nothing about the handshake. This
# runs the real stdio server and reads the real initialize response, which is
# the thing a client sees.


def handshake(surface: str) -> dict:
    env = dict(os.environ)
    env["CLAUDE_BLOCKERS_SURFACE"] = surface
    env["CLAUDE_BLOCKERS_HOME"] = str(TMP / "home")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    proc = subprocess.run(
        [sys.executable, "-m", "claude_blockers", "mcp"],
        input=json.dumps(request) + "\n",
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue  # stdout is the JSON-RPC channel, but be forgiving.
        if message.get("id") == 1:
            return message.get("result") or {}
    raise AssertionError(f"no initialize response; stderr: {proc.stderr[-400:]}")


result = handshake(context.CLAUDE_DESKTOP)
check("the Desktop handshake carries instructions", "instructions" in result,
      f"got keys {sorted(result)}")
check("and they are the Desktop ones",
      result.get("instructions") == guidance.for_mcp(context.CLAUDE_DESKTOP))
check("the server still identifies itself",
      (result.get("serverInfo") or {}).get("name") == "claude-blockers", f"got {result.get('serverInfo')}")

code = handshake(context.CLAUDE_CODE)
check("the Claude Code handshake carries none",
      not code.get("instructions"), f"got {str(code.get('instructions'))[:80]}")
check("and is otherwise a normal handshake", "capabilities" in code, f"got keys {sorted(code)}")

# --------------------------------------------------------------------------- #
print(f"\n{BAR}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{BAR}\n")

sys.exit(1 if FAIL else 0)
