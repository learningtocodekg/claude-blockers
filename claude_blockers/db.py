"""SQLite storage.

Several Claude Code sessions and the web server all touch this database at the
same time, so every connection opens in WAL mode with a generous busy timeout.
Writes are small and rare (a handful per session), so contention is a non-issue
in practice -- the settings are here to make a stray concurrent write wait
rather than raise "database is locked".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS blockers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    resolved_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',     -- open | resolved | dismissed
    kind            TEXT    NOT NULL DEFAULT 'blocker',  -- blocker | question | idle | permission
    urgency         TEXT    NOT NULL DEFAULT 'normal',   -- low | normal | high
    title           TEXT    NOT NULL,
    detail          TEXT,
    how_to          TEXT,
    project         TEXT    NOT NULL,
    repo            TEXT,
    branch          TEXT,
    cwd             TEXT    NOT NULL,
    session_id      TEXT,
    transcript_path TEXT,
    claude_pid      INTEGER,
    source          TEXT    NOT NULL DEFAULT 'mcp',      -- mcp | hook
    resolution      TEXT,
    seen            INTEGER NOT NULL DEFAULT 0,
    fingerprint     TEXT,
    answer          TEXT,                                -- the decision, once someone made it
    answered_at     TEXT,
    answered_by     TEXT,
    host            TEXT,
    surface         TEXT                                 -- session id, or 'user' from the CLI
);

CREATE INDEX IF NOT EXISTS idx_blockers_status  ON blockers(status);
CREATE INDEX IF NOT EXISTS idx_blockers_project ON blockers(project);
CREATE INDEX IF NOT EXISTS idx_blockers_session ON blockers(session_id);
CREATE INDEX IF NOT EXISTS idx_blockers_updated ON blockers(updated_at);
CREATE INDEX IF NOT EXISTS idx_blockers_fprint  ON blockers(fingerprint);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    cwd             TEXT,
    project         TEXT,
    transcript_path TEXT,
    pid             INTEGER,
    started_at      TEXT,
    last_seen       TEXT,
    ended_at        TEXT
);
"""


def now() -> str:
    """ISO-8601 UTC, second precision, always suffixed with Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    # A corrupt file, a disconnected network drive or a permissions problem all
    # arrive here as some sqlite3.Error whose text names no file at all -- "file
    # is not a database" and nothing else. That message then travels all the way
    # to a Claude session as the reason its connector died. Naming the path is
    # the difference between a mystery and a thing you can go and move aside.
    path = config.db_path()
    try:
        conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise sqlite3.OperationalError(
            f"could not open the board database at {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the first release. Existing databases are upgraded in
# place rather than rebuilt -- a board that has been collecting blockers for
# weeks should survive an update.
_MIGRATIONS = {
    "blockers": {
        "repo": "TEXT",
        "branch": "TEXT",
        "answer": "TEXT",
        "answered_at": "TEXT",
        "answered_by": "TEXT",
        # Which machine or sandbox raised it. Blank on every row written before
        # a board could receive blockers from more than one, which is exactly
        # what "no host recorded" should mean.
        "host": "TEXT",
        # Which Claude the blocker came from: "claude-code", "claude-desktop".
        # Blank on rows written before the board served more than one, which
        # reads correctly as "back when there was only Claude Code".
        "surface": "TEXT",
    },
}


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in _MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# --------------------------------------------------------------------------- #
# blockers
# --------------------------------------------------------------------------- #

def create_blocker(
    *,
    title: str,
    detail: str | None,
    how_to: str | None,
    project: str,
    cwd: str,
    repo: str | None = None,
    branch: str | None = None,
    urgency: str = "normal",
    kind: str = "blocker",
    session_id: str | None = None,
    transcript_path: str | None = None,
    claude_pid: int | None = None,
    source: str = "mcp",
    fingerprint: str | None = None,
    host: str | None = None,
    surface: str | None = None,
) -> int:
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO blockers (
                created_at, updated_at, status, kind, urgency, title, detail,
                how_to, project, repo, branch, cwd, session_id, transcript_path,
                claude_pid, source, fingerprint, host, surface
            ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts, ts, kind, urgency, title, detail, how_to, project, repo,
                branch, cwd, session_id, transcript_path, claude_pid, source,
                fingerprint, host, surface,
            ),
        )
        return int(cur.lastrowid)


def find_open_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    """Used by hooks to avoid stacking duplicate 'waiting on you' entries."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM blockers WHERE fingerprint = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def touch_blocker(blocker_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE blockers SET updated_at = ? WHERE id = ?", (now(), blocker_id))


def list_blockers(
    *,
    status: str | None = "open",
    project: str | None = None,
    session_id: str | None = None,
    surface: str | None = None,
    host: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if project and project != "all":
        clauses.append("project = ?")
        params.append(project)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if surface:
        clauses.append("surface = ?")
        params.append(surface)
    if host:
        clauses.append("host = ?")
        params.append(host)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM blockers {where}
            ORDER BY
                CASE status WHEN 'open' THEN 0 ELSE 1 END,
                CASE urgency WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_blocker(blocker_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM blockers WHERE id = ?", (blocker_id,)).fetchone()
    return _row_to_dict(row) if row else None


def set_status(blocker_id: int, status: str, resolution: str | None = None) -> bool:
    ts = now()
    resolved_at = ts if status in ("resolved", "dismissed") else None
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE blockers
               SET status = ?, resolution = COALESCE(?, resolution),
                   resolved_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (status, resolution, resolved_at, ts, blocker_id),
        )
        return cur.rowcount > 0


EDITABLE = ("title", "detail", "how_to", "urgency", "kind", "project")


def update_blocker(blocker_id: int, **fields: Any) -> list[str]:
    """Revise a blocker in place. Returns the names of the fields that changed.

    Only the card's content is editable -- its number, origin, timestamps and
    status are the record of what happened and are not up for rewriting.

    Editing clears `seen`, so the row lights up as unread again. A user who read
    the old instructions has not read the new ones, and silently swapping the
    steps under someone who already looked is worse than not editing at all.
    """
    changes = {
        k: v for k, v in fields.items()
        if k in EDITABLE and v is not None
    }
    if not changes:
        return []
    assignments = ", ".join(f"{k} = ?" for k in changes)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE blockers SET {assignments}, seen = 0, updated_at = ? WHERE id = ?",
            (*changes.values(), now(), blocker_id),
        )
        return list(changes) if cur.rowcount > 0 else []


def delete_blocker(blocker_id: int) -> bool:
    """Erase one blocker outright. Nothing keeps a copy -- see delete_by_status."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM blockers WHERE id = ?", (blocker_id,))
        return cur.rowcount > 0


def record_answer(
    blocker_id: int,
    answer: str,
    *,
    answered_by: str | None = None,
    resolve: bool = True,
    resolution: str = "Answered",
) -> bool:
    """Write the decision onto a blocker.

    This is how a session that did not raise the blocker hands its verdict back:
    the answer is stored on the row itself rather than folded into `resolution`,
    so the board can show "here is what was decided" separately from "this stopped
    needing you". Answering resolves it by default -- a decision that leaves the
    card open is possible but is the unusual case.
    """
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE blockers
               SET answer = ?, answered_at = ?, answered_by = ?, updated_at = ?,
                   status = CASE WHEN ? THEN 'resolved' ELSE status END,
                   resolved_at = CASE WHEN ? THEN ? ELSE resolved_at END,
                   resolution = CASE WHEN ? THEN COALESCE(resolution, ?)
                                     ELSE resolution END
             WHERE id = ?
            """,
            (answer, ts, answered_by, ts, resolve, resolve, ts, resolve, resolution,
             blocker_id),
        )
        return cur.rowcount > 0


CLEARABLE = ("open", "resolved", "dismissed")

# The only statuses a row may hold. Anything else and the card falls out of every
# tab, every filter and every clear -- present on the board but unreachable from
# it, which is worse than a card that was simply deleted.
STATUSES = ("open", "resolved", "dismissed")


def delete_by_status(statuses: Iterable[str]) -> int:
    """Permanently remove blockers in the given statuses.

    This is the only destructive path in the app: clearing throws away history so
    the board stops accumulating forever. Callers always name the statuses, and
    nothing anywhere defaults to including 'open' -- wiping something still
    waiting on the user is a scope they have to pick and confirm by hand.
    """
    wanted = [s for s in dict.fromkeys(statuses) if s in CLEARABLE]
    if not wanted:
        return 0
    placeholders = ",".join("?" * len(wanted))
    with connect() as conn:
        cur = conn.execute(
            f"DELETE FROM blockers WHERE status IN ({placeholders})", wanted
        )
        return cur.rowcount


def mark_seen(ids: Iterable[int]) -> None:
    ids = list(ids)
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        conn.execute(f"UPDATE blockers SET seen = 1 WHERE id IN ({placeholders})", ids)


def resolve_session_hook_blockers(session_id: str) -> int:
    """Close hook-generated 'waiting on you' rows once the user actually replies.

    Only touches source='hook' rows -- an explicit raise_blocker from Claude
    describes real work and must be closed deliberately, not by the act of
    typing something into that session.
    """
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE blockers
               SET status = 'resolved', resolved_at = ?, updated_at = ?,
                   resolution = COALESCE(resolution, 'Answered in session')
             WHERE session_id = ? AND status = 'open' AND source = 'hook'
            """,
            (ts, ts, session_id),
        )
        return cur.rowcount


def projects() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT project,
                   SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
                   COUNT(*) AS total
              FROM blockers
             GROUP BY project
             ORDER BY open_count DESC, project ASC
            """
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def stats() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open,
                SUM(CASE WHEN status = 'open' AND seen = 0 THEN 1 ELSE 0 END) AS unseen,
                SUM(CASE WHEN status = 'open' AND urgency = 'high' THEN 1 ELSE 0 END) AS high,
                SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
                SUM(CASE WHEN status = 'dismissed' THEN 1 ELSE 0 END) AS dismissed,
                COUNT(*) AS total
              FROM blockers
            """
        ).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #

def upsert_session(
    *,
    session_id: str,
    cwd: str | None = None,
    project: str | None = None,
    transcript_path: str | None = None,
    pid: int | None = None,
    ended: bool = False,
) -> None:
    ts = now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, cwd, project, transcript_path, pid,
                                  started_at, last_seen, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                cwd             = COALESCE(excluded.cwd, sessions.cwd),
                project         = COALESCE(excluded.project, sessions.project),
                transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
                pid             = COALESCE(excluded.pid, sessions.pid),
                last_seen       = excluded.last_seen,
                ended_at        = excluded.ended_at
            """,
            (session_id, cwd, project, transcript_path, pid, ts, ts,
             ts if ended else None),
        )


def open_sessions() -> list[dict[str, Any]]:
    """Sessions that never reported an end. Some will have died without saying so,
    so callers still need to check the pid before calling one live."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_session(session_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None
