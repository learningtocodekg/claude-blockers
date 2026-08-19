"""Attacks a web page can make your browser perform against this board.

    uv run python tests/test_web_security.py

Drives a real socket rather than urllib, because the attacks are about what is
on the wire -- a body that is itself a second request, a header left off
entirely -- and a polite HTTP client will not send any of them.

WHAT WENT WRONG. The board trusts loopback: anything arriving from 127.0.0.1 is
you, so it needs no token. That is the right call for a single-user tool, and it
is why the requests a *hostile page* can make your browser send are the whole
attack surface. Three of them worked.

The board speaks HTTP/1.1 with keep-alive, and refused a request without ever
reading its body -- so the body stayed on the socket and was parsed as the next
request. A page on any origin could POST a body that was itself a complete HTTP
request, have the outer one refused by the Origin check, and have the inner one
run with full local privilege, carrying no Origin and arriving from 127.0.0.1.
It reached /api/clear, which empties the board including open cards, and /jump,
which starts a terminal on the host.

The Origin check itself only asked whether the origin *looked* numeric, never
whether it was this board. "null" -- what any page gets by running the fetch
inside a sandboxed iframe -- parsed to an empty hostname and passed. So did any
page served from a bare IP. And the Content-Type gate read `if ctype and ...`,
so a request with no Content-Type skipped it; a fetch of a Blob with an empty
type sends exactly that, as a simple request needing no preflight.

Each of those is one test below, phrased as the attack.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="claude-blockers-sec-"))
os.environ["CLAUDE_BLOCKERS_HOME"] = str(TMP)
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude")
os.environ.pop("CLAUDE_BLOCKERS_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_blockers import db, web  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


db.init()
db.create_blocker(title="canary", detail=None, how_to=None, project="p", cwd="/tmp")

# Port 0 lets the OS pick, so this never collides with a board someone is
# actually running.
httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

HOST = f"127.0.0.1:{PORT}".encode()


def raw(payload: bytes) -> bytes:
    """Send bytes, read whatever comes back. No client library in the way."""
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    sock.sendall(payload)
    time.sleep(0.5)
    out = b""
    try:
        sock.settimeout(2)
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            out += chunk
    except (socket.timeout, OSError):
        pass
    sock.close()
    return out


def open_count() -> int:
    return len(db.list_blockers(status="open"))


CLEAR = json.dumps({"statuses": ["open", "resolved", "dismissed"]}).encode()

# --------------------------------------------------------------------------- #
print("Smuggling a second request inside a refused one")

inner = (b"POST /api/clear HTTP/1.1\r\nHost: " + HOST + b"\r\n"
         b"Content-Type: application/json\r\n"
         b"Content-Length: " + str(len(CLEAR)).encode() + b"\r\n\r\n" + CLEAR)
outer = (b"POST / HTTP/1.1\r\nHost: evil.example\r\n"
         b"Content-Type: text/plain\r\n"
         b"Content-Length: " + str(len(inner)).encode() + b"\r\n\r\n" + inner)

before = open_count()
answer = raw(outer)
check("the outer request is refused", b"403" in answer, answer[:120].decode("latin-1"))
check("the smuggled request never runs", open_count() == before,
      f"open blockers went {before} -> {open_count()}")
check("and nothing answered it", answer.count(b"HTTP/1.1 200") == 0,
      answer[:300].decode("latin-1"))

# --------------------------------------------------------------------------- #
print("\nOrigins that are not this board")


def clear_with(headers: bytes) -> bytes:
    return raw(b"POST /api/clear HTTP/1.1\r\nHost: " + HOST + b"\r\n" + headers +
               b"Content-Length: " + str(len(CLEAR)).encode() +
               b"\r\nConnection: close\r\n\r\n" + CLEAR)


for label, headers in [
    ("Origin: null, which a sandboxed iframe produces",
     b"Origin: null\r\nContent-Type: application/json\r\n"),
    ("a page served from someone else's bare IP",
     b"Origin: http://198.51.100.7\r\nContent-Type: application/json\r\n"),
    ("a request with no Content-Type at all",
     b""),
]:
    before = open_count()
    answer = clear_with(headers)
    check(f"refused: {label}", b"403" in answer, answer[:150].decode("latin-1"))
    check("  and the board still has its cards", open_count() == before)

# --------------------------------------------------------------------------- #
print("\nA value that would take the board offline for good")

# pid lands in an INTEGER column, but SQLite keeps whatever it is handed, so a
# string used to survive the write and then fail a comparison in /api/state --
# the one route the UI polls. The board read that as "offline" and stayed there.
rpc = json.dumps({"op": "upsert_session",
                  "kwargs": {"session_id": "deadbeef", "pid": "notanint"}}).encode()
answer = raw(b"POST /api/rpc HTTP/1.1\r\nHost: " + HOST + b"\r\n"
             b"Content-Type: application/json\r\n"
             b"Content-Length: " + str(len(rpc)).encode() +
             b"\r\nConnection: close\r\n\r\n" + rpc)
check("a pid that is not a number is refused where it arrives",
      b"400" in answer, answer[:200].decode("latin-1"))

state = raw(b"GET /api/state HTTP/1.1\r\nHost: " + HOST +
            b"\r\nConnection: close\r\n\r\n")
check("so the board still answers afterwards", b"HTTP/1.1 200" in state,
      state[:200].decode("latin-1") or "<no response at all>")

# --------------------------------------------------------------------------- #
print("\nAnd the board still works for you")

state = raw(b"GET /api/state HTTP/1.1\r\nHost: " + HOST +
            b"\r\nConnection: close\r\n\r\n")
check("loopback still needs no token", b"HTTP/1.1 200" in state)
check("the page refuses to be framed", b"frame-ancestors" in state,
      state[:300].decode("latin-1"))

genuine = raw(b"POST /api/seen HTTP/1.1\r\nHost: " + HOST + b"\r\n"
              b"Origin: http://" + HOST + b"\r\n"
              b"Content-Type: application/json\r\nContent-Length: 12\r\n"
              b"Connection: close\r\n\r\n" + b'{"ids": [1]}')
check("the board's own POSTs are unaffected", b"HTTP/1.1 200" in genuine,
      genuine[:200].decode("latin-1"))

httpd.shutdown()

print(f"\n{'=' * 58}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{'=' * 58}\n")
sys.exit(1 if FAIL else 0)
