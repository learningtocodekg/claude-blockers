"""End-to-end smoke test. Run with: uv run python tests/test_smoke.py

Uses a temporary CLAUDE_BLOCKERS_HOME so it never touches your real board.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="claude-blockers-test-")
os.environ["CLAUDE_BLOCKERS_HOME"] = TMP
os.environ["CLAUDE_BLOCKERS_PORT"] = "4319"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_blockers import config, context, db, hook, jump  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


def skip(name: str, reason: str) -> None:
    SKIP.append(name)
    print(f"  [SKIP] {name}  -- {reason}")


# A couple of checks read the identity Claude Code injects into the processes it
# spawns, so they can only mean anything when this file is run *by* a session.
# On CI, in a container, or from a plain terminal there is no such identity and
# the right answer is to skip rather than to fail -- a suite that only passes on
# its author's machine is one nobody else will run.
UNDER_CLAUDE = bool(os.environ.get("CLAUDE_CODE_SESSION_ID"))


WIN = sys.platform == "win32"
CWD = r"C:\code\acme-audio" if WIN else "/home/you/code/acme-audio"


def event(**kwargs) -> dict:
    return kwargs


# --------------------------------------------------------------------------- #
print("\n== database ==")
db.init()
check("database file created", config.db_path().exists(), str(config.db_path()))

bid = db.create_blocker(
    title="Test the mic", detail="d", how_to="h",
    project="acme-audio", cwd=CWD, urgency="high",
    session_id="sess-abc", claude_pid=None,
)
check("create_blocker returns an id", isinstance(bid, int) and bid > 0)

row = db.get_blocker(bid)
check("blocker round-trips", row is not None and row["title"] == "Test the mic")
check("defaults to open", row["status"] == "open")
check("stats counts it", db.stats()["open"] == 1)

db.set_status(bid, "resolved", "done")
check("resolve changes status", db.get_blocker(bid)["status"] == "resolved")
check("resolve records resolution", db.get_blocker(bid)["resolution"] == "done")
check("resolved drops from open stats", db.stats()["open"] == 0)


# --------------------------------------------------------------------------- #
# Handing a blocker to a session that never saw the conversation: it is found by
# id alone, and the decision comes back onto the same row.
print("\n== read by id / answer ==")
from claude_blockers import render  # noqa: E402

hand_id = db.create_blocker(
    title="Decide: fp16 or int8", detail="Both fit; int8 frees 3GB.",
    how_to="Listen to both clips, reply with the one you can live with.",
    project="acme-audio", cwd=CWD, kind="question", session_id="sess-somebody-else",
)
card = render.blocker_card(db.get_blocker(hand_id), board_url="http://board/")
check("card leads with the id", card.startswith(f"Blocker #{hand_id}:"))
for needed in ("Decide: fp16 or int8", "What is blocked", "How to unblock it",
               "acme-audio", f"http://board/#{hand_id}"):
    check(f"card carries {needed!r}", needed in card)
check("an unanswered card has no decision section", "Decision recorded" not in card)

check("record_answer writes the answer",
      db.record_answer(hand_id, "Keep fp16.", answered_by="sess-fresh")
      and db.get_blocker(hand_id)["answer"] == "Keep fp16.")
answered = db.get_blocker(hand_id)
check("answering resolves it by default", answered["status"] == "resolved")
check("answering stamps who and when",
      answered["answered_by"] == "sess-fresh" and bool(answered["answered_at"]))
check("answering fills the resolution", answered["resolution"] == "Answered")
check("the answer shows on the card",
      "Decision recorded" in render.blocker_card(answered)
      and "Keep fp16." in render.blocker_card(answered))

# An answer that does not settle it must leave the card on the board.
open_answer_id = db.create_blocker(title="Half-decided", detail="d", how_to="h",
                                   project="acme-audio", cwd=CWD)
db.record_answer(open_answer_id, "Leaning int8, still need your ears.", resolve=False)
still = db.get_blocker(open_answer_id)
check("resolve=False keeps it open", still["status"] == "open")
check("resolve=False still records the answer", still["answer"].startswith("Leaning int8"))
check("an unresolved answer sets no resolution", still["resolution"] is None)
check("answering a vanished id is a no-op", db.record_answer(999_999, "nobody") is False)
db.set_status(hand_id, "resolved")
db.set_status(open_answer_id, "resolved")
check("board is back to empty for the next section", db.stats()["open"] == 0)


# --------------------------------------------------------------------------- #
# A posted card can be revised and, when it should never have existed, erased.
print("\n== edit / delete ==")
edit_id = db.create_blocker(title="Wrong title", detail="d", how_to="old steps",
                            project="acme-audio", cwd=CWD, urgency="low")
db.mark_seen([edit_id])
changed = db.update_blocker(edit_id, title="Right title", how_to="new steps",
                            urgency="high")
edited = db.get_blocker(edit_id)
check("update reports the fields it changed", set(changed) == {"title", "how_to", "urgency"},
      str(changed))
check("update rewrites the named fields",
      edited["title"] == "Right title" and edited["how_to"] == "new steps"
      and edited["urgency"] == "high")
check("update leaves unnamed fields alone", edited["detail"] == "d")
check("editing marks it unread again", edited["seen"] == 0)
check("update keeps the id and status", edited["id"] == edit_id and edited["status"] == "open")
check("update with nothing to change is a no-op", db.update_blocker(edit_id) == [])
check("update cannot rewrite fields outside the card's text",
      db.update_blocker(edit_id, status="resolved", id=1) == []
      and db.get_blocker(edit_id)["status"] == "open")
check("updating a vanished id changes nothing", db.update_blocker(999_999, title="x") == [])

keeper_id = db.create_blocker(title="Untouched", detail="d", how_to="h",
                              project="acme-audio", cwd=CWD)
check("delete removes the row", db.delete_blocker(edit_id) is True
      and db.get_blocker(edit_id) is None)
check("delete leaves its neighbours alone", db.get_blocker(keeper_id) is not None)
check("deleting a vanished id is False", db.delete_blocker(999_999) is False)
db.delete_blocker(keeper_id)
check("nothing open going into the next section", db.stats()["open"] == 0)


# --------------------------------------------------------------------------- #
print("\n== context / session identity ==")
check(
    "project label is the folder name",
    context.project_label(CWD) == "acme-audio",
    context.project_label(CWD),
)
encoded = context.encode_project_dir(r"C:\code\acme-docs")
check(
    "transcript dir encoding matches Claude Code",
    encoded == "C--code-acme-docs",
    encoded,
)
# This process was itself spawned by Claude Code, so the real env var is present.
if UNDER_CLAUDE:
    check(
        "reads CLAUDE_CODE_SESSION_ID from env",
        context.session_id() is not None,
        "not running under Claude Code",
    )
else:
    skip("reads CLAUDE_CODE_SESSION_ID from env", "not running under Claude Code")


# --------------------------------------------------------------------------- #
print("\n== hooks ==")
hook.HANDLERS["SessionStart"](event(
    hook_event_name="SessionStart", session_id="sess-hook-1",
    cwd=CWD, transcript_path="x.jsonl", source="startup",
))
check("SessionStart registers the session", db.get_session("sess-hook-1") is not None)
check("SessionStart records cwd", db.get_session("sess-hook-1")["cwd"] == CWD)

notify = event(
    hook_event_name="Notification", session_id="sess-hook-1", cwd=CWD,
    message="Claude needs your permission to use Bash",
)
hook.HANDLERS["Notification"](notify)
open_after_one = db.stats()["open"]
check("Notification creates a blocker", open_after_one == 1)

created = db.list_blockers(status="open")[0]
check("classified as a permission block", created["kind"] == "permission", created["kind"])
check("marked as hook-sourced", created["source"] == "hook")
check("labelled with the project", created["project"] == "acme-audio")

hook.HANDLERS["Notification"](notify)
hook.HANDLERS["Notification"](notify)
check(
    "identical notifications dedupe instead of stacking",
    db.stats()["open"] == 1,
    f"open={db.stats()['open']}",
)

hook.HANDLERS["Notification"](event(
    hook_event_name="Notification", session_id="sess-hook-1", cwd=CWD,
    message="Claude is waiting for your input",
))
check("a different message is a new blocker", db.stats()["open"] == 2)
check(
    "idle message classified as idle",
    any(b["kind"] == "idle" for b in db.list_blockers(status="open")),
)

# An MCP-raised blocker must survive the user simply typing in that session.
mcp_id = db.create_blocker(
    title="Real work item", detail="d", how_to="h", project="acme-audio",
    cwd=CWD, session_id="sess-hook-1", source="mcp",
)
hook.HANDLERS["UserPromptSubmit"](event(
    hook_event_name="UserPromptSubmit", session_id="sess-hook-1",
    cwd=CWD, prompt="ok go",
))
check("replying closes auto-captured blockers", db.stats()["open"] == 1)
check(
    "replying does NOT close an explicit raise_blocker",
    db.get_blocker(mcp_id)["status"] == "open",
)

hook.HANDLERS["SessionEnd"](event(hook_event_name="SessionEnd", session_id="sess-hook-1"))
check("SessionEnd marks the session ended", db.get_session("sess-hook-1")["ended_at"] is not None)

# Malformed input must not raise -- a crash here would wedge a real session.
proc = subprocess.run(
    [sys.executable, "-m", "claude_blockers", "hook"],
    input="this is not json", capture_output=True, text=True,
    cwd=str(Path(__file__).resolve().parent.parent), env={**os.environ},
)
check("malformed hook input still exits 0", proc.returncode == 0, f"rc={proc.returncode}")


# --------------------------------------------------------------------------- #
print("\n== jump ==")
check("pid_alive(None) is False", jump.pid_alive(None) is False)
check("pid_alive(current process) is True", jump.pid_alive(os.getpid()) is True)
check("pid_alive(bogus pid) is False", jump.pid_alive(999_999_998) is False)
check(
    "resume command is well formed",
    jump.resume_command("abc-123") == "claude --resume abc-123",
    jump.resume_command("abc-123"),
)
result = jump.jump({"session_id": None, "cwd": CWD, "claude_pid": None})
check("jump without a session id fails cleanly", result["ok"] is False and "action" in result)

# This test process is Python, not Claude -- so the name guard must reject it
# even though the pid is perfectly alive. That guard is what stops a leaked or
# recycled pid from focusing an unrelated terminal window.
if WIN:
    name = jump.process_image_name(os.getpid())
    check("can read a process image name", name is not None and name.endswith(".exe"), str(name))
    check(
        "pid_is_claude rejects a live non-Claude process",
        jump.pid_is_claude(os.getpid()) is False,
        f"image={name}",
    )
check("pid_is_claude rejects a dead pid", jump.pid_is_claude(999_999_998) is False)

# A resumed session must not inherit the board server's session identity. When
# the server is started from inside a Claude session, inheriting
# CLAUDE_CODE_CHILD_SESSION makes the new session stop saving its transcript.
os.environ["CLAUDE_CODE_CHILD_SESSION"] = "1"
os.environ["CLAUDE_PID"] = "12345"
spawned = jump.spawn_env()
for leaked in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
               "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_JOB_DIR"):
    check(f"spawn env drops {leaked}", leaked not in spawned)
check(
    "spawn env keeps everything else",
    spawned.get("CLAUDE_BLOCKERS_HOME") == os.environ.get("CLAUDE_BLOCKERS_HOME"),
)
check("spawn env is not empty", len(spawned) > 5, f"only {len(spawned)} vars")

check("focus machinery is gone", not hasattr(jump, "focus_session_window"))

# --------------------------------------------------------------------------- #
print("\n== many blockers from one session ==")
# "What is blocking this project?" should produce one card per task, not one
# card listing five. Nothing may collapse or dedupe them.
before_many = db.stats()["open"]
many_ids = [
    db.create_blocker(
        title=f"Task {n}", detail=f"detail {n}", how_to=f"step {n}",
        project="multi", cwd=CWD, session_id="one-session",
        urgency=["low", "normal", "high"][n % 3], source="mcp",
    )
    for n in range(5)
]
check("five calls create five rows", len(set(many_ids)) == 5, str(many_ids))
check("all five are open", db.stats()["open"] == before_many + 5)
mine = db.list_blockers(status="open", session_id="one-session")
check("all five belong to the one session", len(mine) == 5)
check("they keep their own urgencies", len({b["urgency"] for b in mine}) == 3)

# Resolving one must leave the rest alone -- the reason for separate cards.
db.set_status(many_ids[0], "resolved", "did that one")
still_open = db.list_blockers(status="open", session_id="one-session")
check("resolving one leaves the other four open", len(still_open) == 4)
check("the resolved one is actually closed",
      db.get_blocker(many_ids[0])["status"] == "resolved")

# Identical text posted twice is still two blockers: the user really may need
# the same thing done in two places.
dup_a = db.create_blocker(title="Same", detail="d", how_to="h", project="multi",
                          cwd=CWD, session_id="one-session", source="mcp")
dup_b = db.create_blocker(title="Same", detail="d", how_to="h", project="multi",
                          cwd=CWD, session_id="one-session", source="mcp")
check("identical MCP blockers are not deduped", dup_a != dup_b
      and db.get_blocker(dup_a)["status"] == "open"
      and db.get_blocker(dup_b)["status"] == "open")

# --------------------------------------------------------------------------- #
print("\n== upgrade from the old name ==")
from claude_blockers import cli  # noqa: E402

check("legacy hook commands are recognised as ours",
      cli._is_ours('"py.exe" -m blocker_board hook') is True)
check("current hook commands are recognised as ours",
      cli._is_ours('"py.exe" -m claude_blockers hook') is True)
check("unrelated hook commands are left alone",
      cli._is_ours("some-other-tool --flag") is False)

legacy_md = Path(TMP) / "CLAUDE.md"
legacy_md.write_text(
    "# My notes\n\nkeep this\n\n"
    f"{cli.LEGACY_MARK_START}\nold guidance about blocker-board\n{cli.LEGACY_MARK_END}\n",
    encoding="utf-8",
)
cli._install_guidance(legacy_md)
after_md = legacy_md.read_text(encoding="utf-8")
check("old guidance block is removed on upgrade", cli.LEGACY_MARK_START not in after_md)
check("new guidance block is added", cli.MARK_START in after_md)
check("unrelated notes survive the upgrade", "keep this" in after_md)
check("guidance is not duplicated", after_md.count(cli.MARK_START) == 1)

cli._install_guidance(legacy_md)
check("installing twice does not stack blocks",
      legacy_md.read_text(encoding="utf-8").count(cli.MARK_START) == 1)

# --------------------------------------------------------------------------- #
print("\n== project / repo metadata ==")
REPO_ROOT = Path(__file__).resolve().parent.parent
git = context.git_info(REPO_ROOT)
check("detects the enclosing repo", git["repo"] == REPO_ROOT.name, str(git))
check("detects the branch", isinstance(git["branch"], str) and len(git["branch"]) > 0, str(git))

# A session working in a subdirectory should still be labelled with the project,
# not with the subdirectory it happens to be sitting in.
sub = REPO_ROOT / "claude_blockers" / "static"
check("subdirectory still reports the repo", context.git_info(sub)["repo"] == REPO_ROOT.name)
check("plain folder label is the folder name", context.project_label(sub) == "static")

no_git = context.git_info(Path(tempfile.mkdtemp()))
check("non-repo returns no repo", no_git["repo"] is None and no_git["branch"] is None)

described = context.describe(str(REPO_ROOT))
check("describe prefers the repo for project", described["project"] == REPO_ROOT.name)
check("describe carries the branch", described["branch"] == git["branch"])
check(
    "an explicit project name wins over the repo",
    context.describe(str(REPO_ROOT), "My Custom Name")["project"] == "My Custom Name",
)

meta_id = db.create_blocker(
    title="metadata row", detail="d", how_to="h", project="proj",
    repo="myrepo", branch="feature/x", cwd=CWD,
)
stored = db.get_blocker(meta_id)
check("repo persists", stored["repo"] == "myrepo")
check("branch persists", stored["branch"] == "feature/x")
check("created_at is recorded", bool(stored["created_at"]))

# A pid recorded by a hook must win over the ambient (possibly inherited) env var.
db.upsert_session(session_id="sess-pid-test", cwd=CWD, project="acme-audio", pid=4242)
_real_sid = context.session_id
context.session_id = lambda: "sess-pid-test"
try:
    check(
        "hook-recorded pid beats the inherited CLAUDE_PID",
        context.resolve_pid() == 4242,
        f"got {context.resolve_pid()}",
    )
finally:
    context.session_id = _real_sid


# --------------------------------------------------------------------------- #
print("\n== web api ==")
from claude_blockers import backend, web  # noqa: E402
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

httpd = ThreadingHTTPServer(("127.0.0.1", 4319), web.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = "http://127.0.0.1:4319"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return r.status, json.loads(r.read())


def post(path: str, payload: dict | None = None):
    req = urllib.request.Request(
        BASE + path, method="POST",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def post_any(path: str, payload: dict | None = None):
    """Like post(), but a refusal is the result rather than an exception.

    The relay is supposed to reject things, so its rejections have to be
    inspectable -- an assertion that a 400 was raised says nothing about whether
    the board refused for the right reason.
    """
    try:
        return post(path, payload)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


status, state = get("/api/state")
check("GET /api/state returns 200", status == 200)
check("state includes blockers", isinstance(state.get("blockers"), list))
check("state includes projects", isinstance(state.get("projects"), list))
check("state includes stats", "open" in state.get("stats", {}))
check("stats include resolved and dismissed counts",
      "resolved" in state["stats"] and "dismissed" in state["stats"])
check("state reports the live session count",
      isinstance(state.get("sessions", {}).get("live"), int), str(state.get("sessions")))
check("state includes the version for the status bar", bool(state.get("version")))

# The board is one page; if a static asset stops being served the UI is blank.
with urllib.request.urlopen(BASE + "/", timeout=5) as r:
    shell = r.read().decode()
for needed in ("side-list", "statusbar", "tabbar", "notify-toggle",
               "clear-btn", "clear-more", "clear-menu", "picker", "confirm-wrap"):
    check(f"shell markup keeps {needed}", needed in shell)

# "Everything runs on your machine ... no network calls" is a claim on the front
# of the README, and a board that reads project paths and transcript excerpts had
# better honour it. A webfont link is the easy way to break it by accident.
for offsite in ("//fonts.googleapis.com", "//fonts.gstatic.com", "//cdn.",
                "//unpkg.com", "//cdnjs."):
    check(f"the board calls out to nothing at {offsite}", offsite not in shell)

status, detail = get(f"/api/blocker/{mcp_id}")
check("GET a single blocker works", status == 200 and detail["id"] == mcp_id)
check("detail reports liveness", "live" in detail)
# The board renders the decision, so the answer fields have to reach the client.
for field in ("answer", "answered_at", "answered_by"):
    check(f"detail exposes {field}", field in detail)

status, ctx = get(f"/api/blocker/{mcp_id}/context")
check("context endpoint responds", status == 200 and "turns" in ctx)
check(
    "context reports whether a transcript exists at all",
    "transcript_found" in ctx and ctx["transcript_found"] is False,
    str(ctx)[:120],
)
check("a stale path with no session resolves to nothing",
      web._resolve_transcript("Z:/gone/nope.jsonl", None) is None)
# The real transcript of the session running this test is findable by id alone,
# even when the recorded path is wrong -- that is the re-resolution path.
if UNDER_CLAUDE:
    check(
        "a stale path re-resolves by session id",
        web._resolve_transcript("Z:/gone/nope.jsonl", context.session_id()) is not None,
        "this session has no transcript on disk",
    )
else:
    skip("a stale path re-resolves by session id", "no live session transcript to find")

# --------------------------------------------------------------------------- #
# The relay a sandboxed session uses. Its whole reason to exist is that a WSL or
# container session cannot open the database on the host, so the board has to
# accept the same operations over HTTP -- and must not accept anything else.
print("\n== relay (sandboxed sessions) ==")

check("a plain session is not in remote mode", backend.is_remote() is False)
check("describe names the local database", "local database" in backend.describe(),
      backend.describe())

status, rpc = post("/api/rpc", {"op": "stats", "kwargs": {}})
check("rpc runs a whitelisted op", status == 200 and "result" in rpc, str(rpc)[:120])
check("rpc stats matches the database directly",
      rpc["result"]["total"] == db.stats()["total"])

status, rpc = post("/api/rpc", {"op": "get_blocker", "kwargs": {"blocker_id": mcp_id}})
check("rpc reads one blocker back", rpc["result"]["id"] == mcp_id)

# The dispatch list is the entire remote surface. delete_by_status is a real
# function on db, which is exactly why it is worth proving it is unreachable.
for forbidden in ("delete_by_status", "connect", "init", "__import__"):
    _, rpc = post_any("/api/rpc", {"op": forbidden, "kwargs": {}})
    check(f"rpc refuses non-whitelisted op {forbidden!r}",
          "error" in rpc and "unknown op" in rpc["error"], str(rpc)[:100])

_, rpc = post_any("/api/rpc", {"op": "get_blocker", "kwargs": {"nonsense": 1}})
check("rpc rejects bad arguments without crashing",
      "error" in rpc and "bad arguments" in rpc["error"], str(rpc)[:100])

# Loopback is trusted, which is what keeps the browser working without a token.
check("loopback needs no token", get("/api/state")[0] == 200)

# Where a blocker ran, so a shared board stays readable.
host = context.host_label()
check("host label is non-empty", bool(host), repr(host))
# Rows written before the column existed read back as None rather than blowing
# up, which is the whole point of migrating in place instead of rebuilding.
check("every blocker exposes a host column", "host" in db.get_blocker(mcp_id))
check("a blocker raised without one reads back as None",
      db.get_blocker(mcp_id)["host"] is None, str(db.get_blocker(mcp_id)["host"]))

host_id = db.create_blocker(
    title="host stamped", detail=None, how_to=None, project="p", cwd="/tmp",
    host="WSL:Ubuntu-24.04",
)
check("a stamped host survives the round trip",
      db.get_blocker(host_id)["host"] == "WSL:Ubuntu-24.04")
check("the card shows where it ran",
      "WSL:Ubuntu-24.04" in render.blocker_card(db.get_blocker(host_id)))
db.delete_blocker(host_id)

status, _ = post("/api/seen", {"ids": [mcp_id]})
check("marking seen works", status == 200 and db.get_blocker(mcp_id)["seen"] == 1)

status, _ = post(f"/api/blocker/{mcp_id}/resolve", {"resolution": "tested"})
check("resolve endpoint works", db.get_blocker(mcp_id)["status"] == "resolved")

status, _ = post(f"/api/blocker/{mcp_id}/reopen")
check("reopen endpoint works", db.get_blocker(mcp_id)["status"] == "open")

# Clearing deletes rows outright, so the guarantee that matters is that an open
# blocker is never swept up by a request that did not name it.
keep_id = db.create_blocker(title="still open", detail="d", how_to="h",
                            project="clearing", cwd=CWD)
gone_id = db.create_blocker(title="dismissed one", detail="d", how_to="h",
                            project="clearing", cwd=CWD)
db.set_status(gone_id, "dismissed")
status, cleared = post("/api/clear")
check("clear endpoint responds", status == 200 and cleared["removed"] >= 1)
check("a dismissed blocker is deleted", db.get_blocker(gone_id) is None)
check("an open blocker survives the default clear", db.get_blocker(keep_id) is not None)
check("clearing again removes nothing", post("/api/clear")[1]["removed"] == 0)

try:
    post("/api/clear", {"statuses": ["everything"]})
    rejected = False
except urllib.error.HTTPError as exc:
    rejected = exc.code == 400
check("clear rejects a status it does not know", rejected)

# The wider scopes the board offers behind the caret. Only 'resolved' or
# 'dismissed' can be swept without touching anything live; 'open' has to be
# asked for by name, and then it really does go.
db.set_status(keep_id, "resolved")
check("a single closed status can be cleared on its own",
      post("/api/clear", {"statuses": ["resolved"]})[1]["removed"] >= 1
      and db.get_blocker(keep_id) is None)

wipe_id = db.create_blocker(title="open and doomed", detail="d", how_to="h",
                            project="clearing", cwd=CWD)
total_before = db.stats()["total"]
status, wiped = post("/api/clear", {"statuses": ["open", "resolved", "dismissed"]})
check("naming 'open' deletes open rows too",
      wiped["removed"] == total_before and db.get_blocker(wipe_id) is None)
check("the board is empty after clearing everything", db.stats()["total"] == 0)

with urllib.request.urlopen(BASE + "/", timeout=5) as r:
    body = r.read().decode()
check("index.html is served", "Claude Blockers" in body)
with urllib.request.urlopen(BASE + "/static/app.js", timeout=5) as r:
    app_js = r.read().decode()
    check("app.js is served", r.status == 200)
# Being able to hand a card to a fresh session is the point of the id, so the
# board has to offer that prompt rather than leave the user to compose it.
for needed in ("handoffPrompt", "read_blocker", "answer_blocker", "rid"):
    check(f"app.js keeps {needed}", needed in app_js)
with urllib.request.urlopen(BASE + "/static/style.css", timeout=5) as r:
    check("style.css is served", r.status == 200)

try:
    urllib.request.urlopen(BASE + "/static/../../../etc/passwd", timeout=5)
    traversal_blocked = False
except urllib.error.HTTPError as exc:
    traversal_blocked = exc.code in (403, 404)
except Exception:
    traversal_blocked = True
check("path traversal is blocked", traversal_blocked)

httpd.shutdown()


# --------------------------------------------------------------------------- #
print("\n== mcp server (stdio jsonrpc) ==")
def mcp_exchange(requests: list[dict], want_id: int, timeout: float = 45.0) -> dict | None:
    """Speak JSON-RPC to the server the way a real client does.

    Stdin is held open until the wanted response arrives. Closing it right after
    writing races the server's shutdown against its request handling, which made
    this test flaky -- Claude Code itself keeps the pipe open for the session's
    whole life, so holding it open is the accurate model.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_blockers", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        cwd=str(Path(__file__).resolve().parent.parent), env={**os.environ},
    )
    answer: dict | None = None
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == want_id:
                answer = message
                break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return answer


init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "smoke", "version": "1"}}}
ready_req = {"jsonrpc": "2.0", "method": "notifications/initialized"}

listed = mcp_exchange([init_req, ready_req, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], 2)
tool_names = [t["name"] for t in (listed or {}).get("result", {}).get("tools", [])]
check("MCP server lists its tools", bool(tool_names), f"got {listed}")
for expected_tool in ("raise_blocker", "resolve_blocker", "list_my_blockers", "board_status",
                      "read_blocker", "answer_blocker", "update_blocker", "delete_blocker"):
    check(f"exposes {expected_tool}", expected_tool in tool_names)

# Actually call raise_blocker over the wire and confirm the row lands in SQLite.
before = db.stats()["open"]
called = mcp_exchange([
    init_req, ready_req,
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "raise_blocker",
        "arguments": {
            "title": "Plug in the audio interface",
            "detail": "Cannot verify capture without hardware.",
            "how_to": "Plug it in, run `python -m recorder.check`.",
            "urgency": "high",
        }}},
], 3)
check("raise_blocker call succeeds", called is not None and "result" in (called or {}), f"got {called}")
check("raise_blocker actually writes a row", db.stats()["open"] == before + 1)
landed = [b for b in db.list_blockers(status="open") if b["title"] == "Plug in the audio interface"]
check("row carries the calling session id", bool(landed) and landed[0]["session_id"] == context.session_id())
check("row carries high urgency", bool(landed) and landed[0]["urgency"] == "high")

# The handoff, over the wire: read a blocker this session did not raise, then put
# the decision back on it.
if landed:
    handed = landed[0]["id"]
    read = mcp_exchange([
        init_req, ready_req,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "read_blocker", "arguments": {"blocker_id": handed}}},
    ], 4)
    read_text = json.dumps((read or {}).get("result", {}))
    check("read_blocker returns the card", f"Blocker #{handed}" in read_text, read_text[:160])
    check("the card carries how_to", "python -m recorder.check" in read_text)

    answered = mcp_exchange([
        init_req, ready_req,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "answer_blocker", "arguments": {
                "blocker_id": handed,
                "answer": "Interface was already plugged in; capture verified at -12 dBFS.",
            }}},
    ], 5)
    check("answer_blocker call succeeds", answered is not None and "result" in (answered or {}),
          f"got {answered}")
    row = db.get_blocker(handed)
    check("the answer lands on the row", (row["answer"] or "").startswith("Interface was already"))
    check("answering over the wire closes it", row["status"] == "resolved")
    check("the answering session is recorded", bool(row["answered_by"]))

    # A card that already carries someone's decision must survive delete_blocker:
    # the answer is not the deleting session's to throw away.
    refused = mcp_exchange([
        init_req, ready_req,
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "delete_blocker", "arguments": {"blocker_id": handed}}},
    ], 6)
    check("delete_blocker refuses an answered card",
          "Refusing" in json.dumps((refused or {}).get("result", {}))
          and db.get_blocker(handed) is not None,
          json.dumps(refused)[:200])

# Editing and deleting over the wire, on a card with no decision on it.
wire_id = db.create_blocker(title="Rough draft", detail="d", how_to="vague",
                            project="acme-audio", cwd=CWD, urgency="low")
db.mark_seen([wire_id])
edited_wire = mcp_exchange([
    init_req, ready_req,
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "update_blocker", "arguments": {
            "blocker_id": wire_id, "how_to": "1. Run the thing.", "urgency": "high"}}},
], 7)
wire_row = db.get_blocker(wire_id)
check("update_blocker call succeeds", edited_wire is not None and "result" in (edited_wire or {}),
      f"got {edited_wire}")
check("update_blocker rewrites over the wire",
      wire_row["how_to"] == "1. Run the thing." and wire_row["urgency"] == "high")
check("update_blocker leaves the title alone", wire_row["title"] == "Rough draft")
check("an edited card is unread again", wire_row["seen"] == 0)

deleted_wire = mcp_exchange([
    init_req, ready_req,
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
        "name": "delete_blocker", "arguments": {"blocker_id": wire_id}}},
], 8)
check("delete_blocker call succeeds", deleted_wire is not None and "result" in (deleted_wire or {}),
      f"got {deleted_wire}")
check("delete_blocker erases the row over the wire", db.get_blocker(wire_id) is None)

# --------------------------------------------------------------------------- #
print(f"\n{'=' * 58}")
summary = f"  {len(PASS)} passed, {len(FAIL)} failed"
if SKIP:
    summary += f", {len(SKIP)} skipped (not run under Claude Code)"
print(summary)
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{'=' * 58}\n")
sys.exit(1 if FAIL else 0)
