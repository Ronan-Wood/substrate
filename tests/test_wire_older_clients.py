"""A wire-4 engine still serves a wire-2 or wire-3 client, and a refusal from each PR that adds
one to the write tools reaches that client as an ordinary tool refusal.

Wire 4 leaves `MIN_VERSION` at 1 (`docs/client-contract.md`): nothing a wire-3 client decodes is
removed or renamed, and the write tools' changed meanings reach an older client as tool refusals it
already handles, or as calls that now plan where they refused. These tests check that against a
real engine on a socket, serving a granted client as Scripta's engine serves Scripta, which speaks
wire 2. Every write here is a plan, which writes nothing, so the reply is the whole result.

`data/contract-0.2.2.json` is the contract 0.2.2 shipped, the last release at wire 3, byte for byte.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate import grants, wire  # noqa: E402
from substrate.mcp import bridge  # noqa: E402
from test_engine_keys import RENAMED  # noqa: E402
from test_grants import _engine_cfg  # noqa: E402
from test_local_socket import _engine, _socket_dir  # noqa: E402
from test_mcp_server import _NOTE, _fixture  # noqa: E402
from test_wire import _canned, _raw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WIRE_3 = Path(__file__).resolve().parent / "data" / "contract-0.2.2.json"
OLDER = ("2", "3")
MANIFEST = 'name = "demo"\ninherits = []\n'
HELD = ("---\nsubstrate_status: active\nsubstrate_doc_type: decision\n"
        "substrate_confidence: proposed\nclass: conversation\n---\n\n# A call\n\nSaid.\n")


@contextlib.contextmanager
def _world(manifest: str | None = MANIFEST, files: dict[str, str] | None = None):
    """A registered scope `demo` whose project vault holds `files` beside its one note and has no
    `04-synthesis`, served on a socket to a granted client. `manifest` None removes the vault's
    `.substrate.toml`. Yields the socket and the headers that client sends."""
    root, registry = _fixture(manifest=True)
    vault = root / "demo-vault"
    if manifest is None:
        (vault / ".substrate.toml").unlink()
    else:
        (vault / ".substrate.toml").write_text(manifest, encoding="utf-8")
    for rel, text in (files or {}).items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text, encoding="utf-8")
    _, token = grants.issue("scripta")
    caller = {grants.CLIENT_HEADER: "scripta", grants.GRANT_HEADER: token.read_text().strip()}
    with _socket_dir() as d, _engine(d / "s.sock", _engine_cfg(registry)):
        yield d / "s.sock", caller


def _tool(sock: tuple[Path, dict], version: str, name: str,
          arguments: dict) -> tuple[str | None, dict]:
    """One `tools/call` stating `Substrate-Wire: version`: (the reply's wire header, its body)."""
    path, caller = sock
    frame = {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
    status, resp, body = _raw(path, frame, {"Content-Type": "application/json",
                                            wire.HEADER: version, **caller})
    assert status == 200, body
    return resp.getheader(wire.HEADER), json.loads(body)


def test_the_engine_speaks_wire_4_and_serves_clients_from_wire_1() -> None:
    assert (wire.MIN_VERSION, wire.VERSION) == (1, 4)
    for version in ("1", *OLDER, "4"):
        assert wire.unserved([version]) is None, version
    assert "update the engine" in wire.unserved(["5"])


@pytest.mark.parametrize("version", OLDER)
def test_a_wire_2_or_3_request_is_dispatched_and_answered_in_wire_4(version: str) -> None:
    with _world() as sock:
        status, resp, body = _raw(sock[0], {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
                                  {"Content-Type": "application/json", wire.HEADER: version})
        assert status == 200 and "result" in json.loads(body), body
        assert resp.getheader(wire.HEADER) == str(wire.VERSION)
        header, reply = _tool(sock, version, "list_scopes", {})
    assert header == str(wire.VERSION)
    scopes = json.loads(reply["result"]["content"][0]["text"])["scopes"]
    assert [s["scope"] for s in scopes] == ["demo"], scopes


INGEST = {"scope": "demo", "content": _NOTE, "filename": "d.md"}

# (manifest, files, tool, arguments, words the refusal must hold). Each call took the write at
# 0.2.2. The #69 manifest declares `skip`, so the unknown key is what refuses it.
REFUSALS = {
    "58-an-omitted-folder-without-04-synthesis": (
        MANIFEST, {}, "ingest", INGEST, "no `folder` was given"),
    "58-amend-of-a-tier-1-note-under-a-nested-folder": (
        MANIFEST, {"02-areas/00-operator/profile.md": _NOTE}, "amend",
        {"scope": "demo", "path": "02-areas/00-operator/profile.md", "content": _NOTE + "\nMore.\n"},
        "the operator or reference tier"),
    "58-a-project-vault-with-no-manifest": (
        None, {"02-areas/x.md": _NOTE}, "ingest", {**INGEST, "folder": "02-areas"},
        "has no .substrate.toml"),
    "54-a-folder-the-vault-skips": (
        MANIFEST + 'skip = ["02-areas"]\n', {"02-areas/x.md": _NOTE}, "ingest",
        {**INGEST, "folder": "02-areas"}, "is in this vault's skip list"),
    "54-a-source-meta-in-another-case": (
        MANIFEST, {"02-areas/x.md": _NOTE}, "ingest",
        {**INGEST, "filename": "_META.md", "folder": "02-areas"},
        "named as a source's _meta.md (in any letter case)"),
    "69-an-unknown-manifest-key": (
        MANIFEST + 'skip = ["99-templates"]\nwritable_foldrs = ["02-areas"]\n',
        {"02-areas/x.md": _NOTE}, "ingest", {**INGEST, "folder": "02-areas"},
        "'writable_foldrs' is not a key the manifest defines"),
}


@pytest.mark.parametrize("version", OLDER)
@pytest.mark.parametrize("case", REFUSALS)
def test_a_new_refusal_reaches_an_older_client_as_a_tool_refusal(case: str, version: str) -> None:
    """Shaped exactly as every refusal before wire versioning: a JSON-RPC result, `isError`, one
    text item. Never a JSON-RPC error, which a client reads as the call failing to arrive."""
    manifest, files, tool, arguments, words = REFUSALS[case]
    with _world(manifest, files) as sock:
        header, reply = _tool(sock, version, tool, arguments)
    assert header == str(wire.VERSION)
    assert set(reply) == {"jsonrpc", "id", "result"}, reply
    assert set(reply["result"]) == {"content", "isError"} and reply["result"]["isError"] is True
    [content] = reply["result"]["content"]
    assert set(content) == {"type", "text"} and content["type"] == "text", content
    assert words in content["text"], content["text"]


@pytest.mark.parametrize("version", OLDER)
def test_amend_of_a_note_held_under_a_renamed_key_is_refused_as_0_2_2_refused_it(
        version: str) -> None:
    """#74: a vault that renames `class` reads `class: conversation` as the note's own, and holds
    the note. 0.2.2 refused the same amend, for the frozen class; only the words differ."""
    with _world(MANIFEST + "\n" + RENAMED, {"02-areas/call.md": HELD}) as sock:
        _, reply = _tool(sock, version, "amend", {"scope": "demo", "path": "02-areas/call.md",
                                                  "content": HELD + "\nMore.\n"})
    assert reply["result"]["isError"] is True, reply
    assert "its `class` holds `conversation`" in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize("version", OLDER)
@pytest.mark.parametrize("tool, arguments", [
    # #58 widened `folder`: one of the three names an older client sends still plans.
    ("ingest", {**INGEST, "folder": "02-areas"}),
    # #58: with no `writable_folders`, amend takes a note at the vault root, which 0.2.2 refused.
    ("amend", {"scope": "demo", "path": "root.md", "content": _NOTE + "\nMore.\n"}),
])
def test_what_an_older_client_sends_still_plans(tool: str, arguments: dict, version: str) -> None:
    with _world(MANIFEST, {"02-areas/x.md": _NOTE, "root.md": _NOTE}) as sock:
        _, reply = _tool(sock, version, tool, arguments)
    assert not reply["result"].get("isError"), reply
    assert json.loads(reply["result"]["content"][0]["text"])["plan"]["confirm_token"]


@pytest.mark.parametrize("version", OLDER)
def test_an_older_connect_client_reads_a_new_refusal_as_a_refusal(version: str) -> None:
    """The client's side: `--connect` at wire 2 or 3 reads a reply stating wire 4, and hands the
    refusal on as the error result it is, not as a wire mismatch. `bridge.py` and `local_socket.py`
    are 0.2.2's, and `wire.unread` differs from 0.2.2's only by `VERSION`, so this is that client.
    It refuses a reply one version older than its own, so it is speaking `version`."""
    with _world() as sock:
        header, reply = _tool(sock, version, "ingest", INGEST)
    body = json.dumps(reply).encode()

    def _read(stated: str) -> dict:
        with mock.patch.object(wire, "VERSION", int(version)), \
                _canned(f"{wire.HEADER}: {stated}\r\n", body=body) as (path, _):
            return bridge.Remote(path)(reply["id"], "ingest", INGEST)

    out = _read(header)
    assert out["result"]["isError"] is True, out
    assert out["result"]["content"] == reply["result"]["content"]
    assert "substrate wire" not in out["result"]["content"][0]["text"]
    older = _read(str(int(version) - 1))["result"]["content"][0]["text"]
    assert f"speaks substrate wire {int(version) - 1}, older than this client's {version}" in older


def test_nothing_a_wire_3_client_decodes_is_removed_renamed_or_given_a_new_value() -> None:
    """Against 0.2.2's contract: every reply object keeps every key it had, and every other
    section, the closed vocabularies among them, is unchanged. The contract holds key names, not
    types; the golden frames hold the shapes. `tiers` is left out: it is the eval's measurement,
    which no client decodes."""
    then = json.loads(WIRE_3.read_text(encoding="utf-8"))
    now = json.loads((ROOT / "contract" / "contract.json").read_text(encoding="utf-8"))
    assert then["wire"]["version"] == 3
    for name, keys in then["payloads"].items():
        assert set(keys) <= set(now["payloads"].get(name, ())), name
    for name, parts in then["extends"].items():
        assert now["extends"].get(name) == parts, name
    assert {**then["wire"], "version": None} == {**now["wire"], "version": None}
    for section in set(then) - {"wire", "payloads", "extends", "tiers"}:
        assert now[section] == then[section], section
