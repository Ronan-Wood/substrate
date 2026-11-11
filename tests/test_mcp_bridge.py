"""`--connect`: one shared server for every tool, the writes included, and none run here.

Each stdio client used to spawn a full engine of its own. The bridge replaces that with a process
that builds no stack and forwards every call to the shared engine on a Unix socket. These tests
check the properties the design rests on:
  * reads really are answered by the shared server;
  * a write is forwarded with who it is from, and never runs in the bridge;
  * a closed engine is an error the model can read, not a dead tool list, and writes nothing;
  * a listener that is not this user's engine is sent nothing, a grant token least of all.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate import grants, local_socket, stack, wire  # noqa: E402
from substrate.mcp import bridge, server  # noqa: E402
from test_local_socket import OTHER_UID, _engine, _patched, _socket_dir  # noqa: E402
from test_mcp_server import _NOTE, REPO, _call, _cfg, _fixture, _tmpdir  # noqa: E402


def _no_engine() -> Path:
    """A socket path in a private directory, with nothing bound: Scripta closed."""
    return Path(tempfile.mkdtemp(dir="/tmp")) / "substrate.sock"


def _bridge(registry: Path, remote) -> server.Config:
    return server.Config(str(registry), None, remote=remote)


def _tools_call(cfg: server.Config, name: str, args: dict, rid: int = 7) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, cfg)


class _Recorder:
    """A stand-in for `bridge.Remote` that records what reached the socket, and who each call said
    it was from."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.callers: list[grants.Caller | None] = []

    def __call__(self, rid, name, arguments, caller=None) -> dict:
        self.calls.append(name)
        self.callers.append(caller)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text",
                                                                     "text": "{}"}]}}


class _UnixStub(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _stub(handler_body, *, headers: list | None = None):
    """An HTTP listener on a fresh private socket that answers every POST with
    `handler_body(handler)`, stating this client's wire version as an engine would. Returns
    (server, socket path, the bodies it was sent). `headers` collects each request's headers."""
    seen: list[bytes] = []

    class H(http.server.BaseHTTPRequestHandler):
        def send_response(self, *a, **kw):
            super().send_response(*a, **kw)
            self.send_header(wire.HEADER, str(wire.VERSION))

        def do_POST(self):  # noqa: N802
            if headers is not None:
                headers.append(dict(self.headers))
            seen.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            handler_body(self)

        def log_message(self, *a):
            pass

    path = Path(tempfile.mkdtemp(dir="/tmp")) / "substrate.sock"
    httpd = _UnixStub(str(path), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, path, seen


def test_reads_are_answered_by_the_shared_server() -> None:
    _, registry = _fixture()
    with _socket_dir() as d, _engine(d / "s.sock", _cfg(registry)):
        cfg = _bridge(registry, bridge.Remote(d / "s.sock"))
        resp = _tools_call(cfg, "search", {"scope": "demo", "query": "composition"})
        assert resp["id"] == 7
        body = resp["result"]
        assert not body.get("isError"), body
        bridged = json.loads(body["content"][0]["text"])
        direct = _call("search", {"scope": "demo", "query": "composition"}, registry)
        assert bridged["passages"] and bridged["passages"] == direct["passages"]
        assert cfg.stack is None, "the bridge must not have answered from a stack of its own"


















def test_a_write_is_forwarded_with_who_it_is_from() -> None:
    """The bridge writes nothing itself: `ingest` and `amend`, plan and commit alike, go to the
    engine, each carrying the client's name and its token, read from the token file per call."""
    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    rec = _Recorder()
    cfg = _bridge(registry, rec)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"clientInfo": {"name": "Claude Code", "version": "9.9"}}}, cfg)
    args = {"scope": "demo", "content": _NOTE, "filename": "d.md"}
    out = _tools_call(cfg, "ingest", args)
    assert not out["result"].get("isError"), out
    assert rec.calls == ["ingest"]
    assert rec.callers[0] == grants.Caller("claude-code", None, {"name": "Claude Code",
                                                               "version": "9.9"})
    _, token_file = grants.issue("claude-code")
    _tools_call(cfg, "amend", {"scope": "demo", "path": "04-synthesis/d.md", "content": _NOTE})
    assert rec.calls == ["ingest", "amend"]
    assert rec.callers[1].token == token_file.read_text().strip()
    assert not (root / "demo-vault" / "04-synthesis" / "d.md").exists(), "the bridge wrote"
    _tools_call(cfg, "search", {"scope": "demo", "query": "x"})
    assert rec.callers[2] is None, "a read carries no grant token"


def test_client_overrides_the_declared_name() -> None:
    _, registry = _fixture()
    rec = _Recorder()
    cfg = _bridge(registry, rec)
    cfg.client = "zed-work"
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"clientInfo": {"name": "Zed"}}}, cfg)
    _tools_call(cfg, "ingest", {"scope": "demo", "content": _NOTE, "filename": "d.md"})
    assert rec.callers[0].client == "zed-work"


def test_a_write_with_no_engine_is_refused_and_writes_nothing() -> None:
    """It used to run in the bridge, and still worked with the engine down. Now the engine is the
    one place a write is decided, so no engine means no write, said as such."""
    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    cfg = _bridge(registry, bridge.Remote(_no_engine()))
    out = _tools_call(cfg, "ingest", {"scope": "demo", "content": _NOTE, "filename": "d.md"})
    assert out["result"]["isError"] is True, out
    assert "Nothing was written" in out["result"]["content"][0]["text"]
    assert not (root / "demo-vault" / "04-synthesis" / "d.md").exists()


def test_the_bridge_refuses_source_path_and_forwards_nothing() -> None:
    """It used to read the file and send it as `content`, so a model's path still became a read of
    any file the client could reach. Refused here too, not only in the engine: an engine from a
    release that still reads `source_path` would read it with the host app's file access."""
    _, registry = _fixture()
    rec = _Recorder()
    sent: list = []

    def _seen(rid, name, arguments, caller=None):
        sent.append(arguments)
        return rec(rid, name, arguments, caller)

    cfg = _bridge(registry, _seen)
    src = _tmpdir() / "from-disk.md"
    src.write_text(_NOTE + "\nPRIVATE LINE\n", encoding="utf-8")
    for args in ({"scope": "demo", "source_path": str(src)},
                 {"scope": "demo", "source_path": str(src), "content": _NOTE,
                  "filename": "d.md"}):
        out = _tools_call(cfg, "ingest", args)
        text = out["result"]["content"][0]["text"]
        assert out["result"]["isError"] and "pass its text as `content`" in text, (args, text)
        assert "PRIVATE LINE" not in text
    assert sent == [], "a source_path was forwarded"


def test_the_bridge_lists_every_tool_without_reaching_the_server() -> None:
    """A client reads the tool list once, at connect time. If the bridge needed the server for
    that, opening a session before Scripta would leave the session with no substrate tools."""
    _, registry = _fixture()
    rec = _Recorder()
    cfg = _bridge(registry, rec)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, cfg)
    assert init["result"]["serverInfo"]["name"] == "substrate"
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, cfg)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"search", "expand", "status", "documents", "list_scopes", "ingest"} <= names, names
    assert rec.calls == []


def test_local_refusals_happen_before_forwarding() -> None:
    _, registry = _fixture()
    rec = _Recorder()
    cfg = _bridge(registry, rec)
    assert "error" in _tools_call(cfg, "no_such_tool", {})
    ro = server.Config(str(registry), None, read_only=True, remote=rec)
    assert _tools_call(ro, "ingest", {"scope": "demo"})["result"]["isError"]
    assert rec.calls == []


def test_an_absent_engine_is_a_readable_tool_error() -> None:
    _, registry = _fixture()
    cfg = _bridge(registry, bridge.Remote(_no_engine()))
    resp = _tools_call(cfg, "search", {"scope": "demo", "query": "x"})
    assert resp["id"] == 7
    body = resp["result"]
    assert body["isError"] is True
    text = body["content"][0]["text"]
    assert "Start the engine" in text and "still works" not in text, text


def test_a_null_id_refusal_comes_back_under_the_callers_id() -> None:
    """The server's pre-read refusals (the 503 at its connection ceiling, the guards' 4xx)
    carry `id: null`. Relayed as they are, the client's call would never resolve."""
    def answer(h):
        out = json.dumps({"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32603, "message": "at its ceiling"}}).encode()
        h.send_response(503)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(out)))
        h.end_headers()
        h.wfile.write(out)

    httpd, path, _ = _stub(answer)
    try:
        r = bridge.Remote(path)(41, "search", {})
        assert r["id"] == 41 and r["error"]["message"] == "at its ceiling", r
    finally:
        httpd.shutdown()


def test_a_stranger_on_the_socket_is_named_not_relayed() -> None:
    def answer(h):
        out = b"<html>hello</html>"
        h.send_response(200)
        h.send_header("Content-Type", "text/html")
        h.send_header("Content-Length", str(len(out)))
        h.end_headers()
        h.wfile.write(out)

    httpd, path, _ = _stub(answer)
    try:
        r = bridge.Remote(path)(3, "search", {})
        assert r["id"] == 3 and r["result"]["isError"] is True
        assert "not a JSON-RPC response" in r["result"]["content"][0]["text"]
    finally:
        httpd.shutdown()


def test_a_hung_server_is_bounded() -> None:
    gate = threading.Event()

    def answer(h):
        gate.wait(5)

    httpd, path, _ = _stub(answer)
    try:
        t0 = time.monotonic()
        r = bridge.Remote(path, timeout=0.3)(4, "search", {})
        assert time.monotonic() - t0 < 3
        assert r["result"]["isError"] is True and "did not answer" in \
            r["result"]["content"][0]["text"]
    finally:
        gate.set()
        httpd.shutdown()


def test_main_connect_builds_no_stack_and_keeps_ingest() -> None:
    _, registry = _fixture()
    seen: dict = {}

    def _capture(cfg, *a, **kw):
        seen["cfg"] = cfg
        return 0

    def _refuse(**kw):
        raise AssertionError("--connect built a retrieval stack")

    real = server.serve, stack.build
    server.serve, stack.build = _capture, _refuse
    try:
        assert server.main(["--connect", "--registry", str(registry)]) == 0
        cfg = seen["cfg"]
        assert cfg.stack is None and cfg.read_only is False
        assert cfg.remote.path == local_socket.default_path()

        assert server.main(["--connect", "/tmp/elsewhere/s.sock", "--read-only",
                            "--registry", str(registry)]) == 0
        assert seen["cfg"].read_only is True
        assert seen["cfg"].remote.path == Path("/tmp/elsewhere/s.sock")
    finally:
        server.serve, stack.build = real


def test_main_connect_refuses_what_it_cannot_honour() -> None:
    real_err = sys.stderr
    sys.stderr = io.StringIO()
    try:
        # TCP is not a transport --connect offers, loopback included: whoever holds the port while
        # Scripta is closed would be sent every query.
        for spec in ("127.0.0.1:8765", "localhost:8765", "10.0.0.5:8765"):
            assert server.main(["--connect", spec]) == 2, spec
        assert "socket path" in sys.stderr.getvalue()
        assert server.main(["--connect", "--http"]) == 2
        assert server.main(["--connect", "--socket"]) == 2
        assert server.main(["--connect", "/tmp/" + "x" * 120 + "/s.sock"]) == 2
    finally:
        sys.stderr = real_err


def test_main_connect_refuses_agent_mode() -> None:
    """Agent mode narrows a server to snapshots, read-only. Under `--connect` it would be dropped
    without a word, and the client would get every scope the shared server holds plus `ingest`."""
    real_serve, real_err = server.serve, sys.stderr
    server.serve = lambda cfg: 0
    sys.stderr = io.StringIO()
    try:
        for extra in (["--agent-mode", "--scopes", "atlas", "--snapshot-dir", "/nonexistent"],
                      ["--scopes", "atlas"], ["--snapshot-dir", "/nonexistent"]):
            assert server.main(["--connect", *extra]) == 2, extra
        assert "--agent-mode" in sys.stderr.getvalue()
    finally:
        server.serve, sys.stderr = real_serve, real_err


def test_main_connect_says_which_flags_it_ignores() -> None:
    """The dev shim passes `--rerank-model cross`. Under --connect the shared server's reranker
    answers, so silently accepting the flag would read as a configuration that is not in force."""
    _, registry = _fixture()
    real_serve, real_err = server.serve, sys.stderr
    server.serve, sys.stderr = (lambda cfg, *a, **kw: 0), io.StringIO()
    try:
        assert server.main(["--connect", "--rerank-model", "cross",
                            "--registry", str(registry)]) == 0
        err = sys.stderr.getvalue()
    finally:
        server.serve, sys.stderr = real_serve, real_err
    assert "ignoring --rerank-model" in err, err


def test_the_bridge_process_exits_when_its_client_goes() -> None:
    """End of stdin is how a stdio client says it has gone. A bridge that outlived it would just
    be the pile-up again, one level down."""
    proc = subprocess.run(
        [sys.executable, "-m", "substrate.mcp.server", "--connect", str(_no_engine())],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
        capture_output=True, text=True, cwd=REPO, timeout=30)
    assert proc.returncode == 0, proc.stderr
    names = {t["name"] for t in json.loads(proc.stdout)["result"]["tools"]}
    assert "ingest" in names




def test_a_lone_surrogate_is_forwarded_not_fatal() -> None:
    """Legal JSON (`"\\ud800"`) that UTF-8 cannot encode. It used to raise out of the forward."""
    _, registry = _fixture()
    with _socket_dir() as d, _engine(d / "s.sock", _cfg(registry)):
        cfg = _bridge(registry, bridge.Remote(d / "s.sock"))
        resp = _tools_call(cfg, "search", {"scope": "demo", "query": "composition \ud800"})
        assert resp["id"] == 7 and "result" in resp, resp


def test_a_fault_in_the_forward_is_a_tool_fault() -> None:
    """Anything the forward raises comes back as an isError result under the caller's id, the
    same as a fault inside a tool, rather than escaping the dispatcher."""
    class _Broken(_Recorder):
        def __call__(self, rid, name, arguments, caller=None):
            raise RuntimeError("forward blew up")

    _, registry = _fixture()
    cfg = _bridge(registry, _Broken())
    for tool, args in (("search", {"scope": "demo", "query": "x"}),
                       ("ingest", {"scope": "demo", "content": _NOTE, "filename": "d.md"})):
        resp = _tools_call(cfg, tool, args)
        assert resp["id"] == 7 and resp["result"]["isError"] is True, resp
        assert "blew up" in resp["result"]["content"][0]["text"]


def _registry_answer(registry: str):
    """A stub body that answers every call as `list_scopes` reporting `registry`."""
    def answer(h):
        text = json.dumps({"scopes": [], "registry": registry})
        out = json.dumps({"jsonrpc": "2.0", "id": 0, "result": {
            "content": [{"type": "text", "text": text}]}}).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(out)))
        h.end_headers()
        h.wfile.write(out)
    return answer


def test_a_squatter_of_another_uid_is_sent_no_query() -> None:
    """The case loopback TCP lost: Scripta is closed and someone else holds the address. The query
    names a scope and carries the reader's own words, so the refusal has to come BEFORE the
    write. An answer is then never read, so none can reach the model as vault content."""
    _, registry = _fixture()
    httpd, path, seen = _stub(_registry_answer(str(registry)))
    try:
        cfg = _bridge(registry, bridge.Remote(path))
        with _patched(local_socket, "peer_uid", lambda sock: OTHER_UID):
            resp = _tools_call(cfg, "search", {"scope": "atlas", "query": "the private question"})
        assert resp["id"] == 7 and resp["result"]["isError"] is True, resp
        text = resp["result"]["content"][0]["text"]
        assert "NOT sent" in text and f"uid {OTHER_UID}" in text, text
        assert seen == [], f"the squatter was sent {seen}"
    finally:
        httpd.shutdown()




def test_a_squatter_of_another_uid_is_sent_no_grant_token() -> None:
    """The token is the one secret that crosses the socket, so it is checked for hardest: a
    listener that is not this user's never gets the request, its headers least of all."""
    _, registry = _fixture()
    grants.issue("claude-code")
    seen_headers: list = []
    httpd, path, seen = _stub(_registry_answer(str(registry)), headers=seen_headers)
    try:
        cfg = _bridge(registry, bridge.Remote(path))
        cfg.client = "claude-code"
        with _patched(local_socket, "peer_uid", lambda sock: OTHER_UID):
            resp = _tools_call(cfg, "ingest", {"scope": "demo", "content": _NOTE,
                                               "filename": "d.md"})
        assert resp["result"]["isError"] is True and "NOT sent" in \
            resp["result"]["content"][0]["text"], resp
        assert seen == [] and seen_headers == [], "the squatter was sent the write"
    finally:
        httpd.shutdown()


def test_a_socket_in_a_loose_directory_is_not_trusted() -> None:
    _, registry = _fixture()
    httpd, path, seen = _stub(_registry_answer(str(registry)))
    try:
        os.chmod(path.parent, 0o755)
        resp = _tools_call(_bridge(registry, bridge.Remote(path)), "search",
                           {"scope": "demo", "query": "x"})
        assert resp["result"]["isError"] is True
        assert "must be 700" in resp["result"]["content"][0]["text"]
        assert seen == []
    finally:
        httpd.shutdown()
