"""Agent mode: one scope, served to another user from a published snapshot it cannot write.

The fixture registers THREE scopes — `demo` (allowed), `acme` and `globex` — so a refusal that
leaks the registry is visible: an error naming `acme` or `globex` would be exactly the sentence
`scopes.resolve` writes, which is the leak agent mode exists to close.

After publishing, the operator's index and vault are MOVED AWAY. Every agent-side assertion runs
against that state, so a path that quietly fell back to the live index or the vault on disk fails
here rather than passing on a machine where both happen to be readable.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _swift_fixtures as fx  # noqa: E402

from substrate import agent, cli, refresh, refresh_state, scopes, stack  # noqa: E402
from substrate.mcp import server  # noqa: E402
from substrate.models import Chunk, Document  # noqa: E402
from substrate.store.index_store import IndexStore  # noqa: E402

_BODY = ("Snapshot publishing copies the index into a shared folder for the agent. " * 10).strip()
_CALL = "Transcript of a workspace call in a vault that declares guard_state."
OTHERS = ("acme", "globex")


def _scope(root: Path, registry: Path, name: str, *, guard_state: bool = False) -> Path:
    """One composed-looking scope: a project vault with a manifest, its notes, and an index whose
    rows mirror what `compose` stores.

    `guard_state` declares the retired key with no state file behind it and adds a second note:
    the decision is INHERITED (core-vault) and the call is the project vault's own, which is the
    note the retired guard withheld."""
    vault = root / f"{name}-vault"
    (vault / "02-areas").mkdir(parents=True)
    manifest = f'name = "{name}"\ninherits = []\n'
    if guard_state:
        manifest += f'guard_state = "{root / "state.json"}"\n'
    (vault / ".substrate.toml").write_text(manifest, encoding="utf-8")

    db = root / f"{name}.db"
    with IndexStore(str(db)) as s:
        notes = [("decision", vault / "02-areas" / "decision.md",
                  "core-vault" if guard_state else f"{name}-vault", _BODY)]
        if guard_state:
            notes.append(("call", vault / "call.md", f"{name}-vault", _CALL))
        for doc_id, path, owner, text in notes:
            path.write_text(f"# {doc_id}\n\n{text}\n", encoding="utf-8")
            doc = Document(doc_id=doc_id, source_path=str(path),
                           source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                           source_pages=1, document_class="reference-frozen", title=doc_id,
                           status="active", doc_type="decision", confidence="stated",
                           vault=owner, tier=3, domains=["agent"], supersedes=[])
            ch = Chunk(chunk_id=f"{doc_id}#c00000", doc_id=doc_id, kind="passage", text=text,
                       path=[doc_id], level=1, n_chars=len(text),
                       document_class="reference-frozen")
            s.upsert(doc, [ch], markdown_path=str(root / f"{name}-index" / doc_id / "document.md"),
                     markdown_mtime=0.0, markdown_sha256="d" * 64)
    scopes.record(name, vault=vault, db=db, index_root=root, registry=registry)
    return vault


@pytest.fixture
def world(tmp_path):
    """The operator's side, published, then made unreachable. Returns (shared dir, registry)."""
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    for name in ("demo", *OTHERS):
        _scope(home, registry, name)
    refresh_state.record("demo", "refreshed", registry=registry)
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=registry)
    return shared, registry


def _hide(registry: Path) -> None:
    """The agent cannot read the operator's home: rename it away."""
    home = registry.parent
    home.rename(home.with_name("operator-gone"))


def _cfg(shared: Path, **kw) -> server.Config:
    return server.Config(None, stack.build(lexical_only=True),
                         agent=agent.AgentScopes(["demo"], shared, **kw))


def _rpc(cfg, method: str, params: dict | None = None) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                         cfg)


def _call(cfg, tool: str, args: dict) -> tuple[dict | str, bool]:
    body = _rpc(cfg, "tools/call", {"name": tool, "arguments": args})["result"]
    text = body["content"][0]["text"]
    return (text, True) if body.get("isError") else (json.loads(text), False)


# --------------------------------------------------------------------------- allowed scope

def test_search_and_expand_answer_from_the_snapshot_alone(world):
    shared, registry = world
    _hide(registry)
    cfg = _cfg(shared)

    res, err = _call(cfg, "search", {"scope": "demo", "query": "snapshot publishing"})
    assert not err, res
    assert res["passages"], "the allowed scope must answer"
    ref = res["passages"][0]["expand_ref"]

    passage, err = _call(cfg, "expand", {"expand_ref": ref})
    assert not err, passage
    assert passage["passage"]["text"] == _BODY

    note, err = _call(cfg, "expand", {"expand_ref": ref, "mode": "note"})
    assert not err, note
    assert _BODY in note["note"]["text"]
    # Vault-relative, never the operator's absolute path.
    assert note["note"]["path"] == "demo-vault/02-areas/decision.md"
    assert note["note"]["stale"] is False

    for tool in ("status", "documents"):
        out, err = _call(cfg, tool, {"scope": "demo"})
        assert not err, out


def test_list_scopes_names_only_the_allowed_scope(world):
    shared, registry = world
    _hide(registry)
    out, err = _call(_cfg(shared), "list_scopes", {})
    assert not err
    assert [r["scope"] for r in out["scopes"]] == ["demo"]
    text = json.dumps(out)
    assert not any(o in text for o in OTHERS)


# --------------------------------------------------------------------------- disallowed scopes

@pytest.mark.parametrize("tool", ["search", "status", "documents"])
@pytest.mark.parametrize("scope", ["acme", "globex", "nope", "../demo", "demo/..", "DEMO", ""])
def test_any_other_scope_is_refused_without_naming_one(world, tool, scope):
    shared, registry = world
    _hide(registry)
    args = {"scope": scope, "query": "snapshot"} if tool == "search" else {"scope": scope}
    text, err = _call(_cfg(shared), tool, args)
    assert err
    if scope:
        assert text == f"ToolError: {agent.UNAVAILABLE}"
    else:
        assert not any(n in text for n in ("demo", *OTHERS))


@pytest.mark.parametrize("ref", [
    "acme/decision#c00000",            # another scope's ref, well-formed
    "globex/decision#c00000",
    "demo/../../../etc/passwd",        # traversal in the chunk id
    "demo/../acme/decision#c00000",
    "../demo/decision#c00000",         # traversal in the scope
    "/etc/passwd",
    "demo/",
])
def test_expand_accepts_only_refs_inside_the_snapshot(world, ref):
    shared, registry = world
    _hide(registry)
    for mode in ("passage", "note"):
        text, err = _call(_cfg(shared), "expand", {"expand_ref": ref, "mode": mode})
        assert err, text
        assert "root:" not in text
        # The caller's own ref may be echoed back; nothing beyond it may name another scope.
        echoed = text.replace(ref, "").replace(ref.partition("/")[2], "")
        assert not any(o in echoed for o in OTHERS)


# --------------------------------------------------------------------------- read-only

def test_ingest_is_not_registered(world):
    shared, _ = world
    cfg = _cfg(shared)
    names = [t["name"] for t in _rpc(cfg, "tools/list")["result"]["tools"]]
    assert "ingest" not in names
    assert set(names) == {"search", "expand", "documents", "list_scopes", "status"}
    for write in ("ingest", "amend"):
        resp = _rpc(cfg, "tools/call", {"name": write, "arguments": {"scope": "demo"}})
        assert resp["error"]["code"] == -32602 and "unknown tool" in resp["error"]["message"]


def test_the_snapshot_is_opened_read_only(world):
    shared, _ = world
    entry, store = agent.AgentScopes(["demo"], shared).open("demo")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.db.execute("INSERT INTO snapshot_meta(key, value) VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            store.db.execute("DELETE FROM chunks")
    finally:
        store.close()
    assert sorted(p.name for p in shared.iterdir()) == ["demo.snapshot.db",
                                                        "demo.snapshot.json"]


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def test_agent_mode_writes_nothing_and_never_reads_the_registry(world, tmp_path, monkeypatch):
    shared, registry = world
    _hide(registry)
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    monkeypatch.setenv("HOME", str(agent_home))
    monkeypatch.chdir(agent_home)

    def forbidden(*a, **kw):
        raise AssertionError("agent mode reached the operator's registry or refresh record")

    monkeypatch.setattr(scopes, "load", forbidden)
    monkeypatch.setattr(scopes, "resolve", forbidden)
    monkeypatch.setattr(refresh_state, "report", forbidden)

    built = {}
    real_build = stack.build

    def capture(**kw):
        built.update(kw)
        return real_build(lexical_only=True)

    monkeypatch.setattr(server.stack, "build", capture)

    shared.chmod(0o555)
    before = _tree(tmp_path)
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search", "arguments": {"scope": "demo", "query": "snapshot"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "expand",
                    "arguments": {"expand_ref": "demo/decision#c00000", "mode": "note"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "status", "arguments": {"scope": "demo"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "documents", "arguments": {"scope": "demo"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "list_scopes", "arguments": {}}},
    ]
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(f) for f in frames)))
    monkeypatch.setattr(sys, "stdout", out)
    try:
        rc = server.main(["--agent-mode", "--scopes", "demo", "--snapshot-dir", str(shared)])
    finally:
        shared.chmod(0o755)
    assert rc == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 7
    for r in replies[2:]:
        assert not r["result"].get("isError"), r
    assert built["cache_path"] == ":memory:"
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("extra", [["--registry", "/x.toml"], ["--http"], ["--no-read-only"]])
def test_agent_mode_refuses_flags_that_reach_past_the_snapshot(world, extra):
    shared, _ = world
    assert server.main(["--agent-mode", "--scopes", "demo", "--snapshot-dir", str(shared),
                        "--lexical-only", *extra]) == 2


def test_agent_mode_needs_its_scopes_and_folder():
    assert server.main(["--agent-mode", "--lexical-only"]) == 2
    assert server.main(["--scopes", "demo", "--lexical-only"]) == 2


# --------------------------------------------------------------------------- freshness

def test_a_stale_snapshot_says_so(world):
    shared, registry = world
    old = datetime.now(UTC) - timedelta(days=3)
    agent.publish("demo", shared, registry=registry, now=old)
    # Same content, so the publish above only re-verified — at the OLD time.
    _hide(registry)
    res, err = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot"})
    assert not err
    snap = res["refresh"]["snapshot"]
    assert snap["stale"] is True and snap["age_seconds"] >= 3 * 86400
    assert res["refresh"]["note"].startswith("STALE SNAPSHOT")
    status, _ = _call(_cfg(shared), "status", {"scope": "demo"})
    assert status["refresh"]["snapshot"]["stale"] is True
    assert "error" in status["drift"]


def test_a_fresh_snapshot_is_not_stale_and_carries_the_operators_verdict(world):
    shared, registry = world
    _hide(registry)
    res, _ = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot"})
    assert res["refresh"]["snapshot"]["stale"] is False
    assert res["refresh"]["known"] is True and res["refresh"]["outcome"] == "refreshed"
    assert res["refresh"]["frozen"] is False


def test_a_failed_refresh_reaches_the_agent_as_frozen(world):
    shared, registry = world
    refresh_state.record("demo", "compose_failed", registry=registry)
    assert agent.after_refresh_record("demo", "compose_failed", registry=registry) is None
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    db_before = (shared / "demo.snapshot.db").stat().st_mtime_ns
    assert agent.after_refresh_record("demo", "compose_failed", registry=registry)
    assert (shared / "demo.snapshot.db").stat().st_mtime_ns == db_before
    _hide(registry)
    res, _ = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot"})
    assert res["refresh"]["frozen"] is True


def test_a_missing_manifest_reads_as_unknown_age_not_fresh(world):
    shared, registry = world
    (shared / "demo.snapshot.json").unlink()
    _hide(registry)
    res, err = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot"})
    assert not err
    assert res["refresh"]["snapshot"]["stale"] is None
    assert res["refresh"]["known"] is False


# --------------------------------------------------------------------------- the publisher

def test_publishing_strips_operator_paths(tmp_path):
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    _scope(home, registry, "demo")
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=registry)
    raw = (shared / "demo.snapshot.db").read_bytes()
    assert str(home).encode() not in raw, "an operator path survived in the file"
    assert oct((shared / "demo.snapshot.db").stat().st_mode & 0o777) == oct(agent.FILE_MODE)


def test_a_vault_declaring_guard_state_publishes_its_own_notes(tmp_path):
    """The engine ignores `guard_state`. No state file exists here, which the retired guard read as
    "nothing vouches" and answered by removing the project vault's own notes from the copy."""
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    _scope(home, registry, "demo", guard_state=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=registry)
    assert _CALL.encode() in (shared / "demo.snapshot.db").read_bytes()

    _hide(registry)
    res, _ = _call(_cfg(shared), "documents", {"scope": "demo"})
    assert sorted(d["title"] for d in res["documents"]) == ["call", "decision"]
    assert res["total"] == 2


def test_a_snapshot_carries_no_trace_of_its_sources_vector_writes(tmp_path):
    """`vector_generation` counts every write to the live index's vectors, and removing an
    excluded note's vector from the copy moved it once more. Carried into the snapshot, it would
    tell the agent how much was left out. A snapshot gets its own epoch and starts at 0."""
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    _scope(home, registry, "demo")
    call = _add_real_doc(registry, "call", _BODY)
    with IndexStore(str(home / "demo.db")) as s:
        s.store_vectors([("decision#c00000", [1.0, 0.0]), ("call#c00000", [0.0, 1.0])], "m#raw")
        live = tuple(s.db.execute("SELECT epoch, n FROM vector_generation").fetchone())
    call.write_text(call.read_text(encoding="utf-8") + "\nAn edit compose has not seen.\n",
                    encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=registry)
    with IndexStore(str(shared / "demo.snapshot.db"), migrate=False, immutable=True) as snap:
        epoch, n = snap.db.execute("SELECT epoch, n FROM vector_generation").fetchone()
        kept = [r[0] for r in snap.db.execute("SELECT chunk_id FROM chunk_vectors")]
    assert kept == ["decision#c00000"], "the premise: the excluded note's vector was removed"
    assert live[1] == 2 and n == 0
    assert epoch != live[0]


def test_an_unchanged_index_is_verified_not_rewritten(world):
    shared, registry = world
    before = (shared / "demo.snapshot.db").stat().st_mtime_ns
    p = agent.publish("demo", shared, registry=registry)
    assert not p.rewrote
    assert (shared / "demo.snapshot.db").stat().st_mtime_ns == before


def test_publishing_never_creates_the_shared_folder(world, tmp_path):
    _, registry = world
    with pytest.raises(agent.AgentError):
        agent.publish("demo", tmp_path / "absent", registry=registry)
    assert not (tmp_path / "absent").exists()


def test_refresh_record_publishes_and_a_publish_failure_does_not_fail_the_record(
        world, tmp_path, capsys):
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    (shared / "demo.snapshot.db").unlink()
    assert cli.main(["refresh-record", "--scope", "demo", "--outcome", "refreshed",
                     "--registry", str(registry), "--quiet"]) == 0
    assert (shared / "demo.snapshot.db").is_file()

    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{tmp_path / "absent"}"\n', encoding="utf-8")
    assert cli.main(["refresh-record", "--scope", "demo", "--outcome", "refreshed",
                     "--registry", str(registry), "--quiet"]) == 0
    assert "WARNING (agent snapshot demo)" in capsys.readouterr().err


def test_a_snapshot_published_by_the_cli_stays_readable_by_its_group(world):
    """The CLI runs under umask 077 so nothing the operator's engine writes is readable by another
    uid. The snapshot is the one file meant to be: the agent reads it through the folder's group."""
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    for p in agent.snapshot_paths(shared, "demo"):
        p.unlink()
    previous = os.umask(0o022)
    try:
        assert cli.main(["refresh-record", "--scope", "demo", "--outcome", "refreshed",
                         "--registry", str(registry), "--quiet"]) == 0
    finally:
        os.umask(previous)
    for p in agent.snapshot_paths(shared, "demo"):
        assert oct(p.stat().st_mode & 0o777) == oct(agent.FILE_MODE), p


def test_agent_status_does_not_judge_the_shared_snapshot_exposed(world):
    """The snapshot is 0640 on purpose. `exposed` lists what must be sealed; listing the one file
    the operator chose to share would tell the agent to report a correct setup as a leak."""
    shared, registry = world
    _hide(registry)
    status, err = _call(_cfg(shared), "status", {"scope": "demo"})
    assert not err, status
    assert status["exposed"] is None


def test_a_scope_not_in_the_config_is_never_published(world):
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    assert agent.after_refresh_record("acme", "refreshed", registry=registry) is None
    assert not (shared / "acme.snapshot.db").exists()


def test_a_file_at_the_snapshot_path_must_be_that_scopes_snapshot(world):
    shared, registry = world
    agent.publish("demo", shared, registry=registry)
    other = shared / "other"
    other.mkdir()
    os.replace(shared / "demo.snapshot.db", other / "x.db")
    con = sqlite3.connect(other / "x.db")
    con.execute("UPDATE snapshot_meta SET value='acme' WHERE key='scope'")
    con.commit()
    con.close()
    os.replace(other / "x.db", shared / "demo.snapshot.db")
    text, err = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot"})
    assert err and "acme" not in text


# --------------------------------------------------------------------------- crosscheck regressions

def test_an_identity_only_change_republishes_rather_than_reverifying(world):
    shared, registry = world
    entry = scopes.resolve("demo", registry)
    with IndexStore(str(entry.db)) as s:
        s.replace_identity([("ada", "Ada Lovelace", "person", None)])
        s.set_document_entities("decision", [("ada", "Ada")])
    p = agent.publish("demo", shared, registry=registry)
    assert p.rewrote, "the roster changed, so the snapshot must be rebuilt"
    _hide(registry)
    status, _ = _call(_cfg(shared), "status", {"scope": "demo"})
    assert [e["name"] for e in status["entities"]] == ["Ada"]


def test_a_symlinked_or_shared_writable_folder_is_refused(world, tmp_path):
    shared, registry = world
    link = tmp_path / "link"
    link.symlink_to(shared)
    with pytest.raises(agent.AgentError):
        agent.publish("demo", link, registry=registry)
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    with pytest.raises(agent.AgentError):
        agent.publish("demo", loose, registry=registry)
    assert not (loose / "demo.snapshot.db").exists()


def test_a_manifest_planted_as_a_symlink_is_not_read_through(world, tmp_path):
    shared, registry = world
    secret = tmp_path / "secret.json"
    secret.write_text('{"scopes": {"acme": {}}, "token": "s3cr3t"}', encoding="utf-8")
    secret.chmod(0o600)
    manifest = shared / "demo.snapshot.json"
    manifest.unlink()
    manifest.symlink_to(secret)
    # Nothing readable through the link, so nothing copied out. An unreadable manifest may sit
    # beside a copy holding calls, so the copy is withdrawn, and the link with it, not its target.
    assert agent.annotate("demo", shared, registry=registry) == "withdrawn"
    assert not manifest.exists() and not manifest.is_symlink()
    assert "s3cr3t" in secret.read_text(encoding="utf-8")
    assert "s3cr3t" not in "".join(p.read_text(errors="replace") for p in shared.iterdir()
                                   if p.is_file() and not p.is_symlink())


# --------------------------------------------------------------------------- adversary regressions

_SECRET = "zqxjsecrettoken"


@pytest.mark.parametrize("after", ["kept", "deleted", "retargeted"])
def test_a_document_compose_read_from_outside_the_vaults_is_excluded_whole(tmp_path, after):
    """Compose indexes a symlinked note THROUGH the link, so the outside file's text is in the
    passages and the search index — this builds the index that way. The secret must not reach
    the snapshot by either door, and what the link looks like NOW does not matter: deleted, or
    swapped for an innocent file, the chunks still hold what compose read.

    The token is one lowercase word because FTS5 stores tokens lowercased without separators: a
    phrase with spaces would be absent from the raw bytes even if the FTS index still held it."""
    secret_text = f"{_SECRET} " * 20
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    vault = _scope(home, registry, "demo")
    secret = tmp_path / "operator-only.md"
    secret.write_text(secret_text, encoding="utf-8")
    link = vault / "02-areas" / "linked.md"
    link.symlink_to(secret)
    with IndexStore(str(home / "demo.db")) as s:
        for doc_id, source in (("linked", link), ("outside", secret)):
            doc = Document(doc_id=doc_id, source_path=str(source),
                           source_sha256=hashlib.sha256(secret.read_bytes()).hexdigest(),
                           source_pages=1, document_class="reference-frozen", title=_SECRET,
                           status="active", doc_type="decision", confidence="stated",
                           vault="demo-vault", tier=3, domains=[], supersedes=[])
            ch = Chunk(chunk_id=f"{doc_id}#c00000", doc_id=doc_id, kind="passage",
                       text=secret_text, path=[doc_id], level=1, n_chars=len(secret_text),
                       document_class="reference-frozen")
            s.upsert(doc, [ch], markdown_path="x", markdown_mtime=0.0, markdown_sha256="d" * 64)
    if after == "deleted":
        link.unlink()
    elif after == "retargeted":
        link.unlink()
        link.write_text("an innocent note\n", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    p = agent.publish("demo", shared, registry=registry)
    assert p.excluded == 2
    assert _SECRET.encode() not in (shared / "demo.snapshot.db").read_bytes()
    _hide(registry)
    res, err = _call(_cfg(shared), "search", {"scope": "demo", "query": _SECRET})
    assert not err and all(_SECRET not in json.dumps(x) for x in res["passages"])
    docs, _ = _call(_cfg(shared), "documents", {"scope": "demo"})
    assert [d["title"] for d in docs["documents"]] == ["decision"]


def test_a_note_symlinked_within_its_vault_is_still_served(tmp_path):
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    vault = _scope(home, registry, "demo")
    real = vault / "02-areas" / "decision.md"
    moved = vault / "02-areas" / "decision-real.md"
    real.rename(moved)
    real.symlink_to(moved)
    shared = tmp_path / "shared"
    shared.mkdir()
    assert agent.publish("demo", shared, registry=registry).excluded == 0
    _hide(registry)
    out, err = _call(_cfg(shared), "expand",
                     {"expand_ref": "demo/decision#c00000", "mode": "note"})
    assert not err and _BODY in out["note"]["text"]


def _add_real_doc(registry: Path, doc_id: str, text: str) -> Path:
    """A second note indexed the way compose would: a vault file and its real checksum."""
    entry = scopes.resolve("demo", registry)
    note = Path(entry.vault) / "02-areas" / f"{doc_id}.md"
    note.write_text(f"# {doc_id}\n\n{text}\n", encoding="utf-8")
    with IndexStore(str(entry.db)) as s:
        doc = Document(doc_id=doc_id, source_path=str(note),
                       source_sha256=hashlib.sha256(note.read_bytes()).hexdigest(),
                       source_pages=1, document_class="reference-frozen", title=doc_id,
                       status="active", doc_type="decision", confidence="stated",
                       vault="demo-vault", tier=3, domains=[], supersedes=[])
        ch = Chunk(chunk_id=f"{doc_id}#c00000", doc_id=doc_id, kind="passage", text=text,
                   path=[doc_id], level=1, n_chars=len(text), document_class="reference-frozen")
        s.upsert(doc, [ch], markdown_path="x", markdown_mtime=0.0, markdown_sha256="d" * 64)
    return note


def test_a_note_edited_since_compose_is_left_out_until_recompose(world):
    """Its chunks are the OLD text and nothing on disk vouches for them any more, so it is not
    served — not even as `stale`. The refresh that recomposes it brings it back."""
    shared, registry = world
    _add_real_doc(registry, "other", _BODY)
    note = Path(scopes.resolve("demo", registry).vault) / "02-areas" / "decision.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nAn edit compose has not seen.\n",
                    encoding="utf-8")
    p = agent.publish("demo", shared, registry=registry)
    assert p.rewrote and p.excluded == 1
    _hide(registry)
    text, err = _call(_cfg(shared), "expand", {"expand_ref": "demo/decision#c00000"})
    assert err
    out, err = _call(_cfg(shared), "expand", {"expand_ref": "demo/other#c00000", "mode": "note"})
    assert not err and out["note"]["stale"] is False


def test_a_snapshot_with_every_note_excluded_is_refused_not_published(world):
    shared, registry = world
    before = (shared / "demo.snapshot.db").read_bytes()
    note = Path(scopes.resolve("demo", registry).vault) / "02-areas" / "decision.md"
    note.write_text("edited", encoding="utf-8")
    with pytest.raises(agent.AgentError, match="empty snapshot"):
        agent.publish("demo", shared, registry=registry)
    assert (shared / "demo.snapshot.db").read_bytes() == before


def test_an_index_that_changes_during_the_copy_is_not_published(world, monkeypatch):
    shared, registry = world
    (shared / "demo.snapshot.db").unlink()
    real, calls = agent._fingerprint, []

    def drifting(*a):
        calls.append(1)
        return real(*a) + ("x" if len(calls) > 1 else "")

    monkeypatch.setattr(agent, "_fingerprint", drifting)
    with pytest.raises(agent.AgentError, match="changed while it was being copied"):
        agent.publish("demo", shared, registry=registry)
    assert sorted(p.name for p in shared.iterdir()) == ["demo.snapshot.json"]


def test_publishes_of_one_scope_are_serialized(world):
    import threading

    shared, registry = world
    done = threading.Event()
    with agent._publish_lock("demo", registry):
        t = threading.Thread(target=lambda: (agent.publish("demo", shared, registry=registry),
                                             done.set()))
        t.start()
        assert not done.wait(0.5), "publish ran while another held the scope's lock"
    t.join(10)
    assert done.is_set()


def test_guard_state_declared_after_publishing_leaves_the_snapshot_served(world):
    """A failed refresh marks the manifest and keeps the copy, whatever the manifest declares."""
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    vault = Path(scopes.resolve("demo", registry).vault)
    manifest = vault / ".substrate.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8")
                        + f'guard_state = "{registry.parent / "state.json"}"\n', encoding="utf-8")
    refresh_state.record("demo", "compose_failed", registry=registry)
    line = agent.after_refresh_record("demo", "compose_failed", registry=registry)
    assert line == "demo: snapshot manifest marked compose_failed"
    assert (shared / "demo.snapshot.db").exists()
    res, err = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot"})
    assert not err and res["passages"]


# --------------------------------------------------------------------------- fix-pass crosscheck regressions

def _add_doc(db: Path, doc_id: str, vault: str, source: Path) -> None:
    with IndexStore(str(db)) as s:
        doc = Document(doc_id=doc_id, source_path=str(source), source_sha256="0" * 64,
                       source_pages=1, document_class="reference-frozen", title=doc_id,
                       status="active", doc_type="decision", confidence="stated",
                       vault=vault, tier=3, domains=[], supersedes=[])
        ch = Chunk(chunk_id=f"{doc_id}#c00000", doc_id=doc_id, kind="passage", text=_BODY,
                   path=[doc_id], level=1, n_chars=len(_BODY), document_class="reference-frozen")
        s.upsert(doc, [ch], markdown_path=str(source), markdown_mtime=0.0,
                 markdown_sha256="d" * 64)


@pytest.mark.parametrize("rebuild", [True, False])
@pytest.mark.parametrize("change", ["added", "moved"])
def test_a_document_that_changes_while_notes_are_read_is_not_published(world, monkeypatch,
                                                                        change, rebuild):
    shared, registry = world
    entry = scopes.resolve("demo", registry)
    if rebuild:
        (shared / "demo.snapshot.db").unlink()
    before = None if rebuild else (shared / "demo.snapshot.db").read_bytes()
    manifest_before = (shared / "demo.snapshot.json").read_bytes()
    real = agent._read_notes

    def then_compose(*a):
        out = real(*a)
        if change == "added":
            _add_doc(entry.db, "late", "demo-vault", Path(entry.vault) / "late.md")
        else:
            with IndexStore(str(entry.db)) as s:
                s.db.execute("UPDATE documents SET vault='core-vault' WHERE doc_id='decision'")
        return out

    monkeypatch.setattr(agent, "_read_notes", then_compose)
    with pytest.raises(agent.AgentError, match="changed while it was being"):
        agent.publish("demo", shared, registry=registry, now=datetime.now(UTC) + timedelta(1))
    db = shared / "demo.snapshot.db"
    assert (not db.exists()) if rebuild else db.read_bytes() == before
    assert (shared / "demo.snapshot.json").read_bytes() == manifest_before


# --------------------------------------------------------------------------- the identity roster

def _roster(tmp_path: Path, registry: Path, *, inside: bool) -> Path:
    """A declared roster with two people: one a demo note mentions, one it never does — the
    shape of a roster shared across every scope."""
    vault = Path(scopes.resolve("demo", registry).vault)
    roster = (vault / "roster.json") if inside else (tmp_path / "shared-roster.json")
    roster.write_text(json.dumps({"entities": [
        {"id": "ada-lovelace", "name": "Ada Lovelace", "kind": "person",
         "aliases": ["Ada"], "gloss": "zqxjglossofada"},
        {"id": "zqxjotherscopeperson", "name": "Zqxjotherscopeperson", "kind": "person",
         "gloss": "zqxjglossofother"},
    ]}), encoding="utf-8")
    manifest = vault / ".substrate.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + f'identity = "{roster}"\n',
                        encoding="utf-8")
    with IndexStore(str(scopes.resolve("demo", registry).db)) as s:
        s.replace_identity([("ada-lovelace", "Ada Lovelace", "person", "zqxjglossofada"),
                            ("zqxjotherscopeperson", "Zqxjotherscopeperson", "person",
                             "zqxjglossofother")])
        s.set_document_entities("decision", [("ada-lovelace", "Ada")])
    return roster


def test_a_shared_roster_reaches_the_agent_only_as_this_scopes_notes_show_it(world, tmp_path):
    shared, registry = world
    _roster(tmp_path, registry, inside=False)
    assert agent.publish("demo", shared, registry=registry).rewrote
    raw = (shared / "demo.snapshot.db").read_bytes()
    for leak in (b"zqxjotherscopeperson", b"zqxjglossofada", b"zqxjglossofother", b"Lovelace",
                 b"ada-lovelace"):
        assert leak not in raw, leak
    _hide(registry)
    status, _ = _call(_cfg(shared), "status", {"scope": "demo"})
    [ada] = status["entities"]
    assert ada["name"] == "Ada" and ada["gloss"] is None and ada["kind"] == "person"
    assert ada["entity_id"].startswith("e-")
    # The opaque id still drives the `entity` filter.
    res, err = _call(_cfg(shared), "search",
                     {"scope": "demo", "query": "snapshot", "entity": ada["entity_id"]})
    assert not err and res["passages"]
    docs, _ = _call(_cfg(shared), "documents", {"scope": "demo", "entity": ada["entity_id"]})
    assert docs["total"] == 1


def test_a_roster_kept_inside_the_vault_is_stripped_the_same_way(tmp_path):
    """Where the roster file lives grants nothing: one kept in the project vault would otherwise
    ship its glosses beside the scope's notes."""
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    _scope(home, registry, "demo")
    _roster(tmp_path, registry, inside=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=registry)
    raw = (shared / "demo.snapshot.db").read_bytes()
    for leak in (b"zqxjotherscopeperson", b"zqxjglossofada", b"zqxjglossofother", b"Lovelace",
                 b"ada-lovelace"):
        assert leak not in raw, leak


def test_the_rosters_alias_merges_do_not_reach_the_agent(world, tmp_path):
    """"Ada" in one note and "Countess" in another resolve to ONE person in the roster. That is a
    judgement the roster holds and no note states, so the snapshot keeps them apart."""
    shared, registry = world
    _add_real_doc(registry, "other", _BODY)
    _roster(tmp_path, registry, inside=False)
    with IndexStore(str(scopes.resolve("demo", registry).db)) as s:
        s.set_document_entities("other", [("ada-lovelace", "Countess")])
    agent.publish("demo", shared, registry=registry)
    _hide(registry)
    status, _ = _call(_cfg(shared), "status", {"scope": "demo"})
    assert sorted(e["name"] for e in status["entities"]) == ["Ada", "Countess"]
    assert len({e["entity_id"] for e in status["entities"]}) == 2


# --------------------------------------------------------------------------- the app's decoder

# The app's SubstrateKit decodes these payloads (`WireStatusResult`, `WireScopeList`), and it once
# declared `vault`, `composed` and `registry` non-optional — so every agent-mode `status` and
# `list_scopes` failed to decode. Its regression fixtures are CAPTURED here, from this fixture,
# rather than written by hand: set SUBSTRATE_CAPTURE_SWIFT_FIXTURES=1 to rewrite them. Without it
# the test compares their SHAPE (every key, and which values are null) against what the engine
# emits now, so neither side can move without the other noticing.
# Stands in for pytest's tmp folder, which would put this machine's temp layout in the repo and
# change the bytes of every recapture. The folder, the scans and the write are `_swift_fixtures`,
# shared with `test_swift_golden_frames.py`.
SNAPSHOT_DIR_STAND_IN = "/agent-snapshots"


def _agent_frames(shared: Path) -> dict[str, dict]:
    cfg = _cfg(shared)
    frames = {
        "agent-status.frame.json": _rpc(cfg, "tools/call",
                                        {"name": "status", "arguments": {"scope": "demo"}}),
        "agent-list_scopes.frame.json": _rpc(cfg, "tools/call",
                                             {"name": "list_scopes", "arguments": {}}),
    }
    # No manifest: the snapshot's `published` stamp is unknown, so `composed` is null too.
    (shared / "demo.snapshot.json").unlink()
    frames["agent-status-unpublished.frame.json"] = _rpc(
        cfg, "tools/call", {"name": "status", "arguments": {"scope": "demo"}})
    return frames


def _payload(frame: dict) -> dict:
    assert not frame["result"].get("isError"), frame
    return json.loads(frame["result"]["content"][0]["text"])


def test_agent_status_and_list_scopes_name_no_vault_and_no_registry(world):
    """The nulls are the point: an agent server has no vault and no registry to name, and the
    operator's paths are not another user's to read. A placeholder here would be invented
    provenance that a client would treat as a directory."""
    shared, registry = world
    _hide(registry)
    frames = {k: _payload(v) for k, v in _agent_frames(shared).items()}

    status = frames["agent-status.frame.json"]
    assert status["vault"] is None
    assert isinstance(status["composed"], str)

    listing = frames["agent-list_scopes.frame.json"]
    assert listing["registry"] is None
    assert [(r["scope"], r["vault"]) for r in listing["scopes"]] == [("demo", None)]
    assert isinstance(listing["scopes"][0]["composed"], str)

    unpublished = frames["agent-status-unpublished.frame.json"]
    assert unpublished["vault"] is None and unpublished["composed"] is None


def _frame_payload(body: str) -> dict:
    return _payload(json.loads(body))


def test_the_swift_decoders_fixtures_are_what_agent_mode_sends(world):
    shared, registry = world
    _hide(registry)
    frames = _agent_frames(shared)
    # The bytes the HTTP transport writes (`_Handler.do_POST`), so the fixture is a response body
    # exactly as the app would receive one.
    bodies = {name: json.dumps(frame, ensure_ascii=False).replace(str(shared),
                                                                  SNAPSHOT_DIR_STAND_IN)
              for name, frame in frames.items()}
    if fx.capturing():
        # Every body checked BEFORE any is written, so a missed replace never leaves a file to
        # commit, nor a half-rewritten set. The parent holds both the shared folder and the
        # operator's home, whatever --basetemp is.
        fx.check_before_write(bodies, payload_of=_frame_payload, stand_in=SNAPSHOT_DIR_STAND_IN,
                              local=(shared.parent, fx.ENGINE_ROOT.parent))
        fx.write_all(bodies)
    for name, frame in frames.items():
        fx.assert_matches(name, _payload(frame), payload_of=_frame_payload,
                          stand_in=SNAPSHOT_DIR_STAND_IN)


# --------------------------------------------------------------------------- sources stay out

_CALL_TEXT = "Zqxjcalltranscript said on a recorded call, kept from the agent account."


def _sourced_scope(tmp_path: Path) -> tuple[Path, Path]:
    """`demo` holding its decision note and one CALL: a conversation-class note with a passage, an
    outline, a vector and an entity mention, every row a snapshot could carry it in."""
    home = tmp_path / "operator"
    home.mkdir()
    registry = home / "scopes.toml"
    vault = _scope(home, registry, "demo")
    call = vault / "_sources" / "zqxjcallnote.md"
    call.parent.mkdir()
    call.write_text(f"# zqxjcallnote\n\n{_CALL_TEXT}\n", encoding="utf-8")
    with IndexStore(str(home / "demo.db")) as s:
        doc = Document(doc_id="zqxjcallnote", source_path=str(call),
                       source_sha256=hashlib.sha256(call.read_bytes()).hexdigest(),
                       source_pages=1, document_class="conversation", title="zqxjcallnote",
                       status="active", doc_type="reference", confidence="stated",
                       vault="demo-vault", tier=3, domains=[], supersedes=[])
        chunks = [Chunk(chunk_id=f"zqxjcallnote#c0000{i}", doc_id="zqxjcallnote", kind=kind,
                        text=_CALL_TEXT, path=["zqxjcallnote"], level=1, n_chars=len(_CALL_TEXT),
                        document_class="conversation")
                  for i, kind in enumerate(("passage", "outline"))]
        s.upsert(doc, chunks, markdown_path="x", markdown_mtime=0.0, markdown_sha256="d" * 64)
        s.store_vectors([("decision#c00000", [1.0, 0.0]), ("zqxjcallnote#c00000", [0.0, 1.0])],
                        "m#raw")
        s.replace_identity([("ada-lovelace", "Zqxjcallperson", "person", None)])
        s.set_document_entities("zqxjcallnote", [("ada-lovelace", "Zqxjcallperson")])
    shared = tmp_path / "shared"
    shared.mkdir()
    return shared, registry


def test_a_snapshot_leaves_out_every_trace_of_a_call_by_default(tmp_path):
    shared, registry = _sourced_scope(tmp_path)
    agent.publish("demo", shared, registry=registry)
    raw = (shared / "demo.snapshot.db").read_bytes()
    for leak in (b"Zqxjcalltranscript", b"zqxjcallnote", b"Zqxjcallperson"):
        assert leak not in raw, leak
    with IndexStore(str(shared / "demo.snapshot.db"), migrate=False, immutable=True) as snap:
        for table in ("documents", "chunks", "snapshot_notes", "document_entities"):
            classes = {r[0] for r in snap.db.execute(f"SELECT doc_id FROM {table}")}
            assert classes <= {"decision"}, (table, classes)
        assert [r[0] for r in snap.db.execute("SELECT chunk_id FROM chunk_vectors")] == \
            ["decision#c00000"]
    manifest = json.loads((shared / "demo.snapshot.json").read_text(encoding="utf-8"))
    assert manifest["include_sources"] is False
    assert manifest["excluded_documents"] == 0, "a left-out call is not counted anywhere"

    _hide(registry)
    res, err = _call(_cfg(shared), "search",
                     {"scope": "demo", "query": "Zqxjcalltranscript", "include_sources": True})
    assert not err and res["passages"] == []
    assert any("without its sources" in n for n in res["filters"]["notes"]), res["filters"]
    docs, _ = _call(_cfg(shared), "documents", {"scope": "demo", "include_sources": True})
    assert docs["total"] == 1
    assert any("without its sources" in n for n in docs["filters"]["notes"])
    plain, _ = _call(_cfg(shared), "documents", {"scope": "demo"})
    assert not any("without its sources" in n for n in plain["filters"]["notes"]), \
        "said only to a caller who asked for sources"


def test_include_sources_carries_the_calls_into_the_snapshot(tmp_path):
    shared, registry = _sourced_scope(tmp_path)
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = true\n', encoding="utf-8")
    refresh_state.record("demo", "refreshed", registry=registry)
    agent.after_refresh_record("demo", "refreshed", registry=registry)
    assert b"Zqxjcalltranscript" in (shared / "demo.snapshot.db").read_bytes()
    manifest = json.loads((shared / "demo.snapshot.json").read_text(encoding="utf-8"))
    assert manifest["include_sources"] is True

    _hide(registry)
    res, err = _call(_cfg(shared), "search",
                     {"scope": "demo", "query": "Zqxjcalltranscript", "include_sources": True})
    assert not err and [p["expand_ref"] for p in res["passages"]] == ["demo/zqxjcallnote#c00000"], res
    assert not any("without its sources" in n for n in res["filters"]["notes"])


def test_an_agent_server_reads_no_write_log_for_the_calls_a_snapshot_lets_in(tmp_path,
                                                                             monkeypatch):
    """`include_sources = true` publishes calls; it does not let an agent server read the host's
    write log or vault for them. Every passage, call or not, says `unknown`, from search and
    expand alike."""
    from substrate import writelog

    shared, registry = _sourced_scope(tmp_path)
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = true\n', encoding="utf-8")
    refresh_state.record("demo", "refreshed", registry=registry)
    agent.after_refresh_record("demo", "refreshed", registry=registry)
    _hide(registry)

    def no_reading(*a, **kw):
        raise AssertionError("an agent server read the write log or a note for provenance")

    monkeypatch.setattr(writelog, "lookup", no_reading)
    monkeypatch.setattr(writelog, "stamp_of", no_reading)
    res, err = _call(_cfg(shared), "search",
                     {"scope": "demo", "query": "Zqxjcalltranscript", "include_sources": True})
    assert not err and res["passages"], res
    assert {p["provenance"]["state"] for p in res["passages"] + res["outline_records"]} == \
        {"unknown"}
    out, err = _call(_cfg(shared), "expand", {"expand_ref": "demo/zqxjcallnote#c00000"})
    assert not err and out["passage"]["provenance"] == writelog.unknown(), out


def test_turning_include_sources_off_rewrites_the_snapshot_without_the_calls(tmp_path):
    """An unchanged index is not re-verified under the new choice: the notes it leaves out change
    what the snapshot is built from."""
    shared, registry = _sourced_scope(tmp_path)
    agent.publish("demo", shared, registry=registry, include_sources=True)
    p = agent.publish("demo", shared, registry=registry)
    assert p.rewrote
    assert b"Zqxjcalltranscript" not in (shared / "demo.snapshot.db").read_bytes()


@pytest.mark.parametrize("value", ['"true"', "1", "[]", "{}"])
def test_a_non_bool_include_sources_is_refused(world, value):
    """Refused when that entry is published, by a refresh or by hand — not by `load_config`, where
    one bad entry used to refuse every other entry with it."""
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = {value}\n', encoding="utf-8")
    assert "include_sources must be true or false" in \
        agent.load_config(registry).broken["demo"].error
    with pytest.raises(agent.AgentError, match="include_sources must be true or false"):
        agent.after_refresh_record("demo", "refreshed", registry=registry)
    assert cli.main(["publish-snapshot", "--scope", "demo", "--registry", str(registry)]) == 2


def test_an_entry_with_only_a_dir_still_publishes_without_sources(world):
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    assert agent.load_config(registry) == agent.Config({"demo": agent.Target(shared, False)}, {})
    (shared / "demo.snapshot.db").unlink()
    refresh_state.record("demo", "refreshed", registry=registry)
    line = agent.after_refresh_record("demo", "refreshed", registry=registry)
    assert line.startswith("demo: snapshot published")
    manifest = json.loads((shared / "demo.snapshot.json").read_text(encoding="utf-8"))
    assert manifest["include_sources"] is False


def test_a_scope_holding_only_calls_is_refused_not_published_empty(tmp_path):
    shared, registry = _sourced_scope(tmp_path)
    with IndexStore(str(registry.parent / "demo.db")) as s:
        s.remove("decision")
    with pytest.raises(agent.AgentError, match="include_sources = true"):
        agent.publish("demo", shared, registry=registry)
    assert not (shared / "demo.snapshot.db").exists()


def _entry(registry: Path, shared: Path, *, include_sources: bool) -> None:
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = '
        f'{"true" if include_sources else "false"}\n', encoding="utf-8")


def test_revoking_include_sources_withdraws_the_snapshot_on_a_failed_refresh(tmp_path):
    shared, registry = _sourced_scope(tmp_path)
    _entry(registry, shared, include_sources=True)
    refresh_state.record("demo", "refreshed", registry=registry)
    agent.after_refresh_record("demo", "refreshed", registry=registry)
    assert b"Zqxjcalltranscript" in (shared / "demo.snapshot.db").read_bytes()

    _entry(registry, shared, include_sources=False)
    refresh_state.record("demo", "compose_failed", registry=registry)
    line = agent.after_refresh_record("demo", "compose_failed", registry=registry)
    assert "WITHDRAWN" in line
    assert not (shared / "demo.snapshot.db").exists()
    assert not (shared / "demo.snapshot.json").exists()


def test_revoking_include_sources_withdraws_the_snapshot_although_publish_refuses(tmp_path):
    """A scope of calls alone refuses to publish without them, and its copy must not outlive that."""
    shared, registry = _sourced_scope(tmp_path)
    with IndexStore(str(registry.parent / "demo.db")) as s:
        s.remove("decision")
    agent.publish("demo", shared, registry=registry, include_sources=True)
    with pytest.raises(agent.AgentError, match="include_sources = true"):
        agent.publish("demo", shared, registry=registry)
    assert not (shared / "demo.snapshot.db").exists()



# --------------------------------------------------------------------------- withdrawal follow-ups

def _refresh(scope: str, registry: Path) -> list[str]:
    """One `refresh-record` for `scope`, as the command runs it: the withdrawal sweep once, then
    the scope's record and publish. Every line it said."""
    said: list[str] = []
    refresh.withdraw_snapshots(registry, say=said.append)
    refresh.record_and_publish(scope, "refreshed", registry, say=said.append)
    return said


def _ledger(registry: Path) -> dict:
    path = registry.parent / agent.LEDGER_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))["published"] if path.exists() else {}


def _publish_entry(registry: Path, shared: Path) -> Path:
    config = registry.parent / agent.CONFIG_FILENAME
    config.write_text(f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    assert any("snapshot verified" in line or "snapshot published" in line
               for line in _refresh("demo", registry))
    return config


@pytest.mark.parametrize("how", ["entry", "emptied", "moved"])
def test_deleting_an_entry_withdraws_its_snapshot_at_the_next_refresh(world, tmp_path, how):
    """The entry was the only record of the folder, so deleting it orphaned the copy there, served
    to the agent for good. Any scope's refresh now withdraws it: `acme` has no entry at all."""
    shared, registry = world
    config = _publish_entry(registry, shared)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if how == "entry":
        config.write_text("[publish]\n", encoding="utf-8")
    elif how == "emptied":
        config.write_text("", encoding="utf-8")
    else:
        config.write_text(f'[publish.demo]\ndir = "{elsewhere}"\n', encoding="utf-8")
    said = _refresh("acme", registry)
    assert not (shared / "demo.snapshot.db").exists(), said
    assert not (shared / "demo.snapshot.json").exists(), said
    assert any("WITHDRAWN" in line and str(shared) in line for line in said), said
    assert str(shared) not in _ledger(registry).get("demo", [])
    assert not any("WITHDRAWN" in line for line in _refresh("acme", registry))
    if how == "moved":
        _refresh("demo", registry)
        assert (elsewhere / "demo.snapshot.db").exists()


@pytest.mark.parametrize("how", ["missing", "not-a-file"])
def test_a_publish_file_that_cannot_be_read_right_now_withdraws_nothing(world, how):
    """An editor saving `agent-publish.toml` can leave it missing, or something else, for a
    moment. That is not "no entries": the pass says so and withdraws nothing."""
    shared, registry = world
    config = _publish_entry(registry, shared)
    config.unlink()
    if how == "not-a-file":
        config.mkdir()
    said = _refresh("acme", registry)
    assert (shared / "demo.snapshot.db").exists(), said
    assert any(line.startswith("WARNING (agent snapshots)") and "empty" in line
               for line in said), said
    assert _ledger(registry) == {"demo": [str(shared)]}


def test_only_a_folder_published_into_is_recorded(world, tmp_path):
    """Recorded when a snapshot is written there for the entry, never merely because the entry
    names it: a folder that was never published into is nothing to withdraw, and a refusal there
    must not leave a row behind. A `--dir` publish elsewhere is the operator's own, never recorded
    and never withdrawn."""
    shared, registry = world
    config = registry.parent / agent.CONFIG_FILENAME
    missing = tmp_path / "not-made-yet"
    config.write_text(f'[publish.demo]\ndir = "{missing}"\n', encoding="utf-8")
    _refresh("demo", registry)
    assert _ledger(registry) == {}
    by_hand = tmp_path / "by-hand"
    by_hand.mkdir()
    config.write_text(f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
    assert cli.main(["publish-snapshot", "--scope", "demo", "--dir", str(by_hand),
                     "--registry", str(registry)]) == 0
    assert _ledger(registry) == {}
    assert cli.main(["publish-snapshot", "--scope", "demo", "--registry", str(registry)]) == 0
    assert _ledger(registry) == {"demo": [str(shared)]}
    config.write_text("", encoding="utf-8")
    _refresh("demo", registry)
    assert not (shared / "demo.snapshot.db").exists()
    assert (by_hand / "demo.snapshot.db").exists()


def test_a_recorded_folder_that_cannot_be_looked_in_is_kept_for_the_next_pass(world, tmp_path):
    """An unmounted volume, or a parent this user cannot enter: nothing is known about the copy,
    so the row stays and the copy is withdrawn once the folder is back. Forgotten at once, the
    copy outlived the entry for good."""
    shared, registry = world
    config = _publish_entry(registry, shared)
    config.write_text("", encoding="utf-8")
    away = tmp_path / "unmounted"
    shared.rename(away)
    _refresh("acme", registry)
    assert _ledger(registry) == {"demo": [str(shared)]}
    away.rename(shared)
    said = _refresh("acme", registry)
    assert not (shared / "demo.snapshot.db").exists(), said
    assert _ledger(registry) == {}


def test_a_recorded_folder_holding_no_snapshot_keeps_its_row_quietly(world, tmp_path):
    """An unmounted volume's mountpoint is an empty folder, so an empty folder is not proof the
    copy is gone: the row stays until a removal actually happens, and no pass warns about it,
    whatever `_trusted_dir` would say of the folder."""
    shared, registry = world
    config = _publish_entry(registry, shared)
    config.write_text("", encoding="utf-8")
    away = tmp_path / "on-the-volume"
    away.mkdir()
    for name in ("demo.snapshot.db", "demo.snapshot.json"):
        (shared / name).rename(away / name)
    shared.chmod(0o777)
    try:
        for _ in range(2):
            said = _refresh("acme", registry)
            assert not any(line.startswith("WARNING") for line in said), said
            assert _ledger(registry) == {"demo": [str(shared)]}
    finally:
        shared.chmod(0o755)
    for name in ("demo.snapshot.db", "demo.snapshot.json"):
        (away / name).rename(shared / name)
    _refresh("acme", registry)
    assert not (shared / "demo.snapshot.db").exists()
    assert _ledger(registry) == {}


def test_a_dropped_row_that_is_a_live_entrys_file_is_never_withdrawn(world):
    """Judged by file identity, not by name: where case is ignored `Demo.snapshot.db` is
    `demo.snapshot.db`, and a hard link is the same file under any name. A dropped row whose
    files are a live entry's is skipped; one with files of its own is withdrawn, so a volume that
    does not ignore case still loses a stray copy."""
    shared, registry = world
    _publish_entry(registry, shared)
    os.link(shared / "demo.snapshot.db", shared / "acme.snapshot.db")
    (registry.parent / agent.LEDGER_FILENAME).write_text(json.dumps(
        {"version": agent.LEDGER_VERSION, "published": {"Demo": [str(shared)],
                                                        "acme": [str(shared)],
                                                        "demo": [str(shared)]}}),
        encoding="utf-8")
    said = _refresh("globex", registry)
    assert (shared / "demo.snapshot.db").exists(), said
    assert (shared / "acme.snapshot.db").exists(), said
    assert set(_ledger(registry)) == {"Demo", "acme", "demo"}
    (shared / "acme.snapshot.db").unlink()
    (shared / "acme.snapshot.db").write_bytes(b"a copy of its own")
    said = _refresh("globex", registry)
    assert not (shared / "acme.snapshot.db").exists(), said
    assert (shared / "demo.snapshot.db").exists(), said


def test_a_row_is_decided_against_the_entry_as_it_stands_under_the_lock(world, monkeypatch):
    """The sweep read the config once and decided later. An entry re-added, and published, in
    between saw its fresh snapshot withdrawn by the stale decision."""
    shared, registry = world
    config = _publish_entry(registry, shared)
    config.write_text("", encoding="utf-8")
    real = agent._publish_lock

    def lock(scope, reg):
        if scope == "demo":   # the operator re-adds the entry just before the sweep's turn
            config.write_text(f'[publish.demo]\ndir = "{shared}"\n', encoding="utf-8")
        return real(scope, reg)

    monkeypatch.setattr(agent, "_publish_lock", lock)
    said: list[str] = []
    refresh.withdraw_snapshots(registry, say=said.append)
    assert (shared / "demo.snapshot.db").exists(), said
    assert _ledger(registry) == {"demo": [str(shared)]}


def test_a_ledger_that_cannot_be_read_is_left_alone_and_decides_nothing(tmp_path):
    """A link planted at the ledger's name (also EIO, EACCES, over the cap): what it records is
    unknown, so no recorded folder is withdrawn this pass and the file is not written over. A
    broken entry's copy that may hold calls still goes."""
    shared, registry = _sourced_scope(tmp_path)
    _entry(registry, shared, include_sources=True)
    _refresh("demo", registry)
    ledger = registry.parent / agent.LEDGER_FILENAME
    real = ledger.with_name("elsewhere.json")
    ledger.rename(real)
    ledger.symlink_to(real)
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = "no"\n', encoding="utf-8")
    said: list[str] = []
    refresh.withdraw_snapshots(registry, say=said.append)
    assert not (shared / "demo.snapshot.db").exists(), said
    assert any("cannot be read" in line and "left as it is" in line for line in said), said
    assert ledger.is_symlink()


def test_a_ledger_with_bad_rows_is_set_aside_and_its_good_rows_kept(world):
    """A ledger is the only record of where copies went, so bad content is kept for reading and
    every valid row survives it. Wiping it forgot every copy the good rows named."""
    shared, registry = world
    config = _publish_entry(registry, shared)
    ledger = registry.parent / agent.LEDGER_FILENAME
    ledger.write_text(json.dumps({"version": agent.LEDGER_VERSION, "published": {
        "demo": [str(shared)], "bad name!": ["/x"], "acme": ["relative"]}}), encoding="utf-8")
    config.write_text("", encoding="utf-8")
    said: list[str] = []
    refresh.withdraw_snapshots(registry, say=said.append)
    assert any("set aside" in line and "invalid row" in line for line in said), said
    assert list(registry.parent.glob(agent.LEDGER_FILENAME + ".bad-*"))
    assert not (shared / "demo.snapshot.db").exists(), "the good row was still acted on"


@pytest.mark.parametrize("ledger", ["{not json", json.dumps({"version": 99, "published": {}})])
def test_an_unusable_ledger_blocks_no_withdrawal(tmp_path, ledger):
    """Reported, set aside, and a broken entry's copy that may hold calls is still withdrawn from
    its folder. It used to stop the sweep, and every withdrawal with it."""
    shared, registry = _sourced_scope(tmp_path)
    _entry(registry, shared, include_sources=True)
    _refresh("demo", registry)
    (registry.parent / agent.LEDGER_FILENAME).write_text(ledger, encoding="utf-8")
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = "no"\n', encoding="utf-8")
    said: list[str] = []
    refresh.withdraw_snapshots(registry, say=said.append)
    assert not (shared / "demo.snapshot.db").exists(), said
    assert any(line.startswith("WARNING (agent snapshots)") and agent.LEDGER_FILENAME in line
               for line in said), said
    assert _ledger(registry) == {}
    assert list(registry.parent.glob(agent.LEDGER_FILENAME + ".bad-*"))


def test_a_freeze_reaches_the_agent_when_the_ledger_cannot_record(world):
    """`annotate` recorded the folder first, so a ledger failure skipped the frozen-state mark and
    the agent kept reading a healthy scope."""
    shared, registry = world
    _publish_entry(registry, shared)
    ledger = registry.parent / agent.LEDGER_FILENAME
    real = ledger.with_name("elsewhere.json")
    ledger.rename(real)
    ledger.symlink_to(real)
    refresh_state.record("demo", "compose_failed", registry=registry)
    with pytest.raises(agent.AgentError, match="manifest marked"):
        agent.after_refresh_record("demo", "compose_failed", registry=registry)
    manifest = json.loads((shared / "demo.snapshot.json").read_text(encoding="utf-8"))
    assert manifest["refresh"]["outcome"] == "compose_failed", manifest["refresh"]


def test_a_malformed_entry_withholds_only_itself(tmp_path):
    """One bad value used to make `load_config` raise, which stopped every scope's withdrawal:
    here `demo`'s opt-in is turned off while `acme`'s entry is broken. `demo`'s copy loses its
    calls on its next refresh; `acme` is refused, and its own copy, which may hold calls, is
    withdrawn by its own refresh record, sweep or no sweep."""
    shared, registry = _sourced_scope(tmp_path)
    _scope(registry.parent, registry, "acme")
    acme_dir = tmp_path / "acme-shared"
    acme_dir.mkdir()
    config = registry.parent / agent.CONFIG_FILENAME

    def write(demo_sources: str, acme_sources: str) -> None:
        config.write_text(f'[publish.demo]\ndir = "{shared}"\ninclude_sources = {demo_sources}\n'
                          f'[publish.acme]\ndir = "{acme_dir}"\ninclude_sources = '
                          f'{acme_sources}\n', encoding="utf-8")

    write("true", "true")
    _refresh("demo", registry)
    _refresh("acme", registry)
    assert b"Zqxjcalltranscript" in (shared / "demo.snapshot.db").read_bytes()
    assert (acme_dir / "acme.snapshot.db").exists()

    write("false", '"no"')
    refresh_state.record("acme", "refreshed", registry=registry)
    with pytest.raises(agent.AgentError, match="include_sources must be true or false") as e:
        agent.after_refresh_record("acme", "refreshed", registry=registry)
    assert "WITHDRAWN" in str(e.value)
    assert not (acme_dir / "acme.snapshot.db").exists()
    said = _refresh("demo", registry)
    assert b"Zqxjcalltranscript" not in (shared / "demo.snapshot.db").read_bytes(), said


@pytest.mark.parametrize("dir_value", ["relative/folder", "~zqxjnosuchuser/folder", "a\\u0000b"])
def test_a_bad_dir_is_its_entrys_alone(world, tmp_path, dir_value):
    """A relative `dir` would name whichever folder the refresh ran in, and a `~user` that does
    not exist made `load_config` raise for the whole file. Each is its entry's error now."""
    shared, registry = world
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.acme]\ndir = "{dir_value}"\n[publish.demo]\ndir = "{shared}"\n',
        encoding="utf-8")
    config = agent.load_config(registry)
    assert set(config.broken) == {"acme"} and set(config.targets) == {"demo"}, config
    assert any("snapshot verified" in line for line in _refresh("demo", registry))


def test_a_publish_file_that_does_not_parse_still_withdraws_copies_holding_calls(tmp_path):
    """A typo while turning the opt-in off leaves the file unparseable. Where it publishes is then
    unknown, so the folders recorded are kept and a copy there that may hold calls goes."""
    shared, registry = _sourced_scope(tmp_path)
    _entry(registry, shared, include_sources=True)
    _refresh("demo", registry)
    assert b"Zqxjcalltranscript" in (shared / "demo.snapshot.db").read_bytes()
    (registry.parent / agent.CONFIG_FILENAME).write_text(
        f'[publish.demo]\ndir = "{shared}"\ninclude_sources = fals\n', encoding="utf-8")
    said = _refresh("demo", registry)
    assert not (shared / "demo.snapshot.db").exists(), said
    assert any(line.startswith("WARNING (agent snapshot demo)") for line in said), said
    assert _ledger(registry) == {"demo": [str(shared)]}


def test_a_pass_withdraws_once_not_once_per_scope(world, monkeypatch):
    """The sweep reads every entry and every recorded folder; a pass of N scopes ran it N times."""
    import subprocess

    shared, registry = world
    calls = []
    monkeypatch.setattr(agent, "withdraw_dropped",
                        lambda reg=None: calls.append(reg) or ([], []))
    refresh.run_pass(registry, runner=lambda argv, env: subprocess.CompletedProcess(argv, 0, "", ""),
                     log=lambda line: None)
    assert len(calls) == 1, calls
    calls.clear()
    assert cli.main(["refresh-record", "--scope", "demo", "--scope", "acme", "--outcome",
                     "refreshed", "--registry", str(registry), "--quiet"]) == 0
    assert len(calls) == 1, calls


def test_a_call_changing_moves_nothing_a_snapshot_without_calls_shows(tmp_path):
    """The fingerprint, `index_version` and `published` were taken over the whole live index, so
    an agent could watch them move whenever a call was added or edited, though it reads no call."""
    shared, registry = _sourced_scope(tmp_path)
    agent.publish("demo", shared, registry=registry)
    manifest_path = shared / "demo.snapshot.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))

    with IndexStore(str(registry.parent / "demo.db")) as s:
        for doc_id, text in (("zqxjcallnote", _CALL_TEXT + " Edited."),
                             ("zqxjsecondcall", "Another call.")):
            doc = Document(doc_id=doc_id, source_path=str(tmp_path / f"{doc_id}.md"),
                           source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                           source_pages=1, document_class="conversation", title=doc_id,
                           status="active", doc_type="reference", confidence="stated",
                           vault="demo-vault", tier=3, domains=[], supersedes=[])
            s.upsert(doc, [Chunk(chunk_id=f"{doc_id}#c00000", doc_id=doc_id, kind="passage",
                                 text=text, path=[doc_id], level=1, n_chars=len(text),
                                 document_class="conversation")],
                     markdown_path="x", markdown_mtime=0.0, markdown_sha256="d" * 64)
        s.store_vectors([("zqxjsecondcall#c00000", [0.5, 0.5])], "m#raw")
        s.replace_identity([("ada-lovelace", "Zqxjcallperson", "person", None),
                            ("grace", "Zqxjsecondperson", "person", None)])
        s.set_document_entities("zqxjsecondcall", [("grace", "Zqxjsecondperson")])

    p = agent.publish("demo", shared, registry=registry,
                      now=datetime.now(UTC) + timedelta(hours=1))
    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert not p.rewrote
    for key in ("fingerprint", "index_version", "published"):
        assert after[key] == before[key], key
    # The version named is the one the agent's engine computes over the copy it reads.
    with IndexStore(str(shared / "demo.snapshot.db"), migrate=False, immutable=True) as snap:
        assert snap.index_version == after["index_version"]
    # A snapshot that carries calls still moves with them.
    q = agent.publish("demo", shared, registry=registry, include_sources=True)
    with IndexStore(str(registry.parent / "demo.db")) as s:
        s.remove("zqxjsecondcall")
    assert agent.publish("demo", shared, registry=registry, include_sources=True).rewrote, q


def test_the_fingerprint_hashes_what_the_copy_holds_and_nothing_else(world):
    """It counted vectors no passage holds, which the copy also carried — each under a `chunk_id`
    naming the document it came from — and mentions of documents the copy leaves out. Neither is
    in the copy now, and neither moves the fingerprint."""
    shared, registry = world
    db = registry.parent / "demo.db"
    gone = _add_real_doc(registry, "unmatched", _BODY)
    gone.unlink()                       # excluded: its note no longer backs the index
    before = agent.publish("demo", shared, registry=registry)
    manifest = json.loads((shared / "demo.snapshot.json").read_text(encoding="utf-8"))
    with IndexStore(str(db)) as s:
        s.db.execute("INSERT INTO chunk_vectors(chunk_id, vector, embed_model, dim) "
                     "VALUES ('zqxjorphancall#c00000', x'00000000', 'm#raw', 1)")
        s.replace_identity([("ada", "Ada", "person", None)])
        s.set_document_entities("unmatched", [("ada", "Ada")])
    p = agent.publish("demo", shared, registry=registry)
    assert before.excluded == 1 and not p.rewrote
    after = json.loads((shared / "demo.snapshot.json").read_text(encoding="utf-8"))
    assert after["fingerprint"] == manifest["fingerprint"]
    (shared / "demo.snapshot.json").unlink()      # forces a rewrite of the copy
    agent.publish("demo", shared, registry=registry)
    assert b"zqxjorphancall" not in (shared / "demo.snapshot.db").read_bytes()

def test_a_snapshot_whose_manifest_does_not_record_include_sources_is_withdrawn(world):
    """Every snapshot the previous engine published carried calls and no `include_sources`."""
    shared, registry = world
    manifest_path = shared / "demo.snapshot.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["include_sources"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert agent.annotate("demo", shared, registry=registry) == "withdrawn"
    assert not (shared / "demo.snapshot.db").exists()


def test_a_snapshot_published_without_sources_is_only_marked_on_a_failed_refresh(world):
    shared, registry = world
    assert agent.annotate("demo", shared, registry=registry) == "marked"
    assert (shared / "demo.snapshot.db").exists()


def test_a_snapshots_rowids_do_not_show_what_was_left_out(tmp_path):
    shared, registry = _sourced_scope(tmp_path)
    _add_real_doc(registry, "later", _BODY)
    agent.publish("demo", shared, registry=registry)
    with IndexStore(str(shared / "demo.snapshot.db"), migrate=False, immutable=True) as snap:
        for table in ("documents", "chunks", "chunk_vectors", "chunks_fts_docsize"):
            key = "id" if table == "chunks_fts_docsize" else "rowid"
            ids = [r[0] for r in snap.db.execute(f"SELECT {key} FROM {table} ORDER BY 1")]
            assert ids == list(range(1, len(ids) + 1)), (table, ids)
        docs = [r[0] for r in snap.db.execute("SELECT doc_id FROM documents ORDER BY rowid")]
        assert docs == ["decision", "later"]
    _hide(registry)
    res, err = _call(_cfg(shared), "search", {"scope": "demo", "query": "snapshot publishing"})
    assert not err and {p["expand_ref"] for p in res["passages"]} == \
        {"demo/decision#c00000", "demo/later#c00000"}, "FTS must follow the renumbered rowids"


def test_publish_snapshot_into_another_folder_takes_no_calls(tmp_path):
    shared, registry = _sourced_scope(tmp_path)
    _entry(registry, shared, include_sources=True)
    other = tmp_path / "other"
    other.mkdir()
    assert cli.main(["publish-snapshot", "--scope", "demo", "--dir", str(other),
                     "--registry", str(registry)]) == 0
    assert b"Zqxjcalltranscript" not in (other / "demo.snapshot.db").read_bytes()
    assert cli.main(["publish-snapshot", "--scope", "demo", "--registry", str(registry)]) == 0
    assert b"Zqxjcalltranscript" in (shared / "demo.snapshot.db").read_bytes()
