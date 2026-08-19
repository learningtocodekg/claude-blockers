"""Plain-text rendering of a blocker.

The same text is what a fresh Claude session gets from `read_blocker` and what
`claude-blockers show` prints in a terminal. Both audiences are reading it cold,
with no memory of the session that raised it, so the card leads with what was
asked and ends with what to do about it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _block(label: str, body: str | None) -> list[str]:
    if not body:
        return []
    return [f"{label}:", *(f"  {line}" for line in body.strip().splitlines()), ""]


def blocker_card(blocker: dict[str, Any], *, board_url: str | None = None) -> str:
    """One blocker, written out for someone who has never seen this board."""
    where = blocker.get("project") or "unknown project"
    if blocker.get("branch"):
        where += f" ({blocker['branch']})"

    lines = [
        f"Blocker #{blocker['id']}: {blocker['title']}",
        f"  status   {blocker.get('status', 'open')}"
        + (f" -- {blocker['resolution']}" if blocker.get("resolution") else ""),
        f"  kind     {blocker.get('kind', 'blocker')}, {blocker.get('urgency', 'normal')} urgency",
        f"  project  {where}",
    ]
    # The Desktop app has no working directory of its own, so a card raised there
    # carries none. An empty "folder" line reads as a path that went missing.
    if blocker.get("cwd"):
        lines.append(f"  folder   {blocker['cwd']}")
    raised = f"  raised   {blocker.get('created_at', '')}"
    if blocker.get("session_id"):
        raised += f" by session {blocker['session_id']}"
    elif blocker.get("surface") == "claude-desktop":
        # Say where it came from, because the reader's next question is why there
        # is no session and no folder to go to.
        raised += " from the Claude Desktop app, which has no session to resume"
    lines.append(raised)
    # Only worth a line once it could be somewhere other than here: a board that
    # only ever sees one machine gains nothing from being told which.
    if blocker.get("host"):
        lines.append(f"  ran on   {blocker['host']}")
    # A reader who wants more than the card says can go to the source. It is only
    # worth naming if the file is actually there -- a path to nothing is a detour.
    transcript = blocker.get("transcript_path")
    if transcript and Path(transcript).is_file():
        lines.append(f"  context  that session's transcript: {transcript}")
    lines.append("")
    lines += _block("What is blocked", blocker.get("detail"))
    lines += _block("How to unblock it", blocker.get("how_to"))

    if blocker.get("answer"):
        answered_by = blocker.get("answered_by") or "unknown"
        lines += _block(
            f"Decision recorded {blocker.get('answered_at', '')} by {answered_by}",
            blocker["answer"],
        )

    if board_url:
        lines.append(f"On the board: {board_url}#{blocker['id']}")
    return "\n".join(lines).rstrip() + "\n"
