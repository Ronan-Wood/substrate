"""A manifest that still declares `guard_state` composes and withholds nothing.

Manifests already on disk carry the key, and Scripta 0.3.5 still writes it. The engine reads no such
key, so a scope whose project vault declares it returns that vault's own notes through every MCP
read path and takes a write plan, whatever the state file says or whether it exists.

Every world here is a REAL compose of the demo pair, whose directory (`demo-vault`) is not its scope
name (`demo`), so the name on the notes is whatever compose writes.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from substrate import cli, render, scopes, stack  # noqa: E402
from substrate.mcp import server  # noqa: E402
from substrate.store.index_store import IndexStore  # noqa: E402

_VAULTS = Path(__file__).resolve().parent.parent / "vaults"
SCOPE = "demo"
PROJECT = "demo-vault"
INHERITED = "demo-core-vault"

# Words from both vaults' notes, so the OR fallback reaches every note in each.
QUERY = "engine boundary manifest inheritance retrieval partitioning goroutines cadence writing"

# Each state a guard read as "nothing vouches": no file, a beat long past, another scope named.
STATES = {
    "missing": None,
    "stale": {"heartbeat": 0, "activeScope": SCOPE},
    "another-scope": {"heartbeat": time.time(), "activeScope": "something-else"},
}


def _compose(root: Path, monkeypatch) -> tuple[int, str]:
    monkeypatch.setattr(scopes, "engine_home", lambda: root)
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(root / PROJECT), "--db", str(root / "out" / "demo.db"),
                       "--index-root", str(root / "out" / "demo-index"),
                       "--registry", str(root / "scopes.toml")])
    return rc, err.getvalue()


def _declare(root: Path, state: dict | None) -> None:
    """`guard_state` as the manifest's first line, which is where a top-level key must go."""
    shutil.copytree(_VAULTS / PROJECT, root / PROJECT)
    shutil.copytree(_VAULTS / INHERITED, root / INHERITED)
    path = root / "guard-state.json"
    if state is not None:
        path.write_text(json.dumps(state), encoding="utf-8")
    manifest = root / PROJECT / ".substrate.toml"
    manifest.write_text(f'guard_state = "{path}"\n' + manifest.read_text(encoding="utf-8"),
                        encoding="utf-8")


@pytest.fixture(params=sorted(STATES))
def registry(request, tmp_path: Path, monkeypatch) -> Path:
    _declare(tmp_path, STATES[request.param])
    rc, err = _compose(tmp_path, monkeypatch)
    assert rc == 0, err
    return tmp_path / "scopes.toml"


def _call(tool: str, args: dict, registry: Path) -> dict:
    cfg = server.Config(str(registry), stack.build(lexical_only=True))
    body = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}}, cfg)["result"]
    assert not body.get("isError"), body["content"][0]["text"][:400]
    return json.loads(body["content"][0]["text"])


def test_a_manifest_declaring_guard_state_composes(tmp_path: Path, monkeypatch) -> None:
    _declare(tmp_path, None)
    rc, err = _compose(tmp_path, monkeypatch)
    assert rc == 0, err
    assert (tmp_path / "out" / "demo.db").is_file()


def test_search_returns_the_project_vaults_own_notes(registry: Path) -> None:
    hit = _call("search", {"scope": SCOPE, "query": QUERY, "k": 20, "include_sources": True,
                           "include_archived": True}, registry)
    assert {p["vault"] for p in hit["passages"]} == {PROJECT, INHERITED}
    assert not any("guard" in n or "withheld" in n for n in hit["filters"]["notes"]), \
        hit["filters"]["notes"]


def test_documents_returns_the_project_vaults_own_notes(registry: Path) -> None:
    browsed = _call("documents", {"scope": SCOPE, "include_sources": True,
                                  "include_archived": True, "limit": 100}, registry)
    assert {d["vault"] for d in browsed["documents"]} == {PROJECT, INHERITED}
    assert not any("guard" in n or "withheld" in n for n in browsed["filters"]["notes"]), \
        browsed["filters"]["notes"]

    own = _call("documents", {"scope": SCOPE, "vault": PROJECT, "include_sources": True,
                              "include_archived": True, "limit": 100}, registry)
    assert own["total"] > 0 and {d["vault"] for d in own["documents"]} == {PROJECT}


@pytest.mark.parametrize("mode", ["passage", "note"])
def test_expand_returns_a_passage_of_the_project_vault(registry: Path, mode: str) -> None:
    """The ref is read from the index, not from a search, so this holds for `expand` alone."""
    with IndexStore(str(registry.parent / "out" / "demo.db"), migrate=False) as s:
        chunk_id = s.db.execute(
            "SELECT c.chunk_id FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
            "WHERE d.vault = ? AND c.kind = 'passage' ORDER BY c.chunk_id LIMIT 1",
            (PROJECT,)).fetchone()[0]
    out = _call("expand", {"expand_ref": render.expand_ref(SCOPE, chunk_id), "mode": mode},
                registry)
    assert out["passage"]["vault"] == PROJECT
    if mode == "note":
        assert out["note"]["text"], out["note"]


_NOTE = """---
status: active
doc_type: decision
confidence: proposed
---

# A decision

Written into a vault that declares guard_state.
"""


@pytest.mark.parametrize("tool", ["ingest", "amend"])
def test_a_write_plan_into_the_project_vault_is_not_refused(registry: Path, tool: str) -> None:
    """The retired guard refused every write into a vault nothing vouched for."""
    if tool == "ingest":
        args = {"scope": SCOPE, "content": _NOTE, "filename": "d.md"}
    else:
        (registry.parent / PROJECT / "02-areas" / "d.md").write_text(_NOTE, encoding="utf-8")
        args = {"scope": SCOPE, "path": "02-areas/d.md", "content": _NOTE + "\nOne more line.\n"}
    assert _call(tool, args, registry)["plan"]["confirm_token"]
