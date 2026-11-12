"""The engine's write log (`substrate.writelog`): one test per crash point of its design (§3) and
per stamp verdict (§5), through the real granted write path where one exists.

Every test runs against the suite's own engine home (`conftest`), never `~/.substrate`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate import cli, grants, stack, writelog  # noqa: E402
from substrate.mcp import server  # noqa: E402
from substrate.models import Chunk, Document  # noqa: E402
from substrate.store.index_store import IndexStore  # noqa: E402
from test_grants import _ARGS, _bridge, _call, _plan_and_commit, _world  # noqa: E402
from test_mcp_server import _AMENDED, _NOTE, _fixture  # noqa: E402

_WORDS = "Written through the MCP surface, which is a write and therefore two-phase."


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lines() -> list[dict]:
    raw = writelog.path().read_bytes() if writelog.path().exists() else b""
    return [json.loads(line) for line in raw.splitlines()]


def _ingest(registry: Path, sock: Path) -> dict | str:
    grants.issue("claude-code")
    return _plan_and_commit(_bridge(registry, sock), "ingest", _ARGS)[1]


def _amend(folder: Path, registry: Path, sock: Path) -> tuple[dict, dict | str]:
    (folder / "d.md").write_text(_NOTE, encoding="utf-8")
    grants.issue("claude-code")
    return _plan_and_commit(_bridge(registry, sock), "amend",
                            {"scope": "demo", "path": "04-synthesis/d.md", "content": _AMENDED})


def _check(registry: Path) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(["writes", "check", "--registry", str(registry), "--json"])
    return code, json.loads(out.getvalue())


def _index(registry: Path, note: Path, *, sha: str | None = "file") -> None:
    """Index `note` in the demo scope as compose would: its source_path and note_sha256."""
    db = registry.parent / "demo.db"
    with IndexStore(str(db)) as s:
        doc = Document(doc_id="d", source_path=str(note), source_sha256=_sha(note),
                       source_pages=1, document_class="reference-frozen", title="D",
                       status="active", doc_type="decision", confidence="proposed",
                       vault="demo-vault", tier=3,
                       note_sha256=_sha(note) if sha == "file" else sha)
        ch = Chunk(chunk_id="d#c00000", doc_id="d", kind="passage", text=_WORDS, path=["D"],
                   level=1, n_chars=len(_WORDS), document_class="reference-frozen")
        s.upsert(doc, [ch], markdown_path=str(note), markdown_mtime=0.0,
                 markdown_sha256="d" * 64)


def _cfg(registry: Path, writes: str = "granted") -> server.Config:
    return server.Config(str(registry), stack.build(lexical_only=True), writes=writes)


def _search_provenance(registry: Path) -> dict:
    cfg = _cfg(registry)
    err, out = _call(cfg, "search", {"scope": "demo", "query": "MCP surface two-phase"})
    assert not err, out
    [hit] = [p for p in out["passages"] if p["expand_ref"].endswith("d#c00000")]
    return hit["provenance"]


# --------------------------------------------------------------------------- §3 crash points

def test_a_write_the_log_cannot_record_does_not_run() -> None:
    """Before 1: the intent cannot be appended, so the note is never touched."""
    with _world() as (folder, registry, sock):
        writelog.home().mkdir(parents=True, exist_ok=True)
        os.symlink(folder / "elsewhere.jsonl", writelog.path())
        out = _ingest(registry, sock)
        assert isinstance(out, str) and "write log" in out and "No note was written" in out, out
        assert not (folder / "d.md").exists()
        plan, out = _amend(folder, registry, sock)
        assert isinstance(out, str) and "The note was not changed" in out, out
        assert (folder / "d.md").read_text(encoding="utf-8") == _NOTE
    assert not (folder / "elsewhere.jsonl").exists()


def test_the_intent_is_durable_before_the_note_is_touched_and_done_after() -> None:
    with _world() as (folder, registry, sock):
        seen: list[list[dict]] = []
        real = writelog._append

        def spy(fd, record, **kw):
            if record["phase"] == "done":
                seen.append(_lines())
                assert (folder / "d.md").exists(), "done before the note was written"
            else:
                assert not (folder / "d.md").exists(), "the note before its intent"
            return real(fd, record, **kw)

        writelog._append, saved = spy, writelog._append
        try:
            out = _ingest(registry, sock)
        finally:
            writelog._append = saved
        assert isinstance(out, dict) and out["written"], out
    [intent, done] = _lines()
    assert [r["phase"] for r in seen[0]] == ["intent"]
    assert intent["phase"] == "intent" and done == {"v": 1, "seq": 1, "phase": "done",
                                                    "at": done["at"]}
    assert intent["client"] == "claude-code" and intent["op"] == "ingest"
    assert intent["path"] == "04-synthesis/d.md" and intent["scope"] == "demo"
    assert intent["sha256_before"] is None and intent["sha256_after"] == _sha(folder / "d.md")
    assert intent["grant"] == grants.load()["claude-code"].token_sha256[:12]
    assert "prev" not in intent


def test_a_crash_between_the_intent_and_the_write_did_not_land() -> None:
    """Between 1 and 2: the intent is there, and the note still holds what it held before. That
    is decided, so check closes it with `void` and does not fail."""
    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE, encoding="utf-8")
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_text(_intent(1, client="claude-code", vault=folder.parent,
                                           rel="04-synthesis/d.md", before=_sha(note),
                                           after="f" * 64))
        assert writelog.provenance(writelog.read(), str(note), _sha(note),
                                   None)["state"] == "none"
        code, report = _check(registry)
        assert code == 0 and [r["seq"] for r in report["did_not_land"]] == [1], report
        assert _lines()[-1]["phase"] == "void" and _lines()[-1]["reason"] == "did_not_land"
        code, report = _check(registry)
    assert code == 0 and report["did_not_land"] == [], "decided once, then history"


def test_a_write_refused_inside_is_void_and_never_needs_attention() -> None:
    """A refusal inside the logged section (the note changed in the last moment) wrote nothing,
    and the engine says so at once: no check has to decide it."""
    with _world() as (folder, registry, sock):
        (folder / "d.md").write_text(_NOTE, encoding="utf-8")
        who = writelog.Identity("claude-code", "abc", "demo", str(folder.parent))
        with pytest.raises(RuntimeError), writelog.recorded(
                who, op="amend", target=folder / "d.md", doc_id="d",
                before=_sha(folder / "d.md"), after="f" * 64):
            (folder / "d.md").write_text("the user saved meanwhile", encoding="utf-8")
            raise RuntimeError("changed since the plan")
        assert [r["phase"] for r in _lines()] == ["intent", "void"]
        assert _lines()[1]["reason"] == "refused"
        code, report = _check(registry)
    assert code == 0 and report["refused"] == 1 and report["needs_attention"] == [], report


def test_only_an_undecidable_unsettled_write_needs_attention() -> None:
    """The one exit-1 condition: no `done`, no `void`, and a note that holds neither the bytes it
    had before the write nor the bytes the write named."""
    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text("neither side", encoding="utf-8")
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_text(_intent(1, client="zed", vault=folder.parent,
                                           rel="04-synthesis/d.md", before="b" * 64,
                                           after="a" * 64))
        code, report = _check(registry)
    assert code == 1 and [r["seq"] for r in report["needs_attention"]] == [1], report
    assert [r["phase"] for r in _lines()] == ["intent"], "an undecided write is left as it is"


def test_a_crash_between_the_write_and_done_landed_and_check_settles_it() -> None:
    """Between 2 and 3: the note holds the intent's bytes, so it landed."""
    with _world() as (folder, registry, sock):
        real = writelog._append

        def no_done(fd, record, **kw):
            if record["phase"] == "done":
                raise writelog.WriteLogError("the disk filled")
            return real(fd, record, **kw)

        writelog._append = no_done
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                out = _ingest(registry, sock)
        finally:
            writelog._append = real
        assert isinstance(out, dict) and out["written"], out
        assert "was written, and its `done` record was not" in err.getvalue(), err.getvalue()
        assert [r["phase"] for r in _lines()] == ["intent"]
        note = folder / "d.md"
        assert writelog.provenance(writelog.read(), str(note), _sha(note),
                                   None)["state"] == "engine"
        code, report = _check(registry)
        assert code == 0 and [r["seq"] for r in report["settled"]] == [1], report
        assert [r["phase"] for r in _lines()] == ["intent", "done"]
        code, report = _check(registry)
    assert code == 0 and report["settled"] == [] and report["did_not_land"] == [], report


def test_a_finished_write_is_the_engines_until_the_note_is_edited() -> None:
    """After 3: both records; an edit by hand afterwards reads as edited, still for the client."""
    with _world() as (folder, registry, sock):
        assert isinstance(_ingest(registry, sock), dict)
        note = folder / "d.md"
        _index(registry, note)
        prov = _search_provenance(registry)
        assert prov == {"state": "engine", "client": "claude-code", "op": "ingest",
                        "at": prov["at"], "stamp": "matches", "stamped_by": "claude-code"}
        note.write_text(note.read_text(encoding="utf-8") + "\nedited by hand\n", encoding="utf-8")
        _index(registry, note)
        prov = _search_provenance(registry)
    assert prov["state"] == "edited" and prov["client"] == "claude-code", prov


def test_an_amend_records_the_bytes_it_replaced() -> None:
    with _world() as (folder, registry, sock):
        before = hashlib.sha256(_NOTE.encode()).hexdigest()
        plan, out = _amend(folder, registry, sock)
        assert isinstance(out, dict), out
    [intent, done] = _lines()
    assert intent["op"] == "amend" and intent["sha256_before"] == before
    assert intent["sha256_after"] == out["sha256"] and done["phase"] == "done"


def test_a_torn_last_line_is_ended_before_the_next_record() -> None:
    with _world() as (folder, registry, sock):
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_bytes(b'{"v":1,"seq":1,"phase":"inte')
        assert writelog.read().torn
        assert isinstance(_ingest(registry, sock), dict)
        log = writelog.read()
        assert not log.torn and [w.seq for w in log.writes] == [1] and log.writes[0].done
        assert log.damage == ["line 1: not a record"], log.damage
        code, report = _check(registry)
    assert code == 0 and report["damage"] == ["line 1: not a record"], report


@pytest.mark.parametrize(("seqs", "problem"), [
    ([1, 3], "seq 3 after 1, 1 missing"),
    ([1, 5], "seq 5 after 1, 3 missing"),
    ([1, 1], "seq 1 repeated"),
    ([2, 1], "seq 1 after 2, out of order"),
])
def test_a_gap_a_repeat_or_a_reordering_is_reported(seqs: list[int], problem: str) -> None:
    """Each is reported, and none fails: damage is history nothing may rewrite."""
    root, registry = _fixture(manifest=True)
    writelog.home().mkdir(parents=True, exist_ok=True)
    note = root / "demo-vault" / "n.md"
    note.write_text("x", encoding="utf-8")
    writelog.path().write_text("".join(
        _intent(s, client="zed", vault=root / "demo-vault", rel="n.md", after=_sha(note))
        + _done(s) for s in seqs))
    code, report = _check(registry)
    assert any(problem in p for p in report["damage"]), report
    assert code == 0, report


def test_damage_after_the_last_whole_record_is_reported_and_does_not_fail() -> None:
    root, registry = _fixture(manifest=True)
    writelog.home().mkdir(parents=True, exist_ok=True)
    writelog.path().write_bytes(b"not json\n{\"torn")
    code, report = _check(registry)
    assert code == 0, report
    assert report["damage"] == ["line 1: not a record", "the last line is unterminated"], report


def test_the_owners_own_write_is_not_logged() -> None:
    root, registry = _fixture(manifest=True)
    (root / "demo-vault" / "04-synthesis").mkdir()
    _, out = _plan_and_commit(_cfg(registry, "open"), "ingest", _ARGS)
    assert isinstance(out, dict) and out["written"], out
    assert not writelog.path().exists()


# --------------------------------------------------------------------------- §5 stamps

def _log_with(client: str, note: Path) -> writelog.Log:
    log = writelog.Log()
    rec = {"v": 1, "seq": 1, "phase": "intent", "at": "2026-11-11T00:00:00.000Z", "op": "ingest",
           "client": client, "grant": "abc", "scope": "demo", "vault": str(note.parent),
           "path": note.name, "doc_id": "d", "sha256_before": None, "sha256_after": _sha(note)}
    log.feed(json.dumps(rec).encode())
    log.feed(json.dumps({"v": 1, "seq": 1, "phase": "done", "at": rec["at"]}).encode())
    return log


def _note(text: str) -> Path:
    root, _ = _fixture()
    note = root / "demo-vault" / "d.md"
    note.write_text(text, encoding="utf-8")
    return note


def test_a_note_with_no_stamp_has_no_stamp_verdict() -> None:
    note = _note(_NOTE)
    prov = writelog.provenance(_log_with("zed", note), str(note), _sha(note),
                               writelog.stamp_of(str(note), _sha(note)))
    assert prov["state"] == "engine" and prov["stamp"] is None and prov["stamped_by"] is None


@pytest.mark.parametrize("key", ["written_by", "amended_by"])
def test_a_stamp_the_last_write_agrees_with_matches(key: str) -> None:
    note = _note(_NOTE.replace("status: active\n", f"status: active\n{key}: zed\n"))
    prov = writelog.provenance(_log_with("zed", note), str(note), _sha(note),
                               writelog.stamp_of(str(note), _sha(note)))
    assert prov["stamp"] == "matches" and prov["stamped_by"] == "zed", prov


def test_amended_by_is_the_claim_when_both_stamps_are_there() -> None:
    note = _note(_NOTE.replace("status: active\n",
                               "status: active\nwritten_by: zed\namended_by: claude-code\n"))
    assert writelog.stamp_of(str(note), _sha(note)) == "claude-code"


def test_a_stamp_naming_another_client_is_contradicted() -> None:
    note = _note(_NOTE.replace("status: active\n", "status: active\namended_by: alice\n"))
    prov = writelog.provenance(_log_with("zed", note), str(note), _sha(note),
                               writelog.stamp_of(str(note), _sha(note)))
    assert prov["stamp"] == "contradicted" and prov["stamped_by"] == "alice"
    assert prov["client"] == "zed", prov


def test_a_stamp_on_a_note_the_engine_never_wrote_is_unchecked() -> None:
    """A note a client wrote before the log existed carries `written_by` and has no log entry.
    The log has nothing to compare it with, which is not a contradiction."""
    note = _note(_NOTE.replace("status: active\n", "status: active\nwritten_by: zed\n"))
    prov = writelog.provenance(writelog.Log(), str(note), _sha(note),
                               writelog.stamp_of(str(note), _sha(note)))
    assert prov == {"state": "none", "client": None, "op": None, "at": None,
                    "stamp": "unchecked", "stamped_by": "zed"}


def test_a_stamp_is_unchecked_when_the_log_cannot_be_read() -> None:
    note = _note(_NOTE.replace("status: active\n", "status: active\nwritten_by: zed\n"))
    prov = writelog.provenance(None, str(note), _sha(note),
                               writelog.stamp_of(str(note), _sha(note)))
    assert prov["state"] == "unknown" and prov["stamp"] == "unchecked", prov


def test_check_lists_a_contradicted_stamp_and_exits_one() -> None:
    with _world() as (folder, registry, sock):
        assert isinstance(_ingest(registry, sock), dict)
        note = folder / "d.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "written_by: claude-code\n", "written_by: claude-code\namended_by: alice\n"),
            encoding="utf-8")
        _index(registry, note)
        code, report = _check(registry)
        assert code == 0, "a contradicted stamp is reported, and is not a write to settle"
        assert report["contradicted"] == [{"scopes": ["demo"], "path": str(note),
                                           "stamped_by": "alice",
                                           "last_write_client": "claude-code"}]
        assert _search_provenance(registry)["stamp"] == "contradicted"


def test_check_counts_a_stamp_from_before_the_log_and_does_not_fail() -> None:
    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE.replace("status: active\n", "status: active\nwritten_by: zed\n"),
                        encoding="utf-8")
        _index(registry, note)
        code, report = _check(registry)
        assert _search_provenance(registry)["stamp"] == "unchecked"
    assert code == 0 and report["contradicted"] == [], report
    assert report["stamps_without_an_engine_write"] == 1, report


def test_search_and_expand_carry_the_same_provenance() -> None:
    with _world() as (folder, registry, sock):
        assert isinstance(_ingest(registry, sock), dict)
        _index(registry, folder / "d.md")
        searched = _search_provenance(registry)
        err, out = _call(_cfg(registry), "expand", {"expand_ref": "demo/d#c00000"})
    assert not err, out
    assert out["passage"]["provenance"] == searched


def test_every_server_that_reads_the_log_names_it() -> None:
    """Every non-agent server reads the log for provenance, whatever its own writes mode."""
    root, registry = _fixture(manifest=True)
    for writes in ("granted", "none", "open"):
        err, out = _call(_cfg(registry, writes), "list_scopes", {})
        assert not err and out["server"]["writes_log"] == str(writelog.path()), (writes, out)


# --------------------------------------------------------------------------- the index lags a write

def test_an_amend_not_yet_indexed_leaves_the_indexed_bytes_with_their_writer() -> None:
    """The index keeps a note's old bytes until the next refresh pass. Those bytes are still the
    first client's engine write, not an edit by the client that amended it since."""
    with _world() as (folder, registry, sock):
        grants.issue("zed")
        assert isinstance(_plan_and_commit(_bridge(registry, sock, "zed"), "ingest", _ARGS)[1],
                          dict)
        note = folder / "d.md"
        _index(registry, note)
        grants.issue("claude-code")
        text = note.read_text(encoding="utf-8")
        plan, out = _plan_and_commit(_bridge(registry, sock), "amend",
                                     {"scope": "demo", "path": "04-synthesis/d.md",
                                      "content": text + "\nmore\n"})
        assert isinstance(out, dict), out
        prov = _search_provenance(registry)
    assert prov["state"] == "engine" and prov["client"] == "zed" and prov["op"] == "ingest", prov
    assert prov["stamp"] == "unchecked", "the stamp on disk is not the indexed note's"


def test_a_hand_written_note_amended_since_it_was_indexed_is_still_nobodys() -> None:
    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE, encoding="utf-8")
        _index(registry, note)
        plan, out = _amend(folder, registry, sock)
        assert isinstance(out, dict), out
        prov = _search_provenance(registry)
    assert prov["state"] == "none" and prov["client"] is None, prov


def test_a_note_reverted_by_hand_reads_as_edited_from_the_write_it_undid() -> None:
    note = _note(_NOTE)
    s0 = _sha(note)
    log = writelog.Log()
    for seq, (client, before, after) in enumerate([("zed", s0, "1" * 64),
                                                   ("claude-code", s0, "2" * 64)], 1):
        log.feed(json.dumps({"v": 1, "seq": seq, "phase": "intent", "at": "t", "op": "amend",
                             "client": client, "grant": "abc", "scope": "demo",
                             "vault": str(note.parent), "path": note.name, "doc_id": "d",
                             "sha256_before": before, "sha256_after": after}).encode())
        log.feed(json.dumps({"v": 1, "seq": seq, "phase": "done", "at": "t"}).encode())
    prov = writelog.provenance(log, str(note), s0, None)
    assert prov["state"] == "edited" and prov["client"] == "zed", prov


def test_a_stamp_is_read_only_from_the_indexed_bytes() -> None:
    note = _note(_NOTE.replace("status: active\n", "status: active\nwritten_by: zed\n"))
    indexed = _sha(note)
    note.write_text(note.read_text(encoding="utf-8").replace("zed", "alice"), encoding="utf-8")
    assert writelog.stamp_of(str(note), indexed) is writelog.UNREAD
    assert writelog.stamp_of(str(note), None) is writelog.UNREAD
    assert writelog.stamp_of(str(note), _sha(note)) == "alice"


# --------------------------------------------------------------------------- review round 2

def _intent(seq: int, *, client: str, vault: Path, rel: str, after: str,
            before: str | None = None, doc_id: str | None = "d", grant: object = "abc") -> str:
    return json.dumps({"v": 1, "seq": seq, "phase": "intent", "at": "2026-11-11T00:00:00.000Z",
                       "op": "amend" if before else "ingest", "client": client, "grant": grant,
                       "scope": "demo", "vault": str(vault), "path": rel, "doc_id": doc_id,
                       "sha256_before": before, "sha256_after": after}) + "\n"


def _done(seq: int) -> str:
    return json.dumps({"v": 1, "seq": seq, "phase": "done", "at": "t"}) + "\n"


def test_a_write_proven_by_the_next_one_is_settled_though_the_note_changed_since() -> None:
    """A `done` that failed, then the note amended and edited: the next write replaced exactly
    the bytes the first one named, so the first one landed."""
    with _world() as (folder, registry, sock):
        vault = folder.parent
        note = folder / "d.md"
        note.write_text("edited by hand since", encoding="utf-8")
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_text(
            _intent(1, client="zed", vault=vault, rel="04-synthesis/d.md", after="a" * 64)
            + _intent(2, client="claude-code", vault=vault, rel="04-synthesis/d.md",
                      before="a" * 64, after="b" * 64) + _done(2))
        code, report = _check(registry)
        assert [r["seq"] for r in report["settled"]] == [1] and report["did_not_land"] == []
        log = writelog.read()
    assert code == 0 and log.writes[0].done, report
    assert [w.seq for w in writelog._landed(log.writes, None)] == [1, 2]


def test_a_check_waits_for_a_write_in_flight() -> None:
    """Under the log's lock, a write between its intent and its `done` is not judged mid-way."""
    import threading

    with _world() as (folder, registry, sock):
        vault = folder.parent
        note = folder / "d.md"
        body = b"the bytes the write will land"
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_text(_intent(1, client="zed", vault=vault, rel="04-synthesis/d.md",
                                           after=hashlib.sha256(body).hexdigest()))
        out: dict = {}
        with writelog._locked():
            t = threading.Thread(target=lambda: out.update(r=_check(registry)))
            t.start()
            t.join(0.5)
            assert t.is_alive(), "check ran without the log's lock"
            note.write_bytes(body)
            with writelog.path().open("a") as fh:
                fh.write(_done(1))
        t.join(10)
    code, report = out["r"]
    assert code == 0 and report["did_not_land"] == [] and report["settled"] == [], report


def test_check_skips_a_scope_that_was_never_composed() -> None:
    root, registry = _fixture(manifest=True)
    from substrate import scopes

    (root / "p-vault").mkdir()
    scopes.record("pending", vault=root / "p-vault", db=root / "pending.db", index_root=root,
                  registry=registry)
    code, report = _check(registry)
    assert code == 0 and report["damage"] == [] and report["skipped_scopes"] == ["pending"], report
    assert not (root / "pending.db").exists()


def test_a_note_inherited_by_two_scopes_is_listed_once() -> None:
    from substrate import scopes

    with _world() as (folder, registry, sock):
        assert isinstance(_ingest(registry, sock), dict)
        note = folder / "d.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "written_by: claude-code\n", "written_by: claude-code\namended_by: alice\n"),
            encoding="utf-8")
        _index(registry, note)
        root = registry.parent
        (root / "other-vault").mkdir()
        scopes.record("other", vault=root / "other-vault", db=root / "demo.db", index_root=root,
                      registry=registry)
        code, report = _check(registry)
    assert code == 0 and len(report["contradicted"]) == 1, report
    assert report["contradicted"][0]["scopes"] == ["demo", "other"], report


def test_check_prints_a_note_s_text_without_its_terminal_escapes() -> None:
    with _world() as (folder, registry, sock):
        assert isinstance(_ingest(registry, sock), dict)
        note = folder / "d.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "written_by: claude-code\n", "written_by: claude-code\namended_by: \x1b[2Jx\n"),
            encoding="utf-8")
        _index(registry, note)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["writes", "check", "--registry", str(registry)])
    assert code == 0 and "contradicted" in out.getvalue()
    assert "\x1b" not in out.getvalue(), out.getvalue()


def test_a_note_found_only_by_its_doc_id_borrows_no_history() -> None:
    """Keyed by path only. A note at another path that declares a logged write's doc_id (a moved
    note, or one that copied the id) reads `none`, which is true at that path."""
    note = _note(_NOTE)
    log = writelog.Log()
    for line in (_intent(1, client="zed", vault=Path("/a/vault/that/moved"), rel="d.md",
                         after=_sha(note)), _done(1)):
        log.feed(line.strip().encode())
    prov = writelog.provenance(log, str(note), _sha(note), None)
    assert prov["state"] == "none" and prov["client"] is None, prov


def test_a_write_with_no_grant_is_refused_not_logged_unreadably() -> None:
    root, _ = _fixture()
    who = writelog.Identity("claude-code", "", "demo", str(root / "demo-vault"))
    ran = []
    with pytest.raises(writelog.WriteLogError, match="no client or grant"):
        with writelog.recorded(who, op="ingest", target=root / "demo-vault" / "n.md",
                               doc_id="n", before=None, after="a" * 64):
            ran.append(True)
    assert ran == [] and not writelog.path().exists()


def test_a_log_that_fails_to_read_degrades_search_to_unknown(monkeypatch) -> None:
    with _world() as (folder, registry, sock):
        assert isinstance(_ingest(registry, sock), dict)
        _index(registry, folder / "d.md")
        real = os.fstat
        log_inode = os.stat(writelog.path()).st_ino

        def failing(fd):
            st = real(fd)
            if st.st_ino == log_inode:
                raise OSError(5, "I/O error")
            return st

        monkeypatch.setattr(writelog.os, "fstat", failing)
        writelog.CACHE.__init__()
        prov = _search_provenance(registry)
    assert prov["state"] == "unknown", prov


def test_an_unreadable_log_tail_refuses_the_write_in_its_own_words(monkeypatch) -> None:
    with _world() as (folder, registry, sock):
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_text(_intent(1, client="zed", vault=folder.parent,
                                           rel="04-synthesis/x.md", after="a" * 64) + _done(1))
        real = os.pread

        def failing(fd, n, off):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(writelog.os, "pread", failing)
        out = _ingest(registry, sock)
        monkeypatch.setattr(writelog.os, "pread", real)
    assert isinstance(out, str) and "No note was written" in out, out
    assert not (folder / "d.md").exists()


def test_the_next_seq_follows_the_last_intent_not_a_tail_of_settled_dones() -> None:
    with _world() as (folder, registry, sock):
        vault = folder.parent
        writelog.home().mkdir(parents=True, exist_ok=True)
        lines = "".join(_intent(s, client="zed", vault=vault, rel=f"04-synthesis/n{s}.md",
                                after="a" * 64) for s in range(1, 6))
        lines += _done(1) * (70 * 1024 // len(_done(1)) + 1)
        writelog.path().write_text(lines)
        assert isinstance(_ingest(registry, sock), dict)
        log = writelog.read()
    assert [w.seq for w in log.writes][-1] == 6, [w.seq for w in log.writes]
    assert not any("repeated" in d for d in log.damage), log.damage


def test_a_log_rewritten_in_place_is_read_again() -> None:
    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE, encoding="utf-8")
        _index(registry, note)
        writelog.home().mkdir(parents=True, exist_ok=True)
        first = _intent(1, client="zed", vault=folder.parent, rel="04-synthesis/d.md",
                        after=_sha(note)) + _done(1)
        writelog.path().write_text(first)
        assert _search_provenance(registry)["client"] == "zed"
        with writelog.path().open("r+") as fh:
            fh.write(first.replace('"zed"', '"bob"'))
        prov = _search_provenance(registry)
    assert prov["client"] == "bob", prov


def test_a_refused_ingest_leaves_no_new_folder() -> None:
    with _world() as (folder, registry, sock):
        writelog.home().mkdir(parents=True, exist_ok=True)
        os.symlink(folder / "elsewhere.jsonl", writelog.path())
        grants.issue("claude-code")
        _, out = _plan_and_commit(_bridge(registry, sock), "ingest",
                                  {**_ARGS, "folder": "02-areas"})
        assert isinstance(out, str) and "No note was written" in out, out
        assert not (folder.parent / "02-areas").exists()


def test_a_path_the_os_cannot_use_is_damage_not_a_failed_search() -> None:
    with _world() as (folder, registry, sock):
        note = folder / "d.md"
        note.write_text(_NOTE, encoding="utf-8")
        _index(registry, note)
        writelog.home().mkdir(parents=True, exist_ok=True)
        writelog.path().write_text(_intent(1, client="zed", vault=Path("/x\0y"), rel="d.md",
                                           after="a" * 64))
        prov = _search_provenance(registry)
        log = writelog.read()
    assert prov["state"] == "none", prov
    assert log.damage == ["line 1: an intent missing a field, or naming an unusable path"]


def test_a_damaged_grant_field_does_not_drop_the_write() -> None:
    note = _note(_NOTE)
    log = writelog.Log()
    for line in (_intent(1, client="zed", vault=note.parent, rel="d.md", after=_sha(note),
                         grant=123), _done(1)):
        log.feed(line.strip().encode())
    assert log.damage == [] and [w.seq for w in log.writes] == [1]


def test_a_blank_amended_by_does_not_hide_written_by() -> None:
    note = _note(_NOTE.replace("status: active\n",
                               "status: active\nwritten_by: zed\namended_by:\n"))
    assert writelog.stamp_of(str(note), _sha(note)) == "zed"
    blank = _note(_NOTE.replace("status: active\n", "status: active\nwritten_by:\n"))
    assert writelog.stamp_of(str(blank), _sha(blank)) is None


def test_a_cr_only_note_longer_than_the_old_head_reads_its_stamp() -> None:
    head = "---\rwritten_by: zed\rstatus: active\rdoc_type: decision\rconfidence: proposed\r---\r"
    if len(head.encode()) % 2 == 0:
        head = head.replace("zed", "zedd") if False else head + "\r"
    note = _note("x")
    note.write_bytes((head + "é" * 40_000).encode())
    assert writelog.stamp_of(str(note), _sha(note)) == "zed"


def test_an_unchanged_note_is_not_read_again_for_its_stamp(monkeypatch) -> None:
    note = _note(_NOTE.replace("status: active\n", "status: active\nwritten_by: zed\n"))
    reads: list[str] = []
    real = writelog.read_note

    def counted(where, limit):
        reads.append(where)
        return real(where, limit)

    monkeypatch.setattr(writelog, "read_note", counted)
    for _ in range(3):
        assert writelog.stamp_of(str(note), _sha(note)) == "zed"
    assert len(reads) == 1, reads
    note.write_text(note.read_text(encoding="utf-8").replace("zed", "alice!"), encoding="utf-8")
    assert writelog.stamp_of(str(note), _sha(note)) == "alice!"
    assert len(reads) == 2, reads
