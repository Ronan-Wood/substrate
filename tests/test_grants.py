"""Consented writes: a shared engine runs `ingest` and `amend` only for a client the user granted.

Every test here drives the real path where it can: a `--connect` bridge (`server.Config` with a
`bridge.Remote`) in front of a real engine on a Unix socket (`serve_unix`, `writes="granted"`), with
the grant store in the suite's own engine home (`conftest`). What each test pins:

  * the refusals: no grant, a revoked one, a wrong or rotated token, a scope outside the grant,
    each recorded as a request the host can show;
  * a granted write lands, stamped, and the stamp is in what the plan bound;
  * a note cannot claim another author;
  * a plan is bound to the client's grant token;
  * `decide` against a table over generated inputs (the differential test the PR names);
  * the CLI, the files' modes, the `server` block and `main`'s flags.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import random
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate import cli, grants, notes, stack  # noqa: E402
from substrate.mcp import bridge, server  # noqa: E402
from test_local_socket import _engine, _socket_dir  # noqa: E402
from test_mcp_server import _NOTE, _fixture  # noqa: E402

_ARGS = {"scope": "demo", "content": _NOTE, "filename": "d.md"}


def _engine_cfg(registry: Path, writes: str = "granted") -> server.Config:
    return server.Config(str(registry), stack.build(lexical_only=True), writes=writes)


def _bridge(registry: Path, path: Path, client: str | None = "claude-code") -> server.Config:
    cfg = server.Config(str(registry), None, remote=bridge.Remote(path))
    cfg.client = client
    return cfg


def _call(cfg: server.Config, tool: str, args: dict) -> tuple[bool, dict | str]:
    """(isError, the payload, or the refusal text)."""
    out = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}}, cfg)
    assert out["id"] == 9, out
    text = out["result"]["content"][0]["text"]
    if out["result"].get("isError"):
        return True, text
    return False, json.loads(text)


def _plan_and_commit(cfg: server.Config, tool: str, args: dict) -> tuple[dict, dict | str]:
    err, plan = _call(cfg, tool, args)
    assert not err, plan
    err, out = _call(cfg, tool, {**args, "confirm_token": plan["plan"]["confirm_token"]})
    return plan, (out if not err else f"REFUSED: {out}")


@contextlib.contextmanager
def _world(*, writes: str = "granted"):
    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    with _socket_dir() as d, _engine(d / "s.sock", _engine_cfg(registry, writes)):
        yield root / "demo-vault" / "04-synthesis", registry, d / "s.sock"


def _requests() -> list[str]:
    """Who a host lists as asking: requested and not granted."""
    return [r["client"] for r in grants.listing()["requested"]]


def _recorded() -> list[str]:
    """Every refused write recorded, granted clients included."""
    return [r["client"] for r in grants._requests()]


# --------------------------------------------------------------------------- refusals

def test_an_ungranted_client_is_refused_and_recorded() -> None:
    with _world() as (folder, registry, sock):
        err, text = _call(_bridge(registry, sock), "ingest", _ARGS)
    assert err and "client `claude-code` has no grant" in text, text
    assert "substrate grant claude-code" in text, text
    assert not (folder / "d.md").exists()
    assert _requests() == ["claude-code"]


def test_a_revoked_client_is_refused_at_its_next_write() -> None:
    with _world() as (folder, registry, sock):
        grants.issue("claude-code")
        cfg = _bridge(registry, sock)
        err, plan = _call(cfg, "ingest", _ARGS)
        assert not err, plan
        grants.revoke("claude-code")
        err, text = _call(cfg, "ingest", {**_ARGS, "confirm_token": plan["plan"]["confirm_token"]})
    assert err and "no grant" in text, text
    assert not (folder / "d.md").exists()
    assert _requests() == ["claude-code"]


def test_a_wrong_token_is_refused() -> None:
    with _world() as (folder, registry, sock):
        _, path = grants.issue("claude-code")
        path.write_text("0" * 64 + "\n")
        err, text = _call(_bridge(registry, sock), "ingest", _ARGS)
    assert err and "did not present its current grant token" in text, text
    assert _recorded() == ["claude-code"]


def test_a_missing_token_is_refused() -> None:
    with _world() as (_, registry, sock):
        _, path = grants.issue("claude-code")
        path.unlink()
        err, text = _call(_bridge(registry, sock), "ingest", _ARGS)
    assert err and "did not present its current grant token" in text, text


def test_a_rotation_kills_the_old_token_and_its_plans() -> None:
    """Re-granting rotates: the old token stops writing, and so does a plan made with it, even
    when the client then presents the new token."""
    with _world() as (folder, registry, sock):
        _, path = grants.issue("claude-code")
        old = path.read_text()
        cfg = _bridge(registry, sock)
        err, plan = _call(cfg, "ingest", _ARGS)
        assert not err, plan
        grant, _ = grants.issue("claude-code")
        assert grant.generation == 2
        err, text = _call(cfg, "ingest", {**_ARGS, "confirm_token": plan["plan"]["confirm_token"]})
        assert err and "confirm_token was not issued" in text, text
        path.write_text(old)
        err, text = _call(cfg, "ingest", _ARGS)
        assert err and "current grant token" in text, text
    assert not (folder / "d.md").exists()


def test_a_revoke_then_a_new_grant_kills_the_plans_from_before() -> None:
    """A revoke deletes the grant, so the next grant starts at generation 1 again. The plans made
    under the first grant must not come back with it."""
    with _world() as (folder, registry, sock):
        grants.issue("claude-code")
        cfg = _bridge(registry, sock)
        err, plan = _call(cfg, "ingest", _ARGS)
        assert not err, plan
        grants.revoke("claude-code")
        grant, _ = grants.issue("claude-code")
        assert grant.generation == 1
        err, text = _call(cfg, "ingest", {**_ARGS, "confirm_token": plan["plan"]["confirm_token"]})
    assert err and "confirm_token was not issued" in text, text
    assert not (folder / "d.md").exists()


def test_a_scope_outside_the_grant_is_refused() -> None:
    with _world() as (folder, registry, sock):
        grants.issue("claude-code", ["atlas"])
        err, text = _call(_bridge(registry, sock), "ingest", _ARGS)
    assert err and "granted only atlas, not scope 'demo'" in text, text
    assert _recorded() == ["claude-code"]
    assert _requests() == [], "a granted client is not listed as asking"


def test_a_client_that_names_itself_nothing_is_refused_and_not_recorded() -> None:
    with _world() as (_, registry, sock):
        err, text = _call(_bridge(registry, sock, client=None), "ingest", _ARGS)
    assert err and "named no client" in text and "--client" in text, text
    assert _requests() == []


def test_a_plan_cannot_be_redeemed_by_another_client() -> None:
    """The engine's one PlanBook is shared by every client, so a token alone must not carry a
    write across clients."""
    with _world() as (folder, registry, sock):
        grants.issue("claude-code")
        grants.issue("claude-ai")
        err, plan = _call(_bridge(registry, sock, "claude-code"), "ingest", _ARGS)
        assert not err, plan
        token = plan["plan"]["confirm_token"]
        err, text = _call(_bridge(registry, sock, "claude-ai"), "ingest",
                          {**_ARGS, "confirm_token": token})
    assert err and "confirm_token was not issued" in text, text
    assert not (folder / "d.md").exists()


def test_a_read_only_engine_lists_and_refuses_as_before() -> None:
    with _world(writes="none") as (folder, registry, sock):
        grants.issue("claude-code")
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               _engine_cfg(registry, "none"))
        err, text = _call(_bridge(registry, sock), "ingest", _ARGS)
    assert "ingest" not in {t["name"] for t in listed["result"]["tools"]}
    assert err and "read-only" in text, text
    assert not (folder / "d.md").exists()
    assert _requests() == [], "a read-only engine records no requests"


def test_a_granted_engine_lists_the_write_tools() -> None:
    _, registry = _fixture()
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           _engine_cfg(registry, "granted"))
    assert {"ingest", "amend"} <= {t["name"] for t in listed["result"]["tools"]}


def test_a_refused_amend_burns_its_token() -> None:
    from test_mcp_server import _AMENDED

    with _world() as (folder, registry, sock):
        (folder / "d.md").write_text(_NOTE, encoding="utf-8")
        grants.issue("claude-code")
        cfg = _bridge(registry, sock)
        args = {"scope": "demo", "path": "04-synthesis/d.md", "content": _AMENDED}
        err, plan = _call(cfg, "amend", args)
        assert not err, plan
        token = plan["plan"]["confirm_token"]
        grants.revoke("claude-code")
        assert _call(cfg, "amend", {**args, "confirm_token": token})[0]
        grants.issue("claude-code")
        err, text = _call(cfg, "amend", {**args, "confirm_token": token})
    assert err and "not issued" in text, text
    assert (folder / "d.md").read_text(encoding="utf-8") == _NOTE


# --------------------------------------------------------------------------- granted writes

def test_a_granted_ingest_lands_stamped() -> None:
    with _world() as (folder, registry, sock):
        grants.issue("claude-code")
        _, out = _plan_and_commit(_bridge(registry, sock), "ingest", _ARGS)
    assert isinstance(out, dict) and out["written"] is True, out
    text = (folder / "d.md").read_text(encoding="utf-8")
    assert text.startswith("---\nwritten_by: claude-code\nstatus: active\n"), text
    assert text.endswith(_NOTE.split("---\n", 2)[2])
    assert _requests() == []


def test_a_granted_amend_is_stamped_in_the_diff_it_was_planned_with() -> None:
    from test_mcp_server import _AMENDED

    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE, encoding="utf-8")
        grants.issue("claude-code")
        args = {"scope": "demo", "path": "04-synthesis/d.md", "content": _AMENDED}
        plan, out = _plan_and_commit(_bridge(registry, sock), "amend", args)
    assert "+amended_by: claude-code" in plan["plan"]["diff"], plan["plan"]["diff"]
    assert isinstance(out, dict) and out["sha256"] == plan["plan"]["new_sha256"], out
    import hashlib

    assert hashlib.sha256(note.read_bytes()).hexdigest() == plan["plan"]["new_sha256"]
    assert "amended_by: claude-code" in note.read_text(encoding="utf-8")


def test_a_second_clients_amend_replaces_the_stamp_in_place() -> None:
    from test_mcp_server import _AMENDED

    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE.replace("status: active\n", "status: active\namended_by: zed\n"),
                        encoding="utf-8")
        grants.issue("claude-code")
        sent = _AMENDED.replace("status: active\n", "status: active\namended_by: zed\n")
        plan, out = _plan_and_commit(_bridge(registry, sock), "amend",
                                     {"scope": "demo", "path": "04-synthesis/d.md",
                                      "content": sent})
    assert isinstance(out, dict), out
    assert "-amended_by: zed\n+amended_by: claude-code" in plan["plan"]["diff"]
    assert "\nstatus: active\namended_by: claude-code\n" in note.read_text(encoding="utf-8")


def test_the_engine_reads_no_source_path_for_a_granted_client() -> None:
    """Its file access is its host's, not the client's; the bridge sends the file as content."""
    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    src = root / "outside.md"
    src.write_text(_NOTE, encoding="utf-8")
    author = grants.Decision(True, client="claude-code", generation=1, token_sha256="0" * 64)
    with pytest.raises(server.ToolError, match="does not read `source_path`"):
        server._tool_ingest({"scope": "demo", "source_path": str(src)},
                            _engine_cfg(registry), author)


def test_a_forged_written_by_is_refused() -> None:
    with _world() as (folder, registry, sock):
        grants.issue("claude-code")
        forged = _NOTE.replace("status: active\n", "status: active\nwritten_by: the-user\n")
        err, text = _call(_bridge(registry, sock), "ingest", {**_ARGS, "content": forged})
    assert err and "`written_by: the-user`" in text and "Leave the" in text, text
    assert not (folder / "d.md").exists()


def test_an_amend_cannot_change_who_wrote_the_note() -> None:
    from test_mcp_server import _AMENDED

    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE.replace("status: active\n", "status: active\nwritten_by: zed\n"),
                        encoding="utf-8")
        grants.issue("claude-code")
        cfg = _bridge(registry, sock)
        args = {"scope": "demo", "path": "04-synthesis/d.md"}
        err, text = _call(cfg, "amend", {**args, "content": _AMENDED})
        assert err and "keep `written_by: zed`" in text, text
        err, text = _call(cfg, "amend", {**args, "content": _AMENDED.replace(
            "status: active\n", "status: active\nwritten_by: zed\namended_by: someone\n")})
        assert err and "`amended_by: someone`" in text, text


def test_the_owner_is_never_stamped() -> None:
    """Stdio, the client that spawned the process: as before this change, byte for byte."""
    from test_mcp_server import _call as _stdio_call
    from test_mcp_server import _cfg

    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    cfg = _cfg(registry)
    plan = _stdio_call("ingest", _ARGS, registry, cfg)
    _stdio_call("ingest", {**_ARGS, "confirm_token": plan["plan"]["confirm_token"]}, registry, cfg)
    assert (root / "demo-vault" / "04-synthesis" / "d.md").read_text(encoding="utf-8") == _NOTE


def test_a_write_asks_the_refresh_loop_for_a_pass() -> None:
    class _Loop:
        asked = 0

        def request(self):
            self.asked += 1
            return "after_gap", 4.2

    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    cfg = _engine_cfg(registry, "open")
    cfg.refresh_loop = loop = _Loop()
    _, out = _plan_and_commit(cfg, "ingest", _ARGS)
    assert loop.asked == 1
    assert "coming refresh pass, which starts in about 5 s" in out["next"], out


# --------------------------------------------------------------------------- stamping

@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_the_stamp_keeps_the_notes_line_endings(eol: str) -> None:
    content = _NOTE.replace("\n", eol).encode()
    stamped = notes.stamp_new(content, "claude-code")
    assert stamped == f"---{eol}written_by: claude-code{eol}".encode() + content[3 + len(eol):]


def test_a_name_yaml_would_read_as_something_else_is_quoted() -> None:
    stamped = notes.stamp_new(_NOTE.encode(), "true")
    assert stamped.startswith(b"---\nwritten_by: 'true'\n"), stamped


def test_a_first_key_that_only_starts_with_dashes_is_stamped() -> None:
    """The stamp used to be refused here: the old fence closed at any line starting with `---`."""
    content = b"---\n---title: x\nstatus: active\n---\nbody\n"
    assert notes.stamp_new(content, "claude-code") == (
        b"---\nwritten_by: claude-code\n---title: x\nstatus: active\n---\nbody\n")


def test_a_block_the_stamp_cannot_enter_is_refused() -> None:
    content = b"---\n---\nstatus: active\n---\nbody\n"
    with pytest.raises(notes.NoteError, match="cannot put that line"):
        notes.stamp_new(content, "claude-code")
    with pytest.raises(notes.NoteError, match="no frontmatter block"):
        notes.stamp_new(b"# no frontmatter\n", "claude-code")


# --------------------------------------------------------------------------- decide, differentially

def _reference(mode, client, token, scope, grant) -> bool:
    """The rule, as a table a reader can check: what may write."""
    if mode == "open":
        return True
    if mode != "granted" or client is None or grant is None:
        return False
    if not grants.CLIENT_NAME.fullmatch(client):
        return False
    return (token is not None and grants.token_sha256(token) == grant.token_sha256
            and ("*" in grant.scopes or scope in grant.scopes))


def test_decide_agrees_with_the_table_over_generated_inputs() -> None:
    rng = random.Random(20261110)
    tokens = ["a" * 64, "b" * 64, None, ""]
    clients = ["claude-code", "claude-ai", None, "", "Bad Name", "x" * 65]
    scope_sets = [("*",), ("demo",), ("atlas",), ("demo", "atlas")]
    for mode, client, token, scope, gen in itertools.product(
            grants.WRITES_MODES + ("bogus",), clients, tokens, ["demo", "atlas", None, 3],
            [None, 1, 4]):
        held = rng.choice(tokens[:2])
        scopes_held = rng.choice(scope_sets)
        grant = (None if gen is None or client is None else
                 grants.Grant(client, grants.token_sha256(held), gen, scopes_held, "t"))
        store = {} if grant is None else {client: grant}
        for caller in (grants.Caller(client, token), None):
            want = _reference(mode, caller.client if caller else None,
                              caller.token if caller else None, scope, grant)
            got = grants.decide(mode, caller, scope, store)
            assert got.allowed == want, (mode, caller, scope, grant)
            if got.allowed and mode == "granted":
                assert (got.client, got.generation, got.token_sha256) == (
                    client, gen, grant.token_sha256)
            else:
                assert got.client is None and got.binding == ""
    # An unreadable store refuses every granted write, whoever asks.
    assert not grants.decide("granted", grants.Caller("claude-code", "a" * 64), "demo",
                             None).allowed


# --------------------------------------------------------------------------- the files and the CLI

def test_the_store_keeps_only_a_hash_and_its_files_are_private() -> None:
    grant, path = grants.issue("claude-code")
    token = path.read_text().strip()
    store = grants.grants_path().read_text()
    assert token not in store and grant.token_sha256 in store
    assert stat.S_IMODE(os.stat(grants.grants_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert grants.read_token("claude-code") == token


def test_a_name_that_is_not_a_safe_filename_is_refused() -> None:
    for bad in ("../x", "A", "", "-x", "a" * 65, "a/b", "a b"):
        with pytest.raises(grants.GrantError):
            grants.issue(bad)


def test_a_request_is_recorded_once_an_hour_and_drops_out_when_granted() -> None:
    caller = grants.Caller("zed", None, {"name": "Zed", "version": "1", "junk": "x" * 900})
    assert grants.record_request(caller, now=1_000_000.0)
    assert not grants.record_request(caller, now=1_000_000.0 + 3599)
    assert grants.record_request(caller, now=1_000_000.0 + 3601)
    listing = grants.listing()
    assert listing["requested"] == [{"client": "zed", "client_info": {"name": "Zed",
                                                                      "version": "1"},
                                     "at": listing["requested"][0]["at"]}]
    grants.issue("zed")
    assert grants.listing()["requested"] == []
    assert [g["client"] for g in grants.listing()["granted"]] == ["zed"]
    grants.revoke("zed")
    assert grants.listing() == {"granted": [], "requested": []}


def test_the_cli_grants_lists_and_revokes() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["grant", "claude-code", "--scopes", "demo,atlas", "--json"]) == 0
    granted = json.loads(out.getvalue())
    assert granted["scopes"] == ["demo", "atlas"] and granted["generation"] == 1
    assert Path(granted["token_path"]).is_file()
    grants.record_request(grants.Caller("zed"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["grants", "--json"]) == 0
    listing = json.loads(out.getvalue())
    assert [g["client"] for g in listing["granted"]] == ["claude-code"]
    assert [r["client"] for r in listing["requested"]] == ["zed"]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(["revoke", "zed"]) == 0
        assert cli.main(["revoke", "claude-code"]) == 0
    assert grants.listing() == {"granted": [], "requested": []}
    with contextlib.redirect_stderr(io.StringIO()):
        assert cli.main(["grant", "Not/A/Name"]) == 2


def test_a_malformed_store_refuses_every_write() -> None:
    grants.issue("claude-code")
    grants.grants_path().write_text("this is = = not toml")
    decision = grants.may_write("granted", grants.Caller("claude-code",
                                                         grants.read_token("claude-code")), "demo")
    assert not decision.allowed and "cannot be read" in decision.reason


# --------------------------------------------------------------------------- the server and main

def test_the_server_block_says_granted_and_stays_read_only_for_old_clients() -> None:
    """0.3.5's attach check reads `read_only` and refuses an engine reporting false."""
    _, registry = _fixture()
    for writes, read_only in (("none", True), ("granted", True), ("open", False)):
        cfg = _engine_cfg(registry, writes)
        err, payload = _call(cfg, "list_scopes", {})
        assert not err
        assert payload["server"]["writes"] == writes
        assert payload["server"]["read_only"] is read_only


def test_main_refuses_contradictory_or_misplaced_write_flags() -> None:
    real = stack.build
    stack.build = lambda **kw: (_ for _ in ()).throw(AssertionError("built a stack"))
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert server.main(["--socket", "/tmp/x.sock", "--writes=granted", "--read-only"]) == 2
            assert server.main(["--writes=granted"]) == 2
            assert server.main(["--http", "--writes=granted"]) == 2
            assert server.main(["--connect", "--writes=granted"]) == 2
            assert server.main(["--client", "zed"]) == 2
            assert server.main(["--connect", "--client", "Not A Name"]) == 2
    finally:
        stack.build = real
    text = err.getvalue()
    assert "say the same thing two ways" in text and "for the shared --socket server" in text


def test_main_serves_granted_on_the_socket() -> None:
    seen: dict = {}

    def _capture(cfg, path, **kw):
        seen["cfg"], seen["kw"] = cfg, kw
        return 0

    real = server.serve_unix, stack.build
    server.serve_unix, stack.build = _capture, lambda **kw: stack.Stack()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            assert server.main(["--socket", "/tmp/x.sock", "--writes=granted",
                                "--lexical-only"]) == 0
    finally:
        server.serve_unix, stack.build = real
    assert seen["cfg"].writes == "granted" and seen["cfg"].read_only is True
    assert seen["kw"]["allow_writes"] is True


def test_the_banner_says_granted_clients_only() -> None:
    _, registry = _fixture()
    err = io.StringIO()
    with contextlib.redirect_stderr(err), _socket_dir() as d, \
            _engine(d / "s.sock", _engine_cfg(registry, "granted")):
        pass
    assert "writes for granted clients only" in err.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- review fixes

def test_a_new_grant_keeps_the_scopes_it_had() -> None:
    """Rotating is `grant` again, with no `--scopes`; so is Allow on a granted name in a host. It
    must not widen a grant the user narrowed."""
    grants.issue("claude-code", ["demo"])
    grant, _ = grants.issue("claude-code")
    assert grant.scopes == ("demo",) and grant.generation == 2
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(["grant", "claude-code"]) == 0
    assert grants.load()["claude-code"].scopes == ("demo",)
    widened, _ = grants.issue("claude-code", ["*"])
    assert widened.scopes == ("*",), "an explicit list still changes them"
    assert grants.issue("zed")[0].scopes == ("*",), "a first grant still defaults to every scope"


def test_an_empty_scope_list_is_refused_not_every_scope() -> None:
    with pytest.raises(grants.GrantError, match="names no scope"):
        grants.issue("claude-code", [])
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert cli.main(["grant", "claude-code", "--scopes", ""]) == 2
    assert "names no scope" in err.getvalue(), err.getvalue()
    assert grants.load() == {}


@pytest.mark.parametrize("spelled", ['"written_by"', "'written_by'", "Written_By", "WRITTEN_BY",
                                     '"amended_by"', "Amended_by"])
def test_a_stamp_key_spelled_another_way_is_refused(spelled: str) -> None:
    """YAML, Obsidian and Dataview read these as the stamp keys; the engine's line reader does
    not. A note carrying one would show another author once the engine's own line was added."""
    content = _NOTE.replace("status: active\n", f"status: active\n{spelled}: someone\n").encode()
    with pytest.raises(notes.NoteError, match="spells a key"):
        notes.stamp_new(content, "claude-code")
    base = _NOTE.encode()
    with pytest.raises(notes.NoteError, match="spells a key"):
        notes.stamp_amend(content + b"\nedited\n", base, "claude-code")


def test_a_stamp_key_in_a_yaml_block_is_read_as_yaml_reads_it() -> None:
    """A block that is not flat lines is read by YAML. A key YAML reads as `Written_By` is still
    the stamp key to Obsidian and Dataview, which fold the case of a property's name."""
    content = (b'---\nstatus: active\ndoc_type: decision\nconfidence: proposed\ntags:\n  - a\n'
               b'Written_By: someone\n---\n\n# x\n')
    with pytest.raises(notes.NoteError, match="spells a key"):
        notes.stamp_new(content, "claude-code")
