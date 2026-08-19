"""Where a session's blockers actually land.

Two sessions can be running the same code and still need different plumbing. A
session on the host opens the SQLite file directly, which is fast and needs no
server running. A session inside WSL, a devcontainer or Docker *cannot*: the
database lives on the host's filesystem, and a crossing filesystem does not
implement the locks SQLite relies on. Two sides writing at once do not queue up
-- the second one fails immediately with "disk I/O error", ignoring
busy_timeout, and the blocker is simply lost. That is measured behaviour, not
caution: every journal mode fails it the same way.

So a sandboxed session posts over HTTP to the board running on the host, and the
host process stays the only thing that ever opens the file. Everything above
this module calls the same names either way; only the transport differs.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config, db

# Operations a remote session is allowed to ask the board to perform. The board
# dispatches by name, so this list is the entire remote surface -- anything not
# named here is unreachable no matter what a caller sends.
REMOTE_OPS = (
    "create_blocker",
    "get_blocker",
    "list_blockers",
    "set_status",
    "update_blocker",
    "delete_blocker",
    "record_answer",
    "stats",
    "projects",
    # The hook receiver's vocabulary. Without these, a sandboxed session's hooks
    # would quietly write to a database inside the sandbox that nothing ever
    # reads -- the "stalled sessions get caught anyway" promise, silently broken
    # for exactly the sessions least likely to be noticed.
    "upsert_session",
    "get_session",
    "find_open_by_fingerprint",
    "touch_blocker",
    "resolve_session_hook_blockers",
)


class RemoteError(RuntimeError):
    """The board could not be reached, or refused the request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects rather than follow them.

    urllib copies request headers onto the redirected request, dropping only
    content-length and content-type -- so Authorization survives a hop to
    another host. A board that was impersonated, or a plain-HTTP hop that some
    middlebox rewrites, could answer 302 and be handed the token. There is no
    legitimate reason for a board to redirect this call, so not following one
    costs nothing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _numeric_url(url: str) -> str:
    """Swap a hostname in the board's URL for the address it resolves to now.

    The board refuses any request whose Host header is a *name* -- that is its
    DNS-rebinding defence, and it is worth keeping, because rebinding needs a
    name and cannot survive being required to use a number.

    But the address a sandbox has to use is not stable. WSL runs behind a NAT
    whose subnet is picked afresh every time the utility VM cold-starts, so the
    gateway is a different 172.x each time -- and a URL written into a config
    file with last week's number in it does not fail loudly, it fails at the
    moment someone needed to raise a blocker.

    The name is the stable thing, so the name is what gets configured, and it is
    resolved here instead: once per call, so a new address is picked up the first
    time it is used rather than at some point after a restart nobody connected to
    it. What travels is still a number, so the board's rule does not have to bend.

    Left alone for https, where the certificate is checked against the name, and
    for a name that will not resolve -- the connection error that follows names
    the host, which is more use than one naming an address nobody configured.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        return url
    try:
        ipaddress.ip_address(parsed.hostname)
        return url                      # already a number; nothing to do
    except ValueError:
        pass
    if parsed.hostname == "localhost":
        return url                      # the board accepts this one by name
    try:
        info = socket.getaddrinfo(parsed.hostname, parsed.port or 80,
                                  proto=socket.IPPROTO_TCP)
    except OSError:
        return url
    if not info:
        return url
    address = info[0][4][0]
    if ":" in address:                  # IPv6 literals are bracketed in a URL
        address = f"[{address}]"
    netloc = f"{address}:{parsed.port}" if parsed.port else address
    return urllib.parse.urlunsplit(parsed._replace(netloc=netloc))


def is_remote() -> bool:
    return config.remote_url() is not None


def describe() -> str:
    """One line for diagnostics: which board this process will write to."""
    url = config.remote_url()
    return f"remote board at {url}" if url else f"local database {config.db_path()}"


def _call(op: str, **kwargs: Any) -> Any:
    url = config.remote_url()
    assert url is not None
    # Errors below quote `url`, deliberately the configured one: an address this
    # resolved to is not what anyone would go and look for in a config file.
    target = _numeric_url(url)
    payload = json.dumps({"op": op, "kwargs": kwargs}).encode("utf-8")
    request = urllib.request.Request(
        target + "api/rpc",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    secret = config.token()
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")

    try:
        with _opener().open(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            raise RemoteError(
                f"the board at {url} rejected this session's token. Check that "
                f"CLAUDE_BLOCKERS_TOKEN matches the one the board printed when it "
                f"started ({detail})"
            ) from exc
        raise RemoteError(f"the board at {url} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RemoteError(
            f"cannot reach the board at {url} ({exc.reason}). Is `claude-blockers "
            f"serve --host 0.0.0.0` running on the host, and is that address "
            f"reachable from in here?"
        ) from exc

    if isinstance(body, dict) and "error" in body:
        raise RemoteError(str(body["error"]))
    return body.get("result") if isinstance(body, dict) else body


# --------------------------------------------------------------------------- #
# The surface everything above this module uses. Each one is either the local
# database call or the same call made over the wire.
# --------------------------------------------------------------------------- #

def init() -> None:
    # A remote session has no database of its own to create, and must not make
    # one -- an empty file appearing beside a sandbox is how you end up with a
    # second board nobody meant to start.
    if not is_remote():
        db.init()


# What a sandbox knows about itself that the host cannot use. A transcript path
# inside a container names a file the host cannot open; a pid inside a container
# belongs to a different process table, and checking it against the host's would
# badge a card "still running" on the strength of an unrelated process. Better to
# send neither and let the board show the truth: raised over there, not here.
_SANDBOX_LOCAL_FIELDS = ("transcript_path", "claude_pid")


def create_blocker(**kwargs: Any) -> int:
    if is_remote():
        kwargs = {k: v for k, v in kwargs.items() if k not in _SANDBOX_LOCAL_FIELDS}
        return _call("create_blocker", **kwargs)
    return db.create_blocker(**kwargs)


def get_blocker(blocker_id: int) -> dict[str, Any] | None:
    return _call("get_blocker", blocker_id=blocker_id) if is_remote() \
        else db.get_blocker(blocker_id)


def list_blockers(**kwargs: Any) -> list[dict[str, Any]]:
    return _call("list_blockers", **kwargs) if is_remote() else db.list_blockers(**kwargs)


def set_status(blocker_id: int, status: str, resolution: str | None = None) -> bool:
    if is_remote():
        return _call("set_status", blocker_id=blocker_id, status=status, resolution=resolution)
    return db.set_status(blocker_id, status, resolution)


def update_blocker(blocker_id: int, **fields: Any) -> list[str]:
    if is_remote():
        return _call("update_blocker", blocker_id=blocker_id, **fields)
    return db.update_blocker(blocker_id, **fields)


def delete_blocker(blocker_id: int) -> bool:
    return _call("delete_blocker", blocker_id=blocker_id) if is_remote() \
        else db.delete_blocker(blocker_id)


def record_answer(blocker_id: int, answer: str, **kwargs: Any) -> bool:
    if is_remote():
        return _call("record_answer", blocker_id=blocker_id, answer=answer, **kwargs)
    return db.record_answer(blocker_id, answer, **kwargs)


def stats() -> dict[str, Any]:
    return _call("stats") if is_remote() else db.stats()


def projects() -> list[dict[str, Any]]:
    return _call("projects") if is_remote() else db.projects()


# --------------------------------------------------------------------------- #
# Session bookkeeping, used by the hook receiver.
# --------------------------------------------------------------------------- #

def upsert_session(**kwargs: Any) -> None:
    if is_remote():
        # Same strip as create_blocker, and for the same reason -- but this one
        # was missing, and the hooks always send transcript_path. So every
        # SessionStart and every Notification from a sandboxed session was
        # rejected by the board with a 400, swallowed by the hook (which must
        # never wedge a session), and no card was ever created. The promise that
        # stalled sessions get caught anyway was quietly false for exactly the
        # sandboxed sessions the relay exists to serve.
        kwargs = {k: v for k, v in kwargs.items() if k not in _SANDBOX_LOCAL_FIELDS}
        return _call("upsert_session", **kwargs)
    return db.upsert_session(**kwargs)


def get_session(session_id: str) -> dict[str, Any] | None:
    return _call("get_session", session_id=session_id) if is_remote() \
        else db.get_session(session_id)


def find_open_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    if is_remote():
        return _call("find_open_by_fingerprint", fingerprint=fingerprint)
    return db.find_open_by_fingerprint(fingerprint)


def touch_blocker(blocker_id: int) -> None:
    return _call("touch_blocker", blocker_id=blocker_id) if is_remote() \
        else db.touch_blocker(blocker_id)


def resolve_session_hook_blockers(session_id: str) -> Any:
    if is_remote():
        return _call("resolve_session_hook_blockers", session_id=session_id)
    return db.resolve_session_hook_blockers(session_id)
