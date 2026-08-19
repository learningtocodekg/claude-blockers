"""The local board server.

Binds to 127.0.0.1 by default -- this reads your project paths and transcript
excerpts, and none of that should be reachable from the network.

It can be told to bind wider, because a session inside WSL or a container has no
other way to reach the board: the database sits on the host's filesystem, and a
crossing filesystem fails SQLite's locks outright rather than queueing, so a
sandboxed session has to post its blockers over HTTP. Binding wider therefore
demands a token, and every request from off-machine has to carry it. Requests
from this machine are trusted as before, so opening the board in your own
browser is unchanged.
"""

from __future__ import annotations

import hmac
import json
import re
import shutil
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, backend, config, db, jump as jump_mod

# Addresses that mean "this machine", and so need no token. A WSL session does
# not appear as one of these -- it arrives on the host's LAN-facing address --
# which is exactly the boundary the token is meant to sit on.
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})

# Columns a remote caller must never choose. These are not text the board
# displays -- they are what it acts on. `session_id` and `cwd` end up in the
# command line Jump runs; `transcript_path` is a file this host will open and
# hand back; `claude_pid` is a process it inspects. A sandboxed session posting a
# blocker has no business naming any of them, and the host fills them in for
# blockers it raises itself.
#
# `cwd` and `session_id` are deliberately NOT here. A sandboxed session has to be
# able to say which folder its work is in and which of its sessions raised the
# card, and both are shown on the board. `session_id` is validated by shape
# instead, because it is the one that reaches a command line.
UNSAFE_REMOTE_FIELDS = frozenset({
    "transcript_path", "claude_pid",
})

# How large a request body this server will read before giving up. Without a cap
# a single header claiming several gigabytes makes it try to allocate them.
MAX_BODY_BYTES = 1 << 20

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

BLOCKER_ROUTE = re.compile(r"^/api/blocker/(\d{1,12})(?:/(\w{1,32}))?$")


def _addressed_by_number(hostname: str) -> bool:
    """Was this request addressed by IP (or localhost) rather than by name?

    This is the DNS-rebinding check. That attack needs a *name*: a page on
    attacker.com whose DNS flips to 127.0.0.1 becomes same-origin with this board
    and can then read everything it serves. What it cannot do is make the browser
    send a Host header of anything other than that name -- so refusing named
    hosts closes it, while leaving every legitimate caller alone.

    Legitimate callers all address the board numerically: your browser on
    127.0.0.1, and a sandboxed session on whichever interface address reaches the
    host. An empty Host is fine too; only a real name is suspicious.
    """
    import ipaddress

    name = (hostname or "").strip().strip("[]").lower()
    if not name or name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _resolve_transcript(path_str: str | None, session_id: str | None) -> Path | None:
    """Locate a session's transcript, re-searching if the recorded path is stale.

    The path is worked out when the blocker is raised, but a transcript can
    appear later or move if the session changed directory, so a missing file is
    worth one more look by session id before giving up.

    Both lookups are confined to Claude Code's own transcript directory. The
    recorded path is a database column, and a database column is not a promise --
    a blocker can arrive from a sandboxed session over the relay, and this
    function decides which file the board will read and hand back. Left open, it
    answers "does this file exist" for any path on the machine and returns the
    contents of anything shaped like a transcript.
    """
    root = (config.claude_dir() / "projects").resolve()

    def inside(candidate: Path) -> Path | None:
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved != root and root not in resolved.parents:
            return None
        return resolved if resolved.is_file() else None

    if path_str:
        found = inside(Path(path_str))
        if found:
            return found
    # A session id reaches the filesystem through glob, so it has to be a plain
    # name -- `*/../../x.jsonl` is a perfectly good glob and would climb out.
    if session_id and jump_mod.valid_session_id(session_id):
        try:
            for match in root.glob(f"*/{session_id}.jsonl"):
                found = inside(match)
                if found:
                    return found
        except OSError:
            pass
    return None


def _transcript_tail(
    path_str: str | None, session_id: str | None = None, limit: int = 12
) -> list[dict[str, str]]:
    """Last few turns of a session, so the detail view can show what led here.

    Transcripts are JSONL and can be large, so this reads the tail bytes rather
    than the whole file.
    """
    path = _resolve_transcript(path_str, session_id)
    if path is None:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 262_144:
                fh.seek(size - 262_144)
                fh.readline()  # discard the partial line
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []

    turns: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        role = event.get("type")
        if role not in ("user", "assistant"):
            continue
        message = event.get("message") or {}
        content = message.get("content")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
        text = "\n".join(p for p in text_parts if p.strip())
        if not text.strip():
            continue
        turns.append({"role": role, "text": text.strip()[:4000]})
    return turns[-limit:]


class Handler(BaseHTTPRequestHandler):
    server_version = f"claude-blockers/{__version__}"
    protocol_version = "HTTP/1.1"
    # Without this a connection that stops mid-request pins a thread forever,
    # and no authorisation has happened yet at that point.
    timeout = 30

    def handle_one_request(self) -> None:
        """Answer one request, then make sure its body is off the socket.

        This is HTTP/1.1 with keep-alive, so anything left unread in rfile is
        parsed as the beginning of the *next* request. A page on any origin
        could POST a body that is itself a complete HTTP request -- CRLFs in a
        text/plain body are legal and need no preflight -- have the outer
        request refused by the Origin check, and have the smuggled one run with
        full local privilege, because it carries no Origin and arrives from
        127.0.0.1. That reached /api/clear and /jump, which starts a terminal.

        Draining here rather than at each refusal covers every path, including
        the ones that answer 404 or 403 without ever looking at a body.
        """
        self._body_consumed = False
        super().handle_one_request()
        if not self._body_consumed:
            self._drain()

    def _drain(self) -> None:
        """Read and discard a body we are not going to parse."""
        self._body_consumed = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, AttributeError):
            return
        remaining = min(max(length, 0), MAX_BODY_BYTES)
        if remaining and length > MAX_BODY_BYTES:
            # More than we will ever read, so the rest is still on the socket.
            self.close_connection = True
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(remaining, 65536))
            except OSError:
                return
            if not chunk:
                return
            remaining -= len(chunk)

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        self._body_consumed = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        # Trust the header only as far as the cap; otherwise a request claiming
        # several gigabytes gets this process to try to allocate them.
        length = min(length, MAX_BODY_BYTES)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _query(self) -> dict[str, str]:
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs
        raw = parse_qs(self.path.split("?", 1)[1])
        return {k: v[0] for k, v in raw.items()}

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass

    # ------------------------------------------------------------------- auth
    def _presented_token(self) -> str | None:
        """The token, from the header only.

        Deliberately not from `?token=`: a query string lands in browser history,
        the address bar, and every proxy log on the path, and this credential
        does not expire. The only caller that needs it is the relay, which sets a
        header.
        """
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        return None

    def _is_local(self) -> bool:
        return bool(self.client_address) and self.client_address[0] in LOOPBACK

    def _token_ok(self) -> bool:
        secret = config.token()
        presented = self._presented_token()
        if not secret or not presented:
            return False
        try:
            # compare_digest so a wrong token cannot be found one byte at a time
            return hmac.compare_digest(secret, presented)
        except TypeError:
            # Non-ASCII in the header raises rather than returning False.
            return False

    def _authorized(self, path: str) -> bool:
        """Anyone on this machine; anyone else only for /api/rpc, with the token.

        The board is one person's window onto their own machine, so a request
        from that machine needs no ceremony. A request that crossed a network is
        a different thing entirely, and the token is *not* a general pass: it
        buys access to the relay and nothing else.

        That distinction is the whole security model. The rest of this server
        reads transcript excerpts, lists every project path on the machine, and
        -- at /jump -- spawns a terminal. A sandboxed session needs none of that
        to post a blocker, so it does not get it. Granting the token holder every
        route would hand a container the ability to run commands on its host,
        which is the opposite of what a sandbox is for.
        """
        if self._is_local():
            return True
        return path == "/api/rpc" and self._token_ok()

    def _same_origin(self) -> bool:
        """Reject requests a browser was tricked into making.

        Two attacks, one check. DNS rebinding: a page on attacker.com whose DNS
        flips to 127.0.0.1 becomes same-origin with this board, and the browser
        will happily read every response -- unless the server notices the Host
        header still says attacker.com. And ordinary CSRF: a cross-origin form or
        simple fetch arrives with an Origin that is not ours.
        """
        host = (self.headers.get("Host") or "").strip()
        if host and not _addressed_by_number(host.rsplit(":", 1)[0]):
            return False

        origin = self.headers.get("Origin")
        if origin is not None:
            # Asking only whether the origin *looked* numeric let two things
            # through: "null", which any page gets by running the fetch inside
            # a sandboxed iframe and whose hostname parses as empty, and any
            # page served from a bare IP address. Neither has anything to do
            # with this board, so compare it against this board.
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            if not parsed.hostname or not _addressed_by_number(parsed.hostname):
                return False
            if origin.rstrip("/").lower() != f"http://{host}".rstrip("/").lower():
                return False
        return True

    def _deny(self, why: str = "unauthorized: this board requires a token from "
                               "off-machine callers, and the token reaches "
                               "/api/rpc only", status: int = 401) -> None:
        self._drain()
        self.close_connection = True
        self._json({"error": why}, status)

    # -------------------------------------------------------------------- rpc
    def _rpc(self) -> None:
        """Run one whitelisted board operation on behalf of a sandboxed session.

        This is the write path for a session that cannot open the database --
        inside WSL, a devcontainer, or on another machine entirely. It dispatches
        by name against a fixed list, so the operations reachable here are exactly
        backend.REMOTE_OPS and nothing a caller invents can widen it.

        The op name is not the only thing that needs gating, though. Several
        columns are not descriptions of a blocker but instructions to this host:
        `session_id` and `cwd` are interpolated into a command line when someone
        clicks Jump, and `transcript_path` names a file this server will open and
        return. A caller that could set those would be reaching out of its
        sandbox and back into the host, so it cannot set them.
        """
        body = self._body()
        op = body.get("op")
        kwargs = body.get("kwargs") or {}
        if op not in backend.REMOTE_OPS:
            return self._json({"error": f"unknown op: {op!r}"}, 400)
        if not isinstance(kwargs, dict):
            return self._json({"error": "kwargs must be an object"}, 400)

        # Presence is not the test -- value is. The MCP tools always pass these
        # keys, frequently as None, so rejecting the key would refuse the very
        # calls the relay exists to carry.
        rejected = sorted(f for f in UNSAFE_REMOTE_FIELDS if kwargs.get(f) is not None)
        if rejected:
            return self._json(
                {"error": f"these fields cannot be set remotely: {', '.join(rejected)}. "
                          f"They name a file this host would open, or a process it "
                          f"would inspect, and a sandbox's answer to either is not "
                          f"meaningful here."}, 400)

        # A session id is allowed through -- a sandboxed session should be able to
        # say which of its own sessions raised a blocker -- but it ends up on a
        # command line, so it has to look like an id and nothing else.
        sid = kwargs.get("session_id")
        if sid is not None and not jump_mod.valid_session_id(str(sid)):
            return self._json({"error": "session_id is not a well-formed session id"}, 400)

        # SQLite keeps whatever it is handed in an INTEGER column, so a string
        # survives the write and then fails a comparison in /api/state -- the
        # one route the board polls. The UI reads that as "offline" and stays
        # that way until someone edits the database by hand.
        if kwargs.get("pid") is not None:
            try:
                kwargs["pid"] = int(kwargs["pid"])
            except (TypeError, ValueError):
                return self._json({"error": "pid must be a number"}, 400)

        # None and "all" both legitimately mean "every status" to list_blockers.
        if "status" in kwargs and kwargs["status"] not in (None, "all", *db.STATUSES):
            return self._json({"error": f"unknown status: {kwargs['status']!r}"}, 400)
        try:
            result = getattr(db, op)(**kwargs)
        except TypeError as exc:            # wrong/missing arguments from a caller
            return self._json({"error": f"bad arguments for {op}: {exc}"}, 400)
        except Exception as exc:            # pragma: no cover - defensive
            return self._json({"error": f"{op} failed: {exc}"}, 500)
        return self._json({"result": result})

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._same_origin():
            return self._deny("forbidden: unexpected Host or Origin", 403)
        if not self._authorized(path):
            return self._deny()

        if path == "/" or path == "/index.html":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        if path == "/api/state":
            q = self._query()
            # A session row only records that nobody said it ended, so the pid is
            # what actually decides whether it is still alive.
            live = sum(
                1 for s in db.open_sessions()
                if jump_mod.pid_is_claude(s.get("pid"))
            )
            return self._json({
                "blockers": db.list_blockers(
                    status=q.get("status", "open"),
                    project=q.get("project"),
                ),
                "projects": db.projects(),
                "stats": db.stats(),
                "sessions": {"live": live},
                "version": __version__,
            })

        match = BLOCKER_ROUTE.match(path)
        if match and match.group(2) in (None, "context"):
            blocker = db.get_blocker(int(match.group(1)))
            if blocker is None:
                return self._json({"error": "not found"}, 404)
            if match.group(2) == "context":
                sid = blocker.get("session_id")
                turns = _transcript_tail(blocker.get("transcript_path"), sid)
                found = _resolve_transcript(blocker.get("transcript_path"), sid) is not None
                return self._json({
                    "turns": turns,
                    # Distinguish "no transcript exists" from "transcript exists
                    # but held nothing readable" -- they need different answers.
                    "transcript_found": found,
                    "session_id": sid,
                })
            blocker["live"] = jump_mod.pid_is_claude(blocker.get("claude_pid"))
            blocker["resume_command"] = (
                jump_mod.resume_command(blocker["session_id"])
                if blocker.get("session_id") else None
            )
            return self._json(blocker)

        self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._same_origin():
            return self._deny("forbidden: unexpected Host or Origin", 403)
        # Requiring JSON is a CSRF control, not pedantry: a cross-origin fetch can
        # only send text/plain, form or multipart without triggering a preflight
        # this server never answers, so insisting on application/json means a
        # hostile page cannot reach any of this with a simple request.
        # `if ctype and ...` allowed a request with NO Content-Type, and a fetch
        # of a Blob with an empty type sends exactly that, as a simple request
        # with no preflight. Absent is not the same as permitted.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._deny(
                "forbidden: expected application/json, got "
                f"{ctype or 'no Content-Type'}", 403)
        if not self._authorized(path):
            return self._deny()

        if path == "/api/rpc":
            return self._rpc()

        if path == "/api/seen":
            ids = [int(i) for i in self._body().get("ids", []) if str(i).isdigit()]
            db.mark_seen(ids)
            return self._json({"ok": True, "count": len(ids)})

        if path == "/api/clear":
            # Defaults to both closed statuses. Open rows are deletable, but only
            # for a request that names 'open' outright -- the board asks before
            # sending one, and no default here can reach them.
            statuses = self._body().get("statuses") or ["resolved", "dismissed"]
            if not isinstance(statuses, list):
                return self._json({"error": "statuses must be a list"}, 400)
            unknown = [s for s in statuses if s not in db.CLEARABLE]
            if unknown:
                return self._json({"error": f"unknown status: {unknown[0]}"}, 400)
            return self._json({"ok": True, "removed": db.delete_by_status(statuses)})

        match = BLOCKER_ROUTE.match(path)
        if not match:
            return self._json({"error": "not found"}, 404)

        blocker_id, action = int(match.group(1)), match.group(2)
        blocker = db.get_blocker(blocker_id)
        if blocker is None:
            return self._json({"error": "not found"}, 404)

        if action == "resolve":
            db.set_status(blocker_id, "resolved", self._body().get("resolution"))
            return self._json({"ok": True})
        if action == "dismiss":
            db.set_status(blocker_id, "dismissed", self._body().get("resolution"))
            return self._json({"ok": True})
        if action == "reopen":
            db.set_status(blocker_id, "open")
            return self._json({"ok": True})
        if action == "jump":
            return self._json(jump_mod.jump(blocker))

        self._json({"error": "unknown action"}, 400)

    # ---------------------------------------------------------------- static
    def _static(self, relative: str) -> None:
        root = config.static_dir().resolve()
        target = (root / relative).resolve()
        if root not in target.parents and target != root:
            return self._json({"error": "forbidden"}, 403)
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(200, target.read_bytes(),
                   STATIC_TYPES.get(target.suffix, "application/octet-stream"))


def _open_board(url: str) -> None:
    """Show the board, from wherever this is running.

    Inside WSL there is usually no browser on the Linux side, and
    `webbrowser.open` reports success having opened nothing -- so the board comes
    up and never appears. wslview or explorer.exe carry the URL across to the
    Windows browser instead. If interop is off there is no way across and the
    printed URL is the answer.
    """
    if config.is_wsl():
        for tool in ("wslview", "explorer.exe"):
            path = shutil.which(tool)
            if not path:
                continue
            try:
                subprocess.Popen([path, url], close_fds=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
        return  # interop off: the URL is already printed above
    webbrowser.open(url)


def _already_answering(host: str, port: int) -> bool:
    """Is something already serving here?

    Worth asking before binding, because on Windows the answer can be yes and the
    bind can still succeed. WSL forwards the distro's localhost onto this side, so
    a board running inside WSL holds 127.0.0.1:4317 here too; HTTPServer sets
    allow_reuse_address, Windows honours it, and your board starts, prints its URL
    and is never the thing that answers there. You then read someone else's
    blockers believing they are yours -- which is precisely how this was found.
    """
    import socket
    probe = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((probe, port), timeout=0.4):
            return True
    except OSError:
        return False


def _free_port(start: int, host: str) -> int:
    """The next port nothing is sitting on, so the advice we print actually works."""
    for candidate in range(start + 1, start + 20):
        if not _already_answering(host, candidate):
            return candidate
    return start + 1


def serve(port: int | None = None, open_browser: bool = True,
          host: str = "127.0.0.1") -> None:
    # In a sandbox pointed at a board elsewhere there is nothing here to serve.
    # Starting anyway would open a second, empty board on a port the real one may
    # already be forwarding to -- the confusion this command works hardest to
    # avoid.
    if backend.is_remote():
        print(f"This install posts to the board at {config.remote_url()}.")
        print("  There is no database here to serve. Open that address instead,")
        print("  or run `claude-blockers serve` on the machine hosting it.")
        raise SystemExit(1)

    db.init()
    bind_port = port or config.port()
    exposed = host not in LOOPBACK

    if _already_answering(host, bind_port):
        spare = _free_port(bind_port, host)
        print(f"Something is already serving {host}:{bind_port}.")
        print("  Refusing to start a second board there: on Windows the bind can")
        print("  succeed anyway and the other one keeps answering, so you would be")
        print("  reading its blockers and believing they were this board's.")
        print("  A board running inside WSL is the usual culprit -- WSL forwards the")
        print("  distro's localhost onto this side.")
        print(f"  Open http://127.0.0.1:{bind_port}/ to see whose board that is, or")
        print(f"  give this one its own port:  claude-blockers serve --port {spare}")
        raise SystemExit(1)

    # Binding wider than loopback publishes project paths and transcript
    # excerpts, so it is not something to do accidentally. Mint the token here
    # rather than refusing to start: the reason anyone passes --host is that a
    # sandboxed session needs in, and a board that will not start does not help
    # them.
    secret = config.ensure_token() if exposed else None

    try:
        httpd = ThreadingHTTPServer((host, bind_port), Handler)
    except OSError as exc:
        # The case where the OS does refuse. Without this it was a raw traceback.
        spare = _free_port(bind_port, host)
        print(f"Could not bind {host}:{bind_port} -- {exc}")
        print(f"  Give this board its own port:  claude-blockers serve --port {spare}")
        raise SystemExit(1)

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{bind_port}/"
    print(f"claude-blockers serving {url}")
    print(f"  database: {config.db_path()}")
    if exposed:
        print(f"  bound to {host} -- reachable beyond this machine.")
        print("  Off-machine callers must present this token:")
        print(f"    CLAUDE_BLOCKERS_TOKEN={secret}")
        print("  Point a sandboxed session at it with:")
        # --token=, not --token: a token can begin with a dash, and argparse
        # reads that as the start of another flag rather than as this one's
        # value. Newly minted ones no longer can, but the ones already on disk
        # from before that fix still do.
        print(f"    claude-blockers install --remote http://<host-address>:{bind_port}/ "
              f"--token={secret}")
        # Naming the host beats numbering it: WSL's gateway is reassigned every
        # time its VM cold-starts, and a relay pointed at the old number does not
        # fail until someone needed it.
        print("    from WSL, <host-address> is best written $(hostname).mshome.net,")
        print("    which keeps working after a restart when the address does not")
    print("  Ctrl-C to stop.")
    if open_browser:
        _open_board(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
