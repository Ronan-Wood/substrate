"""`substrate-mcp --connect`: a stdio face on the ONE shared engine, holding no stack.

Every stdio client spawns its own server, so N clients meant N engines. Each served whatever
commit was checked out when it started, and each probed Ollama for itself. One shared engine
serves them all (`substrate serve`, or the app that hosts it). This process is what a stdio client
spawns instead: every tool call goes to that engine, and nothing heavier than the stdlib lives here.

**The engine is trusted only once it is proven to be this user's** (`local_socket`). It listens
on a Unix socket in a 0700 directory, and before a byte of a query is written, this side checks
that directory and the uid of the process on the far end. It used to be 127.0.0.1:8765. Any
process could bind that while the engine was down and be sent every query.

**The writes (`ingest`, `amend`) are forwarded too, and the engine decides them.** A write carries
the client's name (`--client`, else its own `clientInfo.name`) and the grant token from
`~/.substrate/clients/<client>.token`, read on every write so a rotation reaches a running session.
An engine started `--writes=granted` runs it only for a client the user granted, and stamps the
note with that name (`substrate.grants`). With no grant, or no engine, nothing is written: this
process writes nothing itself, so a write is exactly as reachable as the engine's grants allow,
whatever host spawned this bridge. `--read-only` here means a write is neither listed nor sent.

Answered locally: `initialize` and `tools/list`. A client therefore connects and keeps its full tool
list even while no engine runs. Forwarded: every `tools/call`. Each one opens a fresh connection,
because the server closes a connection after it has been idle for a while, and an agent's next
call can arrive minutes later.
"""

from __future__ import annotations

import http.client
import json
import shlex
from pathlib import Path
from typing import Any

from substrate import grants, local_socket, wire

# One forwarded call, end to end. The shared server serializes every tool call on one lock, so this
# covers waiting behind other clients' calls as well as a wired `search` (HyDE plus a cross-encoder
# rerank, several seconds alone). It bounds a hung engine, and is not sized for a fast reply.
TIMEOUT_SECONDS = 300.0


def _tool_error(rid: Any, text: str) -> dict:
    """An isError RESULT, not a JSON-RPC error. This matches how every other tool fault comes back
    (`server._dispatch`): the model reads the condition and can tell the user, instead of getting
    a transport failure it cannot interpret."""
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": text}], "isError": True}}


class Remote:
    """Forwards one `tools/call` to the shared server and returns its response under OUR id."""

    def __init__(self, path: Path, *, timeout: float = TIMEOUT_SECONDS) -> None:
        self.path = Path(path)
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return str(self.path)

    @property
    def start_hint(self) -> str:
        """How a reader brings up an engine THIS client reaches, without naming whatever app may
        host it. The engine is a server any host can run, so a message that names one host is
        wrong for every other.

        Starting an engine on a client's own non-default path is never suggested. That path may be
        one a host still means to link to the default (an app's old socket path), and an engine
        started there holds it for life, which leaves two engines serving the same scopes. The
        exception is a link: an engine refuses to bind over one, so the hint names where the link
        points, shell-quoted."""
        default = local_socket.default_path()
        command = "`substrate-mcp --socket`, or the app that hosts it"
        endpoint = self.path.expanduser()
        target = local_socket.link_target(endpoint)
        if endpoint == default or target == default:
            return command
        if target is not None:
            return f"`substrate-mcp --socket {shlex.quote(str(target))}`, where this path links"
        return (f"{command}; this client is registered with {endpoint}, not the engine's default "
                f"{default}, so re-register it with `substrate-mcp --connect` and no path")

    def __call__(self, rid: Any, name: str, arguments: dict,
                 caller: grants.Caller | None = None) -> dict:
        """`caller` is set for a write: who it is from, sent as headers for the engine's gate. A
        missing token is sent as no token, and the engine refuses it in its own words."""
        frame = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}
        # ASCII-escaped: a lone surrogate is legal in a JSON string, and `encode("utf-8")` of the
        # unescaped form raises on it.
        body = json.dumps(frame).encode("ascii")
        try:
            status, raw = self._post(body, headers=_caller_headers(caller))
        except (OSError, http.client.HTTPException) as e:
            return self._unreachable(rid, name, e)
        return self._relay(rid, name, status, raw)

    def _post(self, body: bytes, *, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        """One POST to this user's engine, whatever the status. A refusal (the 503 at the
        connection ceiling, a guard's 4xx) carries a JSON-RPC body, and that body is the message
        worth relaying."""
        return local_socket.post(self.path, body, timeout=self.timeout, headers=headers)

    def _unreachable(self, rid: Any, name: str, reason: object) -> dict:
        if isinstance(reason, wire.WireMismatch):
            return self._other_release(rid, name, str(reason))
        if isinstance(reason, local_socket.UntrustedSocket):
            return _tool_error(rid, (
                f"`{name}` was NOT sent: the path to the substrate engine's socket, "
                f"{self.endpoint}, did not pass this user's trust checks ({reason}). Nothing was "
                f"sent, so no answer can be relayed. Make what that names this user's, or remove "
                f"it, then start the engine ({self.start_hint})."))
        # No socket file is the ordinary state while no engine is running; a refused connection is
        # a crashed engine's leftover. Both mean the same thing to the reader.
        if isinstance(reason, (ConnectionRefusedError, FileNotFoundError)):
            return _tool_error(rid, (
                f"`{name}` runs in the shared substrate engine, and nothing is listening on "
                f"{self.endpoint}. Start the engine ({self.start_hint}) and call again. "
                f"Nothing was written: the write tools run in the engine too."))
        if isinstance(reason, TimeoutError):
            return _tool_error(rid, (
                f"the shared substrate engine on {self.endpoint} did not answer `{name}` within "
                f"{self.timeout:.0f}s. It runs one call at a time for every client, so it may be "
                f"busy, or it may be hung. Whatever started the engine can restart it."))
        return _tool_error(rid, (
            f"the shared substrate engine on {self.endpoint} dropped `{name}`: "
            f"{type(reason).__name__}: {reason}"))

    def _other_release(self, rid: Any, name: str, why: str) -> dict:
        """The same fact from either side: this client's reply format is not the engine's."""
        return _tool_error(rid, (
            f"`{name}` was not answered in a format this client reads: {why}. This client and "
            f"the shared engine on {self.endpoint} come from releases that do not share a reply "
            f"format."))

    def _relay(self, rid: Any, name: str, status: int, raw: bytes) -> dict:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict) or not ("result" in payload or "error" in payload):
            return _tool_error(rid, (
                f"{self.endpoint} answered `{name}` with HTTP {status} and a body that is not a "
                f"JSON-RPC response. Something other than the substrate engine may hold the "
                f"socket."))
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code") == wire.MISMATCH:
            return self._other_release(rid, name, str(error.get("message")))
        # REWRITTEN, not trusted. Refusals made before the request is read (a 503 at the connection
        # ceiling, a guard's 4xx) carry `id: null`, and a client only resolves a pending call by
        # its own id. Passing null through would leave the call to hang until the client's timeout.
        payload["id"] = rid
        return payload




def _caller_headers(caller: grants.Caller | None) -> dict[str, str] | None:
    """The headers a forwarded write carries: who it is from, and its grant token when it has one.
    A name or info that will not fit a header is left out rather than sent mangled."""
    if caller is None:
        return None
    out: dict[str, str] = {}
    if caller.client:
        out[grants.CLIENT_HEADER] = caller.client
    if caller.token:
        out[grants.GRANT_HEADER] = caller.token
    if caller.info:
        info = json.dumps({k: v for k, v in caller.info.items() if k in ("name", "version")
                           and isinstance(v, str)}, ensure_ascii=True, separators=(",", ":"))
        if len(info) <= grants.MAX_HEADER_CHARS and info.isprintable():
            out[grants.CLIENT_INFO_HEADER] = info
    return out
