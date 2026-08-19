"""Hook entrypoint. Claude Code pipes a JSON event on stdin.

This runs inside the user's session on every configured event, so it follows two
rules absolutely: never block for long, and never fail loudly. Any exception is
swallowed and the process still exits 0 -- a bug in the board must never wedge
a real coding session.

Events handled:
    SessionStart      register the session (cwd, pid, transcript)
    Notification      the session is waiting on the user -> post a blocker
    UserPromptSubmit  the user replied -> close hook-generated blockers
    Stop              keep last_seen fresh
    SessionEnd        mark ended, close its stale "waiting" rows
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from . import backend, context


def _fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _classify(message: str) -> tuple[str, str]:
    """Map a Notification message to (kind, title)."""
    low = message.lower()
    if "permission" in low or "approve" in low:
        return "permission", message.strip() or "Permission needed"
    return "idle", message.strip() or "Session is waiting on you"


def _handle_session_start(event: dict[str, Any]) -> None:
    sid = event.get("session_id")
    if not sid:
        return
    cwd = event.get("cwd") or ""
    backend.upsert_session(
        session_id=sid,
        cwd=cwd,
        project=context.project_label(cwd) if cwd else None,
        transcript_path=event.get("transcript_path") or context.transcript_path(cwd, sid),
        pid=context.claude_pid(),
    )


def _handle_notification(event: dict[str, Any]) -> None:
    sid = event.get("session_id")
    cwd = event.get("cwd") or context.resolve_cwd()
    message = str(event.get("message") or "").strip()
    kind, title = _classify(message)

    # Keep the session row current first -- the board's "is it still alive?"
    # check and its jump button both read from it.
    if sid:
        backend.upsert_session(
            session_id=sid,
            cwd=cwd,
            project=context.project_label(cwd),
            transcript_path=event.get("transcript_path"),
            pid=context.claude_pid(),
        )

    fp = _fingerprint(str(sid), kind, message)
    existing = backend.find_open_by_fingerprint(fp)
    if existing:
        # Same session already waiting for the same reason -- refresh, don't stack.
        backend.touch_blocker(int(existing["id"]))
        return

    git = context.git_info(cwd)
    project = git["repo"] or context.project_label(cwd)
    backend.create_blocker(
        title=title,
        repo=git["repo"],
        branch=git["branch"],
        detail=(
            f"This session in '{project}' stopped and is waiting on you. It was "
            "captured automatically rather than described by Claude, so there is "
            "nothing here beyond what the session itself said when it stopped."
        ),
        how_to=(
            f"Switch to the session running in {cwd} and answer it there. Its id is "
            "shown above if you need to find the right tab, and the resume command "
            "is there for when the session has since been closed."
        ),
        project=project,
        cwd=cwd,
        urgency="normal",
        kind=kind,
        source="hook",
        session_id=sid,
        transcript_path=event.get("transcript_path") or context.transcript_path(cwd, sid),
        claude_pid=context.claude_pid(),
        fingerprint=fp,
        host=context.host_label(),
    )


def _handle_user_prompt(event: dict[str, Any]) -> None:
    sid = event.get("session_id")
    if not sid:
        return
    backend.resolve_session_hook_blockers(sid)
    cwd = event.get("cwd") or ""
    backend.upsert_session(
        session_id=sid,
        cwd=cwd or None,
        project=context.project_label(cwd) if cwd else None,
        pid=context.claude_pid(),
    )


def _handle_stop(event: dict[str, Any]) -> None:
    sid = event.get("session_id")
    if not sid:
        return
    cwd = event.get("cwd") or ""
    backend.upsert_session(
        session_id=sid,
        cwd=cwd or None,
        project=context.project_label(cwd) if cwd else None,
        pid=context.claude_pid(),
    )


def _handle_session_end(event: dict[str, Any]) -> None:
    sid = event.get("session_id")
    if not sid:
        return
    backend.resolve_session_hook_blockers(sid)
    backend.upsert_session(session_id=sid, ended=True)


HANDLERS = {
    "SessionStart": _handle_session_start,
    "Notification": _handle_notification,
    "UserPromptSubmit": _handle_user_prompt,
    "Stop": _handle_stop,
    "SessionEnd": _handle_session_end,
}


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        handler = HANDLERS.get(str(event.get("hook_event_name") or ""))
        if handler:
            backend.init()
            handler(event)
    except Exception as exc:  # never wedge the session
        print(f"claude-blockers hook: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
