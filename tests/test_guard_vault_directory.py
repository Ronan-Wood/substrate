"""A guarded vault whose directory name is not its scope name stays withheld on every read path.

The guard withheld the SCOPE name (the manifest's `name`), and the index records the vault
DIRECTORY name on every note it composes (`vault.resolve_vaults`). So wherever the two differed, every
exclusion withheld a name no document carries, and a guarded vault answered as if nothing guarded it.
The demo vault is that case: its directory is `demo-vault` and its scope is `demo`.

Every world here is a REAL compose, so the name on the notes is whatever compose writes. The server
tests used a hand-built index that recorded the scope name, which is how they passed over the bug.

Each withheld assertion has an open twin: the same call with nothing withheld returns the guarded
vault's notes. Without it, an arm that returned nothing at all would pass.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from substrate import cli, guard, render, scopes, stack  # noqa: E402
from substrate.mcp import server  # noqa: E402
from substrate.retrieve import retriever  # noqa: E402
from substrate.store.index_store import IndexStore  # noqa: E402

_VAULTS = Path(__file__).resolve().parent.parent / "vaults"
SCOPE = "demo"
GUARDED = "demo-vault"          # the directory, and the name compose records on its notes
INHERITED = "demo-core-vault"
KEY = "fake-hash-embedder#raw"

# Words from both vaults' notes, so the OR fallback reaches every note in each.
QUERY = "engine boundary manifest inheritance retrieval partitioning goroutines cadence writing"
WIDE = 200


# ---------------------------------------------------------------------- the world

class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault = root / GUARDED
        self.state = root / "guard-state.json"
        self.db = root / "out" / "demo.db"
        self.registry = root / "scopes.toml"

    def vouch(self, active: str = SCOPE) -> None:
        self.state.write_text(json.dumps({"heartbeat": time.time(), "activeScope": active}),
                              encoding="utf-8")

    def lapse(self) -> None:
        self.state.unlink(missing_ok=True)

    def cfg(self) -> server.Config:
        return server.Config(str(self.registry), stack.build(lexical_only=True))


def _guard_the_manifest(vault: Path, state: Path) -> None:
    """`guard_state` as the manifest's FIRST line: a key appended at the end would land inside
    `[reference_pins]`, which is the other bug."""
    manifest = vault / ".substrate.toml"
    manifest.write_text(f'guard_state = "{state}"\n' + manifest.read_text(encoding="utf-8"),
                        encoding="utf-8")


def _compose(root: Path, w: _World, monkeypatch) -> None:
    monkeypatch.setattr(scopes, "engine_home", lambda: root)
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(w.vault), "--db", str(w.db),
                       "--index-root", str(root / "out" / "demo-index"),
                       "--registry", str(w.registry)])
    assert rc == 0, err.getvalue()


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 + 0.01 for b in digest[:16]]


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> _World:
    w = _World(tmp_path)
    shutil.copytree(_VAULTS / GUARDED, w.vault)
    shutil.copytree(_VAULTS / INHERITED, tmp_path / INHERITED)
    _guard_the_manifest(w.vault, w.state)
    _compose(tmp_path, w, monkeypatch)
    with IndexStore(str(w.db)) as s:
        s.store_vectors([(cid, _vector(text)) for cid, text in s.chunks_missing_vectors(KEY)], KEY)
    return w


def _verdict(w: _World) -> guard.Verdict:
    """The verdict the server's own chokepoint reaches, not one this file builds."""
    _, store, verdict = server._open(SCOPE, w.cfg())
    store.close()
    return verdict


def _vaults(hits) -> set[str | None]:
    return {h.vault for h in hits}


# ---------------------------------------------------------------------- the fakes

class _Embedder:
    key = KEY
    model = "fake-hash-embedder"

    def embed_query(self, text: str) -> list[float]:
        return _vector(text)


class _Expander:
    model = "fake-hyde"

    def expand(self, query: str) -> str:
        return query + " an invented paragraph about composition and goroutines"


class _MultiQuery:
    def variants(self, query: str) -> list[str]:
        return ["manifest inheritance", "engine boundary call"]


class _Reranker:
    model = "fake-rerank"
    fallback_queries = 0

    def rerank(self, query: str, hits: list) -> tuple[list, bool]:
        return list(reversed(hits)), True


# ---------------------------------------------------------------------- the precondition

def test_compose_records_the_directory_name_not_the_scope_name(world: _World) -> None:
    """The fact the bug rests on. If compose ever records the scope name, the other tests here
    stop testing the mismatch and this one says so."""
    with IndexStore(str(world.db), migrate=False) as s:
        names = {r[0] for r in s.db.execute("SELECT DISTINCT vault FROM documents")}
    assert names == {GUARDED, INHERITED}
    assert SCOPE not in names


def test_the_verdict_withholds_the_name_the_index_records(world: _World) -> None:
    verdict = _verdict(world)
    assert verdict.reason
    assert GUARDED in verdict.withhold, verdict.withhold
    assert INHERITED not in verdict.withhold, "an inherited vault was never the guard's to withhold"

    world.vouch()
    assert _verdict(world).vouched


# ---------------------------------------------------------------------- every arm, at the store

def _lexical(s: IndexStore, w: frozenset[str]):
    return s.search(QUERY, k=WIDE, kind="passage", include_sources=True, withheld_vaults=w)


def _outlines(s: IndexStore, w: frozenset[str]):
    return s.search(QUERY, k=WIDE, kind="outline", include_sources=True, withheld_vaults=w)


def _vector_cached(s: IndexStore, w: frozenset[str]):
    def refuse(*a, **kw):
        raise AssertionError("the scan ran; this arm is the cached matrix")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(IndexStore, "_vector_scan", refuse)
        return s.vector_search(_vector(QUERY), KEY, k=WIDE, include_sources=True,
                               withheld_vaults=w)


def _vector_scan(s: IndexStore, w: frozenset[str]):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(IndexStore, "_vector_search_cached", lambda *a, **kw: None)
        return s.vector_search(_vector(QUERY), KEY, k=WIDE, include_sources=True,
                               withheld_vaults=w)


def _browse(s: IndexStore, w: frozenset[str]):
    rows, _ = s.browse(statuses=None, include_sources=True, withheld_vaults=w, limit=WIDE)
    return [type("Row", (), {"vault": r["vault"]}) for r in rows]


ARMS = {"lexical": _lexical, "outlines": _outlines, "vector-cached": _vector_cached,
        "vector-scan": _vector_scan, "documents-browse": _browse}


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_every_store_arm_withholds_the_guarded_vault(world: _World, arm: str) -> None:
    run = ARMS[arm]
    with IndexStore(str(world.db), migrate=False) as s:
        assert GUARDED in _vaults(run(s, frozenset())), f"{arm}: the open twin finds nothing"
        withheld = _vaults(run(s, _verdict(world).withhold))
    assert GUARDED not in withheld, f"{arm} returned the guarded vault's notes"
    assert INHERITED in withheld, f"{arm}: the inherited vault must still answer"


def test_the_whole_retriever_withholds_with_every_arm_wired(world: _World) -> None:
    """HyDE, multi-query (lexical and vector variants), route, neighbours, rerank and outline
    records, in one call. Each arm either applies the exclusion or is scoped to a `doc_id` drawn
    from a hit that passed it."""
    def run(withheld: frozenset[str]):
        with IndexStore(str(world.db), migrate=False) as s:
            return retriever.retrieve(
                s, QUERY, k=WIDE, statuses=None, include_sources=True, withheld_vaults=withheld,
                route=True, expand=True, embedder=_Embedder(), expander=_Expander(),
                multiquery=_MultiQuery(), reranker=_Reranker(), with_outlines=WIDE)

    opened = run(frozenset())
    assert opened.trace.variants == 2 and opened.trace.expanded and opened.trace.vector, \
        "the fakes must actually run, or this proves nothing about their arms"
    assert GUARDED in _vaults(opened.passages) and GUARDED in _vaults(opened.outlines)

    result = run(_verdict(world).withhold)
    assert GUARDED not in _vaults(result.passages)
    assert GUARDED not in _vaults(result.outlines)
    assert INHERITED in _vaults(result.passages)


# ---------------------------------------------------------------------- the tools

def _call_raw(tool: str, args: dict, w: _World) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}}, w.cfg())["result"]


def _call(tool: str, args: dict, w: _World) -> dict:
    body = _call_raw(tool, args, w)
    assert not body.get("isError"), body["content"][0]["text"][:400]
    return json.loads(body["content"][0]["text"])


def test_search_withholds_the_guarded_vault(world: _World) -> None:
    args = {"scope": SCOPE, "query": QUERY, "k": 20, "include_sources": True,
            "include_archived": True}
    world.vouch()
    assert GUARDED in {p["vault"] for p in _call("search", args, world)["passages"]}

    world.lapse()
    hit = _call("search", args, world)
    assert {p["vault"] for p in hit["passages"]} == {INHERITED}
    assert any("not reporting" in n for n in hit["filters"]["notes"])


def test_documents_withholds_the_guarded_vault_even_when_asked_for_by_name(world: _World) -> None:
    for args in ({"scope": SCOPE, "include_sources": True, "include_archived": True,
                  "limit": 500},
                 {"scope": SCOPE, "vault": GUARDED, "include_sources": True,
                  "include_archived": True}):
        world.vouch()
        assert GUARDED in {d["vault"] for d in _call("documents", args, world)["documents"]}

        world.lapse()
        browsed = _call("documents", args, world)
        assert GUARDED not in {d["vault"] for d in browsed["documents"]}, args
        assert any("not reporting" in n for n in browsed["filters"]["notes"])


@pytest.mark.parametrize("mode", ["passage", "note"])
def test_expand_refuses_a_guarded_passage(world: _World, mode: str) -> None:
    with IndexStore(str(world.db), migrate=False) as s:
        chunk_id = s.db.execute(
            "SELECT c.chunk_id FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
            "WHERE d.vault = ? AND c.kind = 'passage' ORDER BY c.chunk_id LIMIT 1",
            (GUARDED,)).fetchone()[0]
    ref = render.expand_ref(SCOPE, chunk_id)

    world.vouch()
    assert _call("expand", {"expand_ref": ref, "mode": mode}, world)["passage"]["vault"] == GUARDED

    world.lapse()
    refused = _call_raw("expand", {"expand_ref": ref, "mode": mode}, world)
    assert refused.get("isError"), f"{mode}: expand returned a guarded passage"
    assert "withheld" in refused["content"][0]["text"]


# ---------------------------------------------------------------------- the swallowed guard

def test_compose_refuses_a_guard_written_below_reference_pins(tmp_path: Path, monkeypatch) -> None:
    """Appending `guard_state` to the demo manifest lands it inside `[reference_pins]`. It is a
    string, so it passed the pins' shape check and the vault composed with no guard at all."""
    w = _World(tmp_path)
    shutil.copytree(_VAULTS / GUARDED, w.vault)
    shutil.copytree(_VAULTS / INHERITED, tmp_path / INHERITED)
    manifest = w.vault / ".substrate.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + f'guard_state = "{w.state}"\n',
                        encoding="utf-8")

    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(w.vault), "--db", str(w.db),
                       "--index-root", str(tmp_path / "out" / "demo-index"),
                       "--registry", str(w.registry)])
    assert rc != 0, "composed a vault whose guard TOML had swallowed into a table"
    assert "'guard_state'" in err.getvalue() and "[reference_pins]" in err.getvalue(), \
        err.getvalue()
    assert not w.db.exists()


def test_a_guard_moved_below_a_table_after_compose_still_withholds(world: _World) -> None:
    """Compose refusing is not enough: the index it built earlier is still served, and the guard
    reads the manifest at query time."""
    manifest = world.vault / ".substrate.toml"
    text = manifest.read_text(encoding="utf-8")
    line = f'guard_state = "{world.state}"\n'
    assert text.startswith(line)
    manifest.write_text(text[len(line):] + line, encoding="utf-8")

    world.vouch()
    hit = _call("search", {"scope": SCOPE, "query": QUERY, "k": 20, "include_sources": True},
                world)
    assert {p["vault"] for p in hit["passages"]} == {INHERITED}
    assert any("guard_state" in n for n in hit["filters"]["notes"]), hit["filters"]["notes"]
