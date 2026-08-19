"""What Claude is told about this board, and through which channel.

There are two ways to put standing guidance in front of a model, and this
project needs both because no single one reaches every surface.

CLAUDE.md is Claude Code's, and only Claude Code's. `install` writes the block
below into ~/.claude/CLAUDE.md and Claude Code loads it into every session in
every folder. The Desktop app never reads that file.

MCP `instructions` is the protocol's own answer: the server declares a string
during the `initialize` handshake and the client puts it in the system prompt.
It is client-agnostic, so it reaches Desktop and anything else that speaks MCP,
and it travels with the server rather than with a file on disk.

Tool descriptions are a third channel and a much weaker one -- a description is
only read once the model is already considering that tool, so it can explain how
to call `raise_blocker` well but cannot instil the habit of calling it before
going idle, which is the entire point.

So: the body of the guidance lives here once, and each channel wraps it. The two
were separate strings before, and the copy that was not being edited went stale.
"""

from __future__ import annotations

from . import context

# The channels announce themselves differently, but the substance must not
# diverge. Everything surface-neutral belongs in here.
CORE = """You have a `claude-blockers` MCP server. The user runs many Claude sessions
at once and cannot watch them all. A session that stops and waits without
posting is invisible to them, and the work simply stalls -- often for hours.
Posting is how you get unblocked.

Call `raise_blocker` as soon as you need a human action and cannot proceed:
something to test on real hardware or in a browser, a credential or API key, a
decision only they can make, output to eyeball, a service to start, a file to
place.

- Post it **before** you go idle, not after they come asking.
- **One blocker per thing they have to do.** Asked what is blocking a project
  and found five? That is five calls. Each gets its own urgency and
  instructions, and can be ticked off on its own -- a single card listing five
  tasks cannot be half-done.
- Write `how_to` for someone with no memory of this conversation: exact
  commands, exact paths, and what a good result looks like versus a bad one.
- Set urgency `high` only when everything you are doing is stalled behind it.
- Call `resolve_blocker` the moment it stops mattering, including when you find
  another way through. A stale blocker costs them a pointless context switch.
- Do not post things you can work out yourself. This is for human-only actions.

Every blocker has a number. If the user points you at one -- "read blocker 42",
"take a look at #42, make the call" -- call `read_blocker(42)`. It works from any
session, in any folder, whether or not you raised it. `board_status` lists the
open ones with their numbers when they name a blocker instead of numbering it.

Having decided, call `answer_blocker(42, "...")`. Write the decision and enough
reasoning that nobody has to make it twice; it lands on the card, where both the
user and the session that raised it will see it.

A card is not frozen once posted. `update_blocker(42, how_to="...")` revises the
fields you name and keeps the number, which beats resolving and re-raising when
you learn the steps were wrong. `delete_blocker(42)` erases one for good -- use it
only for cards that should never have existed, like a duplicate or one aimed at
the wrong project. Finished work gets `resolve_blocker`, and deciding the board
looks cluttered is the user's call, not yours."""


# Everything above is true everywhere. This is what is only true in the app.
_DESKTOP = """This is the Claude Desktop app, which tells the server less about itself than
Claude Code does. There is no working directory here, so nothing can work out
which project a card belongs to: pass `project` on `raise_blocker` whenever you
know it, or the card lands under "Claude Desktop" rather than with the work it
is about. There is no session to resume either, so a card raised here offers the
user no way back to this conversation -- put into `how_to` anything they would
otherwise have had to come back and ask you for.

Answering is what this surface is best at. `board_status`, `read_blocker` and
`answer_blocker` all work, so the app is a good place to clear what the user's
Claude Code sessions are stuck on."""


def for_claude_md(start: str, end: str) -> str:
    """The block `install` writes into Claude Code's CLAUDE.md.

    Fenced by markers so that re-running install replaces our block and leaves
    whatever else the user keeps in that file alone.
    """
    return f"{start}\n## Claude Blockers\n\n{CORE}\n{end}"


def for_mcp(surface: str) -> str | None:
    """What to declare in the `initialize` handshake, if anything.

    None for Claude Code, which is not an oversight: it already loads all of
    this from CLAUDE.md, and Claude Code honours `instructions` too, so
    declaring it there would put the same several hundred words into the system
    prompt twice. Every other surface has no such file and gets it here.
    """
    if surface == context.CLAUDE_CODE:
        return None
    if surface == context.CLAUDE_DESKTOP:
        return f"{CORE}\n\n{_DESKTOP}"
    return CORE
