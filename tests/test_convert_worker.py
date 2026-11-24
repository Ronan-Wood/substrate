"""The conversion worker, from the engine's side: start it, bound it, distrust it, kill it.

Nothing here needs docling. The workers are small scripts that speak `convert/1` badly in one
specific way each, started through the same `Worker` the engine uses (`worker_client._command` is
the one seam, replaced per test, never an environment variable). The real worker is started where
the question is how it is launched, and it needs only the engine's own modules for that.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from substrate import cli
from substrate.extract import conversion_quarantine as q
from substrate.extract import convert, convert_frames as frames, docling_arm, worker_client

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = textwrap.dedent('''
    import json, os, sys, time
    def send(frame):
        sys.stdout.buffer.write((json.dumps(frame) + "\\n").encode()); sys.stdout.buffer.flush()
    def raw(data):
        sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
    def hello():
        send({"v": 1, "type": "hello", "docling": "9.9"})
    def request():
        line = sys.stdin.buffer.readline()
        return json.loads(line) if line else None
    def converted(req, markdown="harbour meadow culvert"):
        data = markdown.encode()
        send({"v": 1, "type": "converted", "id": req["id"], "bytes": len(data), "pages": 1,
              "status": "SUCCESS", "seconds": 0.1})
        raw(data)
    def items(req, records, count=None):
        payload = b"".join(
            (json.dumps({**{k: v for k, v in r.items() if k != "text"},
                         "text_bytes": len(r["text"].encode())}) + "\\n").encode() + r["text"].encode()
            for r in records)
        send({"v": 1, "type": "items", "id": req["id"],
              "count": len(records) if count is None else count, "bytes": len(payload)})
        raw(payload)
    def bye():
        r = request()
        sys.exit(0 if r is None or r.get("type") == "bye" else 9)
''')

OK = "hello(); converted(request()); bye()"


@pytest.fixture
def fast(monkeypatch):
    """Limits a test can wait out: seconds rather than minutes."""
    monkeypatch.setattr(worker_client, "HELLO_SECONDS", 3)
    monkeypatch.setattr(worker_client, "KILL_GRACE_SECONDS", 1)
    monkeypatch.setattr(worker_client, "convert_deadline", lambda size: 2)
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert"}))
    monkeypatch.setattr(convert, "docling_version", lambda: "9.9")


def _fake(monkeypatch, *scripts: str) -> list[list[str]]:
    """Each spawn runs the next script (the last repeats). Returns the list of spawned commands."""
    spawned: list[list[str]] = []

    def command(cpu_seconds, lifeline):
        script = scripts[min(len(spawned), len(scripts) - 1)]
        cmd = [sys.executable, "-c", PRELUDE + textwrap.dedent(script), str(ROOT), str(lifeline)]
        spawned.append(cmd)
        return cmd

    monkeypatch.setattr(worker_client, "_command", command)
    return spawned


def _no_spawn(monkeypatch) -> None:
    def refuse(_cpu, _lifeline):
        raise AssertionError("a worker was started")

    monkeypatch.setattr(worker_client, "_command", refuse)


def _alive(pgid: int) -> list[int]:
    """The live (not zombie) processes in group `pgid`."""
    if sys.platform.startswith("linux"):
        out = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                stat = Path(f"/proc/{entry}/stat").read_text()
            except OSError:
                continue
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                out.append(int(entry))
        return out
    ps = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                        capture_output=True, text=True, check=True).stdout
    return [int(p) for p, g, s in (line.split()[:3] for line in ps.splitlines() if line.strip())
            if int(g) == pgid and not s.startswith("Z")]


def _gone_within(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pgid):
            return True
        time.sleep(0.05)
    return not _alive(pgid)


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "review.adoc"
    src.write_text("harbour meadow culvert\n", "utf-8")
    return src


def _convert(src: Path, **kw) -> convert.Conversion:
    return convert.to_markdown(src, convert.spec_for(src), log=lambda *_: None, **kw)


# ---------------------------------------------------------------- frames

GOOD = {"v": 1, "type": "converted", "id": 1, "bytes": 1, "pages": None, "status": "SUCCESS",
        "seconds": 0.5}
ITEM = {"label": "text", "text": "t", "page": 1, "lang": None, "height": 1.5, "left": 2,
        "layer": "BODY"}


@pytest.mark.parametrize(("line", "why"), [
    (b"not json", "not a JSON line"),
    (b"\xff\xfe", "not a JSON line"),
    (b"[1, 2]", "a JSON object"),
    (json.dumps({**GOOD, "v": 2}).encode(), "protocol version"),
    (json.dumps({**GOOD, "v": True}).encode(), "protocol version"),
    (json.dumps({**GOOD, "type": "pickle"}).encode(), "unknown frame type"),
    (json.dumps({**GOOD, "extra": 1}).encode(), "1 unknown key"),
    (json.dumps({k: v for k, v in GOOD.items() if k != "pages"}).encode(), "missing \\['pages'\\]"),
    (json.dumps({**GOOD, "id": "1"}).encode(), "out of bounds"),
    (json.dumps({**GOOD, "id": True}).encode(), "out of bounds"),
    (json.dumps({**GOOD, "pages": -1}).encode(), "out of bounds"),
    (json.dumps({**GOOD, "bytes": frames.MAX_MARKDOWN + 1}).encode(), "out of bounds"),
    (json.dumps({"v": 1, "type": "items", "id": 1, "count": frames.MAX_ITEMS + 1, "bytes": 0})
     .encode(), "out of bounds"),
    (json.dumps({"v": 1, "type": "items", "id": 1, "count": 1,
                 "bytes": frames.MAX_ITEMS_BYTES + 1}).encode(), "out of bounds"),
    (b'{"v": 1, "type": "converted", "id": 1, "id": 2, "bytes": 0, "pages": null, '
     b'"status": "S", "seconds": 0}', "appears twice"),
    (b'{"v": 1, "type": "converted", "id": 1, "bytes": 0, "pages": null, "status": "S", '
     b'"seconds": NaN}', "not a number"),
    (b'{"v": 1, "type": "converted", "id": 1, "bytes": 0, "pages": null, "status": "S", '
     b'"seconds": Infinity}', "not a number"),
    (json.dumps({"v": 1, "type": "refused", "id": 1, "reason": "x" * (frames.MAX_SENTENCE + 1)})
     .encode(), "out of bounds"),
])
def test_a_frame_that_cannot_be_trusted_is_refused(line, why) -> None:
    with pytest.raises(frames.FrameError, match=why):
        frames.decode(line)


def test_a_line_over_the_bound_is_refused_both_ways() -> None:
    with pytest.raises(frames.FrameError, match="over the 65536-byte bound"):
        frames.encode({"v": 1, "type": "refused", "id": 1, "reason": "x" * frames.MAX_LINE})
    with pytest.raises(frames.FrameError, match="over the 65536-byte bound"):
        frames.decode(b"x" * frames.MAX_LINE)


def test_every_frame_type_round_trips() -> None:
    for frame in ({"v": 1, "type": "hello", "docling": None},
                  {"v": 1, "type": "convert", "id": 3, "format": "docx", "path": "/x"},
                  {"v": 1, "type": "extract_pdf", "id": 3, "path": "/x", "first": 1, "last": 9},
                  {"v": 1, "type": "refused", "id": 3, "reason": "r"},
                  {"v": 1, "type": "needs_extra", "id": 3, "error": "e"},
                  {"v": 1, "type": "unsupported", "id": 3, "error": "e"},
                  {"v": 1, "type": "bye"}):
        assert frames.decode(frames.encode(frame).rstrip(b"\n")) == frame
    data = frames.encode_converted(4, "harbour é", pages=2, status="SUCCESS", seconds=0.5)
    header, payload = data.split(b"\n", 1)
    assert frames.decode(header)["bytes"] == len(payload) and frames.text(payload) == "harbour é"
    data = frames.encode_items(5, [ITEM, {**ITEM, "text": "tide\nmeadow"}])
    header, payload = data.split(b"\n", 1)
    head = frames.decode(header)
    assert (head["count"], head["bytes"]) == (2, len(payload))
    assert frames.decode_items(2, payload) == [ITEM, {**ITEM, "text": "tide\nmeadow"}]


@pytest.mark.parametrize("shape", ["lists", "objects", "ints", "floats", "strings", "nesting",
                                   "digits", "escapes"])
def test_no_line_costs_more_than_its_measured_bound_to_parse(shape) -> None:
    """A line at the cap, in each shape that makes a JSON parser allocate most: refused, and the
    parse peaks under 4 MiB (measured 2.8 MiB at worst, for nesting, on 3.14.4 and 3.14.7)."""
    import tracemalloc

    cap = frames.MAX_LINE - 1
    unit, head, tail = {"lists": ("[],", '{"a":[', "[]]}"), "objects": ("{},", '{"a":[', "{}]}"),
                        "ints": ("1,", '{"a":[', "1]}"), "floats": ("1.5,", '{"a":[', "1.5]}"),
                        "strings": ('"\\u4e00",', '{"a":[', '"x"]}'),
                        "nesting": ("[", "", ""), "digits": ("9", '{"a":', "}"),
                        "escapes": ("\\u4e00", '{"a":"', '"}')}[shape]
    if shape == "nesting":
        line = ("[" * (cap // 2) + "]" * (cap // 2)).encode()
    else:
        line = (head + unit * ((cap - len(head) - len(tail)) // len(unit)) + tail).encode()
    assert len(line) < frames.MAX_LINE
    tracemalloc.start()
    try:
        with pytest.raises(frames.FrameError):
            frames.decode(line)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, (shape, peak)


@pytest.mark.parametrize(("payload", "count", "why"), [
    (b'{"label":"text","page":1,"lang":null,"height":null,"left":null,"layer":"B","text_bytes":2}'
     b"\nab", 2, "does not end"),
    (b'{"label":"text","page":1,"lang":null,"height":null,"left":null,"layer":"B","text_bytes":2}'
     b"\nabEXTRA", 1, "5 bytes after"),
    (b'{"label":"text","page":1,"lang":null,"height":null,"left":null,"layer":"B","text_bytes":9}'
     b"\nab", 1, "runs past"),
    (b'{"label":"text","page":1,"lang":null,"height":null,"left":null,"layer":"B","text_bytes":2}'
     b"\n\xff\xfe", 1, "not UTF-8"),
    (b'{"label":"text","page":1,"lang":null,"height":null,"left":null,"layer":"B","text_bytes":2,'
     b'"x":1}\nab', 1, "unknown key"),
    (b'{"label":"' + b"x" * frames.MAX_LINE + b'"}\nab', 1, "does not end"),
])
def test_item_records_that_disagree_with_their_header_are_refused(payload, count, why) -> None:
    with pytest.raises(frames.FrameError, match=why):
        frames.decode_items(count, payload)


# ---------------------------------------------------------------- limits

def test_a_conversion_has_a_base_more_per_mib_and_a_ceiling() -> None:
    assert worker_client.convert_deadline(0) == worker_client.CONVERT_BASE_SECONDS == 60
    assert worker_client.convert_deadline(2**20) == 60 + worker_client.CONVERT_SECONDS_PER_MIB == 70
    assert worker_client.convert_deadline(2**20 + 1) == 71
    assert worker_client.convert_deadline(2**40) == worker_client.CONVERT_CEILING_SECONDS == 900


def test_a_pdf_batch_has_a_base_more_per_page_and_a_ceiling() -> None:
    assert worker_client.pdf_batch_deadline(1) == worker_client.PDF_BATCH_BASE_SECONDS + 20 == 80
    assert worker_client.pdf_batch_deadline(10) == 60 + 10 * worker_client.PDF_BATCH_SECONDS_PER_PAGE
    assert worker_client.pdf_batch_deadline(100) == worker_client.PDF_BATCH_CEILING_SECONDS == 900


def test_the_hello_and_kill_limits() -> None:
    assert worker_client.HELLO_SECONDS == 120
    assert worker_client.MODEL_LOAD_SECONDS == 60
    assert worker_client.KILL_GRACE_SECONDS == 5
    assert worker_client.STDERR_RING == 64 * 1024


# ---------------------------------------------------------------- the decision

@pytest.mark.parametrize(("outcomes", "record", "retry", "expect"), [
    ((), q.ABSENT, False, ("attempt", "keep")),
    ((), q.OTHER, False, ("attempt", "keep")),
    ((), q.MATCHING, False, ("blocked", "keep")),
    ((), q.MATCHING, True, ("attempt", "keep")),
    (("ok",), q.ABSENT, False, ("converted", "keep")),
    (("ok",), q.MATCHING, True, ("converted", "delete")),
    (("ok",), q.OTHER, False, ("converted", "delete")),
    (("hang",), q.ABSENT, False, ("attempt", "keep")),
    (("crash", "ok"), q.ABSENT, False, ("converted", "keep")),
    (("hang", "crash"), q.ABSENT, False, ("quarantined", "write")),
    (("protocol", "protocol"), q.MATCHING, True, ("quarantined", "write")),
    (("refused",), q.ABSENT, False, ("refused", "keep")),
    (("hang", "refused"), q.ABSENT, False, ("refused", "keep")),
    (("needs_extra",), q.MATCHING, True, ("needs_extra", "keep")),
    (("unsupported",), q.ABSENT, False, ("unsupported", "keep")),
    (("changed",), q.MATCHING, True, ("changed", "keep")),
    (("hang", "changed"), q.ABSENT, False, ("changed", "keep")),
])
def test_the_quarantine_decision(outcomes, record, retry, expect) -> None:
    d = q.quarantine_decision(outcomes, record, retry)
    assert (d.verdict, d.record) == expect


@pytest.mark.parametrize("outcomes", [("ok", "hang"), ("refused", "ok"), ("hang", "hang", "hang"),
                                      ("bad",)])
def test_the_decision_refuses_a_sequence_no_driver_produces(outcomes) -> None:
    with pytest.raises(ValueError):
        q.quarantine_decision(outcomes, q.ABSENT, False)


# ---------------------------------------------------------------- the record

CHECKSUM = "a" * 64


def test_a_record_holds_exactly_its_keys_and_nothing_from_the_document(tmp_path) -> None:
    path = q.write(tmp_path / "store", CHECKSUM, fmt="docx", models=False, docling="9.9",
                   faults=["hang", "crash"])
    record = json.loads(path.read_text())
    assert set(record) == {"v", "source_sha256", "format", "models", "docling", "faults", "at"}
    assert record == {**record, "v": 1, "source_sha256": CHECKSUM, "format": "docx",
                      "models": False, "docling": "9.9", "faults": ["hang", "crash"]}
    assert path.stat().st_mode & 0o777 == 0o400 and path.parent.stat().st_mode & 0o777 == 0o700
    assert q.read(tmp_path / "store", CHECKSUM) == record
    assert [p.name for p in path.parent.iterdir()] == [f"{CHECKSUM}.json"]


def test_two_ingests_writing_one_record_leave_a_whole_one(tmp_path) -> None:
    store = tmp_path / "store"
    q.write(store, CHECKSUM, fmt="docx", models=False, docling="1", faults=["hang", "hang"])
    errors: list[BaseException] = []

    def write(i):
        try:
            for _ in range(20):
                q.write(store, CHECKSUM, fmt="docx", models=False, docling=str(i),
                        faults=["crash", "crash"])
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    record = q.read(store, CHECKSUM)
    assert record is not None and record["docling"] in {"0", "1", "2", "3"}
    assert [p.name for p in q.directory(store).iterdir()] == [f"{CHECKSUM}.json"]


def test_a_record_the_engine_did_not_write_blocks_nothing(tmp_path) -> None:
    store = tmp_path / "store"
    q.write(store, CHECKSUM, fmt="docx", models=False, docling="1", faults=["hang", "hang"])
    path = q.directory(store) / f"{CHECKSUM}.json"
    for text in ("not json", json.dumps({"v": 1}), json.dumps({"v": 1, "source_sha256": "b" * 64,
                 "format": "docx", "models": False, "docling": "1", "faults": ["hang"], "at": "x"})):
        path.chmod(0o600)
        path.write_text(text)
        assert q.read(store, CHECKSUM) is None
    assert q.listing(store) == []


def test_a_record_matches_only_its_own_converter() -> None:
    record = {"format": "docx", "models": False, "docling": "1"}
    assert q.state(None, fmt="docx", models=False, docling="1") == q.ABSENT
    assert q.state(record, fmt="docx", models=False, docling="1") == q.MATCHING
    assert q.state(record, fmt="docx", models=False, docling="2") == q.OTHER
    assert q.state(record, fmt="docx", models=True, docling="1") == q.OTHER


# ---------------------------------------------------------------- one worker, misbehaving

def _worker() -> worker_client.Worker:
    return worker_client.Worker(10)


def _ask(w: worker_client.Worker) -> dict:
    return w.request({"type": "convert", "format": "asciidoc", "path": "/x"}, 2,
                     ("converted", "refused"))


@pytest.mark.parametrize(("script", "kind", "said"), [
    ("hello(); request(); time.sleep(60)", "hang", "gave no answer in 2 s"),
    ("hello(); request(); sys.exit(4)", "crash", "exited with status 4"),
    ("hello(); request(); os.kill(os.getpid(), 9)", "crash", "was killed by SIGKILL"),
    ("hello(); request(); raw(b'{\"v\": 1, \"type\": \"conv'); os._exit(1)", "crash",
     "exited with status 1"),
    ("hello(); request(); raw(b'garbage\\n'); time.sleep(60)", "protocol", "could not trust"),
    ("hello(); r = request(); send({'v': 2, 'type': 'refused', 'id': r['id'], 'reason': 'x'})",
     "protocol", "protocol version"),
    ("hello(); r = request(); send({'v': 1, 'type': 'refused', 'id': r['id'] + 1, 'reason': 'x'})",
     "protocol", "for request 2"),
    ("hello(); r = request(); send({'v': 1, 'type': 'needs_extra', 'id': r['id'], 'error': 'x'})",
     "protocol", "with a needs_extra frame"),
])
def test_a_misbehaving_worker_is_a_named_fault_and_its_group_dies(monkeypatch, fast, script, kind,
                                                                  said) -> None:
    _fake(monkeypatch, script)
    w = _worker()
    with pytest.raises(worker_client.WorkerFault) as e:
        with w:
            w.start()
            _ask(w)
            w.finish()
    assert (e.value.kind, said in e.value.said) == (kind, True), e.value.said
    assert _gone_within(w.pgid, 3) and not w.tmp.exists()


def test_a_line_over_the_bound_is_a_protocol_fault(monkeypatch, fast) -> None:
    _fake(monkeypatch, "hello(); request(); raw(b'x' * 100000); time.sleep(60)")
    with pytest.raises(worker_client.WorkerFault, match="over 65536 bytes") as e, _worker() as w:
        w.start()
        _ask(w)
    assert e.value.kind == "protocol"


def test_a_good_answer_followed_by_garbage_is_not_used(monkeypatch, fast) -> None:
    _fake(monkeypatch, "hello(); converted(request()); raw(b'garbage\\n'); bye()")
    w = _worker()
    with pytest.raises(worker_client.WorkerFault, match="said more after its answer"), w:
        w.start()
        assert _ask(w)["type"] == "converted"
        w.finish()


def test_a_grandchild_that_ignores_sigterm_dies_with_the_group(monkeypatch, fast, tmp_path) -> None:
    pidfile = tmp_path / "child.pid"
    _fake(monkeypatch, f"""
        import subprocess
        child = subprocess.Popen([sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
        open({str(pidfile)!r}, "w").write(str(child.pid))
        hello(); request(); time.sleep(60)
    """)
    w = _worker()
    with pytest.raises(worker_client.WorkerFault), w:
        w.start()
        _ask(w)
    child = int(pidfile.read_text())
    assert _gone_within(w.pgid, 3), _alive(w.pgid)
    assert child not in _alive(w.pgid)


def test_a_worker_flooding_stderr_still_ends_at_its_deadline(monkeypatch, fast) -> None:
    _fake(monkeypatch, "hello(); request()\nwhile True: sys.stderr.write('x' * 65536)")
    t0 = time.monotonic()
    with pytest.raises(worker_client.WorkerFault, match="gave no answer in 2 s") as e, \
            _worker() as w:
        w.start()
        _ask(w)
    assert time.monotonic() - t0 < 10
    assert 0 < len(e.value.stderr) <= worker_client.STDERR_RING


def test_a_worker_that_does_not_read_cannot_block_the_engine(monkeypatch, fast) -> None:
    _fake(monkeypatch, "hello(); time.sleep(60)")
    t0 = time.monotonic()
    with pytest.raises(worker_client.WorkerFault, match="did not read its request in 2 s"), \
            _worker() as w:
        w.start()
        w._send(b"x" * 4_000_000, time.monotonic() + 2, 2)
    assert time.monotonic() - t0 < 10


def test_a_worker_that_will_not_exit_after_its_answer_is_a_fault(monkeypatch, fast) -> None:
    _fake(monkeypatch, "hello(); converted(request()); request(); time.sleep(60)")
    with pytest.raises(worker_client.WorkerFault, match="did not exit within 1 s"), _worker() as w:
        w.start()
        _ask(w)
        w.finish()


# ---------------------------------------------------------------- retry and quarantine

def test_a_fault_is_retried_once_in_a_new_worker(monkeypatch, fast, tmp_path) -> None:
    spawned = _fake(monkeypatch, "hello(); request(); sys.exit(1)", OK)
    said: list[str] = []
    conv = convert.to_markdown(_source(tmp_path), convert.spec_for(_source(tmp_path)),
                               log=said.append, quarantine=lambda: tmp_path / "store")
    assert "harbour" in conv.markdown and len(spawned) == 2
    assert any("WARNING (converter)" in s and "exited with status 1" in s and "once more" in s
               for s in said), said
    assert q.listing(tmp_path / "store") == []


def test_two_faults_quarantine_the_file_naming_each_limit(monkeypatch, fast, tmp_path) -> None:
    spawned = _fake(monkeypatch, "hello(); request(); time.sleep(60)",
                    "hello(); request(); sys.exit(3)")
    src, store = _source(tmp_path), tmp_path / "store"
    with pytest.raises(convert.Quarantined) as e:
        _convert(src, quarantine=lambda: store)
    assert len(spawned) == 2
    assert "gave no answer in 2 s" in str(e.value) and "exited with status 3" in str(e.value)
    assert "--retry-quarantined" in str(e.value)
    (row,) = q.listing(store)
    assert (row["format"], row["fault"]) == ("asciidoc", "crash")
    from substrate.extract.base import sha256_file

    assert row["checksum"] == sha256_file(src)
    record = q.read(store, row["checksum"])
    assert record["faults"] == ["hang", "crash"] and record["docling"] == "9.9"


def test_a_quarantined_file_is_refused_before_any_worker_starts(monkeypatch, fast, tmp_path) -> None:
    from substrate.extract.base import sha256_file

    src, store = _source(tmp_path), tmp_path / "store"
    q.write(store, sha256_file(src), fmt="asciidoc", models=False, docling="9.9",
            faults=["hang", "crash"])
    _no_spawn(monkeypatch)
    with pytest.raises(convert.Quarantined, match="hung, then crashed") as e:
        _convert(src, quarantine=lambda: store)
    assert "--retry-quarantined" in str(e.value)


def test_retry_quarantined_converts_and_clears_the_record(monkeypatch, fast, tmp_path) -> None:
    from substrate.extract.base import sha256_file

    src, store = _source(tmp_path), tmp_path / "store"
    q.write(store, sha256_file(src), fmt="asciidoc", models=False, docling="9.9",
            faults=["hang", "crash"])
    _fake(monkeypatch, OK)
    assert "harbour" in _convert(src, quarantine=lambda: store, retry_quarantined=True).markdown
    assert q.read(store, sha256_file(src)) is None


def test_another_converters_record_does_not_block(monkeypatch, fast, tmp_path) -> None:
    from substrate.extract.base import sha256_file

    src, store = _source(tmp_path), tmp_path / "store"
    q.write(store, sha256_file(src), fmt="asciidoc", models=False, docling="1.0",
            faults=["hang", "crash"])
    _fake(monkeypatch, OK)
    assert "harbour" in _convert(src, quarantine=lambda: store).markdown


@pytest.mark.parametrize(("reply", "raises"), [
    ("send({'v': 1, 'type': 'refused', 'id': r['id'], 'reason': 'docling said no'})",
     convert.ConversionRefused),
    ("send({'v': 1, 'type': 'needs_extra', 'id': r['id'], 'error': 'No module named ocrmac'})",
     convert.ConverterMissing),
    ("send({'v': 1, 'type': 'unsupported', 'id': r['id'], 'error': 'not a valid InputFormat'})",
     convert.ConverterUnsupported),
])
def test_docling_s_own_answer_is_never_retried(monkeypatch, fast, tmp_path, reply, raises) -> None:
    spawned = _fake(monkeypatch, f"hello(); r = request(); {reply}; bye()")
    with pytest.raises(raises):
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_without_a_store_the_file_still_refuses_after_two_faults(monkeypatch, fast, tmp_path) -> None:
    _fake(monkeypatch, "hello(); request(); sys.exit(2)")

    def no_store():
        from substrate import blobs

        raise blobs.StoreConfigError("the store is inside a sync folder.")

    with pytest.raises(convert.Quarantined, match="could not be recorded \\(the store is inside"):
        _convert(_source(tmp_path), quarantine=no_store)


# ---------------------------------------------------------------- the bytes that were hashed

def test_a_file_rewritten_during_its_conversion_is_refused_and_not_quarantined(monkeypatch, fast,
                                                                               tmp_path) -> None:
    spawned = _fake(monkeypatch, """
        hello(); r = request()
        with open(r["path"], "a") as f:
            f.write("tide\\n")
        converted(r); bye()
    """)
    with pytest.raises(convert.FileChanged, match="while it was being ingested"):
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_ingest_records_the_checksum_of_the_bytes_it_converted(monkeypatch, fast, tmp_path) -> None:
    from substrate.extract.base import sha256_file

    src = _source(tmp_path)
    _fake(monkeypatch, OK)
    assert _convert(src).source_sha256 == sha256_file(src)


# ---------------------------------------------------------------- before any worker

def test_a_format_whose_extra_is_missing_is_refused_before_any_spawn(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset())
    _no_spawn(monkeypatch)
    err = _run_cli(["ingest", str(_source(tmp_path)), "--out", str(tmp_path / "out")])
    assert err[0] == 2 and "[convert] extra" in err[1] and "Traceback" not in err[1], err


def test_markdown_text_and_a_text_layer_pdf_never_start_a_worker(monkeypatch, tmp_path) -> None:
    pdfium = pytest.importorskip("pypdfium2")
    _no_spawn(monkeypatch)
    md = tmp_path / "note.md"
    md.write_text("# Harbour\n\nThe meadow drains toward the harbour.\n", "utf-8")
    txt = tmp_path / "note.txt"
    txt.write_text("The meadow drains toward the harbour.\n", "utf-8")
    pdf = tmp_path / "blank.pdf"
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 200)
    doc.save(str(pdf))
    for args in (["ingest", str(md)], ["ingest", str(txt)],
                 ["ingest", str(pdf), "--doc-class", "reference-frozen"]):
        rc, err = _run_cli([*args, "--out", str(tmp_path / f"out-{Path(args[1]).suffix[1:]}")])
        assert "a worker was started" not in err and "Traceback" not in err, (args, rc, err)


def _run_cli(argv: list[str]) -> tuple[int, str]:
    import io

    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except AssertionError as e:
            return -1, str(e)
    return rc, err.getvalue()


# ---------------------------------------------------------------- how the worker is launched

def test_the_worker_is_started_with_the_bundle_s_flags_and_off_its_cwd() -> None:
    cmd = worker_client._command(100, 7)
    assert cmd[0] == sys.executable and cmd[1:6] == ["-B", "-E", "-s", "-P", "-c"]
    assert cmd[6] == worker_client.BOOT and cmd[7] == str(ROOT) and cmd[8:] == ["100", "7"]


def test_the_worker_environment_is_built_not_inherited(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SUBSTRATE_SECRET", "x")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    env = worker_client.environment(tmp_path, {"DOCLING_ARTIFACTS_PATH": "/models"})
    assert env["HOME"] == env["TMPDIR"] == str(tmp_path) and env["HF_HUB_OFFLINE"] == "1"
    assert env["LANG"] == "en_GB.UTF-8" and env["DOCLING_ARTIFACTS_PATH"] == "/models"
    assert not {"SUBSTRATE_SECRET", "PYTHONPATH"} & set(env)
    for key in ("XDG_CACHE_HOME", "TORCH_HOME", "HF_HOME"):
        assert env[key].startswith(str(tmp_path)), key


def test_a_module_planted_in_the_worker_s_folder_is_not_imported(monkeypatch, tmp_path) -> None:
    planted = tmp_path / "planted"
    planted.mkdir()
    marker = tmp_path / "imported"
    for name in ("json", "docling", "substrate"):
        (planted / f"{name}.py").write_text(f"open({str(marker)!r}, 'a').write({name!r})\n")
    monkeypatch.setattr(worker_client.tempfile, "mkdtemp", lambda prefix="": str(planted))
    src = _source(tmp_path)
    with worker_client.Worker(60) as w:
        assert w.start()["type"] == "hello"
        reply = w.request({"type": "convert", "format": "asciidoc", "path": str(src)}, 60,
                          ("converted", "needs_extra"))
        w.finish()
    assert reply["type"] in ("converted", "needs_extra") and not marker.exists()


def test_the_worker_runs_from_a_package_in_a_site_directory(tmp_path) -> None:
    """A wheel puts `substrate` in site-packages; the worker must find it there, with `-s` in force
    and the working directory elsewhere. The copy marks that it, not the clone, ran."""
    site = tmp_path / "site"
    shutil.copytree(ROOT / "substrate", site / "substrate",
                    ignore=shutil.ignore_patterns("__pycache__"))
    marker = tmp_path / "ran-from-the-copy"
    worker_py = site / "substrate" / "extract" / "worker.py"
    worker_py.write_text(worker_py.read_text() + f"\nopen({str(marker)!r}, 'w').write('yes')\n")
    probe = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(site)!r})
        from pathlib import Path
        from substrate.extract import worker_client as wc
        assert wc.package_root() == Path({str(site)!r}).resolve(), wc.package_root()
        with wc.Worker(10) as w:
            print(w.start()["type"]); w.finish()
    """)
    out = subprocess.run([sys.executable, "-s", "-c", probe], cwd=tmp_path, capture_output=True,
                         text=True, timeout=120, env={"PATH": os.environ.get("PATH", ""),
                                                      "HOME": str(tmp_path)})
    assert out.returncode == 0 and out.stdout.strip() == "hello", out.stderr
    assert marker.read_text() == "yes"


# ---------------------------------------------------------------- stopping the engine

DRIVER = textwrap.dedent('''
    import os, signal, sys
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.path.insert(0, {root!r})
    from substrate import cli
    from substrate.extract import convert, worker_client
    worker = {worker!r}
    worker_client._command = lambda cpu, lifeline: [sys.executable, "-c", worker, {root!r},
                                                    str(lifeline)]
    convert.installed_extras = lambda: frozenset({{"convert"}})
    convert.docling_version = lambda: "9.9"
    sys.exit(cli.main(["ingest", {src!r}, "--out", {out!r}]))
''')


def _hanging_worker(pidfile: Path, *, watchdog: bool) -> str:
    guard = ("sys.path.insert(0, sys.argv[1]); from substrate.extract import worker; "
             "worker._watch_parent(int(sys.argv[2]))") if watchdog else "pass"
    return PRELUDE + textwrap.dedent(f"""
        {guard}
        hello(); request()
        open({str(pidfile)!r} + ".tmp", "w").write(str(os.getpgrp()))
        os.rename({str(pidfile)!r} + ".tmp", {str(pidfile)!r})
        time.sleep(600)
    """)


def _engine(tmp_path: Path, *, watchdog: bool = False) -> tuple[subprocess.Popen, int]:
    pidfile = tmp_path / "worker.pgid"
    code = DRIVER.format(root=str(ROOT), worker=_hanging_worker(pidfile, watchdog=watchdog),
                         src=str(_source(tmp_path)), out=str(tmp_path / "out"))
    engine = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    deadline = time.monotonic() + 60
    while not pidfile.exists():
        if time.monotonic() > deadline or engine.poll() is not None:
            engine.kill()
            raise AssertionError(f"the worker never started: {engine.communicate()}")
        time.sleep(0.05)
    return engine, int(pidfile.read_text())


def test_scripta_s_stop_leaves_no_worker_alive(tmp_path) -> None:
    """Scripta stops an ingest with SIGTERM to its process group and, shortly after, SIGKILL. The
    worker is in a session of its own, so neither signal reaches it: the engine kills it."""
    engine, worker_group = _engine(tmp_path)
    os.killpg(engine.pid, signal.SIGTERM)
    time.sleep(0.3)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(engine.pid, signal.SIGKILL)
    engine.communicate(timeout=30)
    assert _gone_within(worker_group, 3), _alive(worker_group)


def test_sigterm_and_sighup_stop_the_ingest_as_ctrl_c_does(tmp_path) -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        run = tmp_path / sig.name
        run.mkdir()
        engine, worker_group = _engine(run)
        os.kill(engine.pid, sig)
        _out, err = engine.communicate(timeout=30)
        assert engine.returncode == 128 + sig, (sig.name, engine.returncode, err)
        assert f"FATAL (interrupted): {sig.name}" in err and "Traceback" not in err, err
        assert _gone_within(worker_group, 3), (sig.name, _alive(worker_group))


def test_an_engine_killed_outright_takes_its_worker_with_it(tmp_path) -> None:
    """SIGKILL gives the engine no chance to clean up: the worker's own watchdog does it."""
    engine, worker_group = _engine(tmp_path, watchdog=True)
    os.kill(engine.pid, signal.SIGKILL)
    engine.communicate(timeout=30)
    assert _gone_within(worker_group, 3), _alive(worker_group)


# ---------------------------------------------------------------- the PDF read with models

def _pdf(path: Path, pages: int) -> Path:
    pdfium = pytest.importorskip("pypdfium2")
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 200)
    doc.save(str(path))
    return path


def test_a_models_pdf_is_read_in_batches_by_the_worker_and_assembled_here(monkeypatch, fast,
                                                                          tmp_path) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    seen = tmp_path / "seen.jsonl"
    _fake(monkeypatch, f"""
        hello()
        while True:
            r = request()
            if r is None or r["type"] == "bye":
                sys.exit(0)
            open({str(seen)!r}, "a").write(json.dumps({{"first": r["first"], "last": r["last"],
                "models": os.environ.get("DOCLING_ARTIFACTS_PATH")}}) + "\\n")
            page = r["first"]
            items(r, [
                {{"label": "section_header", "text": f"Harbour {{page}}", "page": page, "lang": None,
                  "height": 20.0, "left": 10.0, "layer": "BODY"}},
                {{"label": "page_header", "text": "Running head", "page": page, "lang": None,
                  "height": 8.0, "left": 10.0, "layer": "FURNITURE"}},
                {{"label": "text", "text": "The meadow drains toward the harbour.", "page": page,
                  "lang": None, "height": 10.0, "left": 10.0, "layer": "BODY"}}])
    """)
    pdf = _pdf(tmp_path / "survey.pdf", 2)
    models = tmp_path / "models"
    doc = docling_arm.DoclingExtractor(models, batch_pages=1).extract(
        pdf, "reference-frozen", log=lambda *_: None)
    batches = [json.loads(line) for line in seen.read_text().splitlines()]
    assert batches == [{"first": 1, "last": 1, "models": str(models)},
                       {"first": 2, "last": 2, "models": str(models)}]
    assert doc.extractor == "docling 9.9" and doc.extractor_arm == "docling"
    assert [b.text for b in doc.blocks if b.furniture_claimed] == ["Running head"] * 2
    assert "The meadow drains toward the harbour." in [b.text for b in doc.body_blocks]


def test_the_worker_turns_docling_s_items_into_frames(monkeypatch, tmp_path) -> None:
    from substrate.extract import worker

    class Item:
        def __init__(self, label, text, layer):
            self.label, self.text, self.content_layer = label, text, layer
            self.prov = [type("P", (), {"page_no": 3, "bbox": type("B", (), {
                "t": 30.0, "b": 10.0, "l": 5.25})()})()]

    class Document:
        def iterate_items(self, **_kw):
            yield Item("DocItemLabel.SECTION_HEADER", "Harbour", "ContentLayer.BODY"), 1
            yield Item("text", "  ", "BODY"), 1

    class Converter:
        def convert(self, path, page_range):
            assert page_range == (3, 4)
            return type("R", (), {"document": Document()})()

    monkeypatch.setattr(worker, "_pdf_converter", None)
    monkeypatch.setattr(worker, "_converter", lambda models: Converter())
    monkeypatch.setenv("DOCLING_ARTIFACTS_PATH", str(tmp_path))
    reply = worker.extract_pdf({"id": 7, "path": "/x.pdf", "first": 3, "last": 4})
    header, payload = worker.wire(reply).split(b"\n", 1)
    assert frames.decode_items(frames.decode(header)["count"], payload) == reply["items"]
    assert reply["items"] == [{"label": "section_header", "text": "Harbour", "page": 3,
                               "lang": None, "height": 20.0, "left": 5.2, "layer": "CONTENTLAYER.BODY"}]


# ---------------------------------------------------------------- what the operator sees

def test_blobs_status_lists_the_quarantine(monkeypatch, tmp_path) -> None:
    from substrate import blobs

    q.write(blobs.root(), CHECKSUM, fmt="docx", models=False, docling="9.9", faults=["hang", "crash"])
    rc, out = _stdout(["blobs", "status", "--json", "--registry", str(tmp_path / "scopes.toml")])
    assert rc == 0
    (row,) = json.loads(out)["quarantine"]
    assert set(row) == {"checksum", "format", "fault", "at"}
    assert (row["checksum"], row["format"], row["fault"]) == (CHECKSUM, "docx", "crash")
    rc, out = _stdout(["blobs", "status", "--registry", str(tmp_path / "scopes.toml")])
    assert "conversion quarantine: 1 file(s)" in out and f"{CHECKSUM[:12]}…  docx  crash" in out


def _stdout(argv: list[str]) -> tuple[int, str]:
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    return rc, out.getvalue()


def test_ingest_exits_3_for_a_quarantined_file(monkeypatch, fast, tmp_path) -> None:
    from substrate import blobs
    from substrate.extract.base import sha256_file

    src = _source(tmp_path)
    q.write(blobs.root(), sha256_file(src), fmt="asciidoc", models=False, docling="9.9",
            faults=["protocol", "protocol"])
    _no_spawn(monkeypatch)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert rc == 3 and "FATAL (quarantined)" in err and "answered with frames it could not trust" in err
    _fake(monkeypatch, OK)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out"), "--retry-quarantined"])
    assert rc == 0, err
    assert q.read(blobs.root(), sha256_file(src)) is None


# ---------------------------------------------------------------- what the review found

def test_a_worker_that_fails_before_hello_is_the_engine_s_problem_not_the_file_s(monkeypatch, fast,
                                                                                tmp_path) -> None:
    """The document is sent after `hello`: a worker that dies or hangs first says nothing about it,
    so it is exit 2, never a retry, never a record. An engine bug in the worker's imports would
    otherwise quarantine every file ingested."""
    for script in ("import nosuchmodule_engine", "time.sleep(60)",
                   "send({'v': 1, 'type': 'bye'}); time.sleep(60)"):
        spawned = _fake(monkeypatch, script)
        with pytest.raises(worker_client.WorkerUnavailable, match="before it was given the file|"
                                                                  "not hello"):
            _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
        assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_a_file_changed_while_the_worker_failed_on_it_is_not_quarantined(monkeypatch, fast,
                                                                        tmp_path) -> None:
    spawned = _fake(monkeypatch, """
        hello(); r = request()
        with open(r["path"], "a") as f:
            f.write("tide\\n")
        sys.exit(1)
    """)
    with pytest.raises(convert.FileChanged):
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_a_rewrite_that_keeps_the_size_and_the_mtime_is_still_seen(monkeypatch, fast,
                                                                   tmp_path) -> None:
    _fake(monkeypatch, """
        hello(); r = request()
        st = os.stat(r["path"])
        data = open(r["path"], "rb").read()
        open(r["path"], "r+b").write(data.upper())
        os.utime(r["path"], ns=(st.st_atime_ns, st.st_mtime_ns))
        converted(r); bye()
    """)
    with pytest.raises(convert.FileChanged):
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")


def test_the_unchanged_check_hashes_the_file_again(tmp_path) -> None:
    src = _source(tmp_path)
    from substrate.extract.base import sha256_file

    checksum = sha256_file(src)
    assert convert.unchanged(src, checksum)
    assert not convert.unchanged(src, "0" * 64)
    src.unlink()
    assert not convert.unchanged(src, checksum)


def test_a_worker_given_its_full_grace_to_exit_keeps_its_answer(monkeypatch, fast, tmp_path) -> None:
    monkeypatch.setattr(worker_client, "KILL_GRACE_SECONDS", 4)
    _fake(monkeypatch, "hello(); converted(request()); request(); os.close(1); time.sleep(1.5)")
    assert "harbour" in _convert(_source(tmp_path)).markdown


def test_a_stop_during_a_faulted_worker_s_grace_still_kills_its_group(monkeypatch, fast) -> None:
    _fake(monkeypatch, "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                       "hello(); request(); time.sleep(60)")
    real = worker_client.Worker._killpg

    def killpg(self, sig):
        real(self, sig)
        if sig == signal.SIGTERM:  # the grace period has begun: a second stop arrives
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(worker_client.Worker, "_killpg", killpg)
    w = _worker()
    with pytest.raises(KeyboardInterrupt):
        with w:
            w.start()
            _ask(w)
    assert _gone_within(w.pgid, 3), _alive(w.pgid)


def test_the_watchdog_kills_the_group_when_the_engine_lets_go(monkeypatch, fast) -> None:
    """The lifeline's end of file is the engine's exit, whatever reuses its pid afterwards."""
    _fake(monkeypatch, "sys.path.insert(0, sys.argv[1]); from substrate.extract import worker; "
                       "worker._watch_parent(int(sys.argv[2]))\nhello(); time.sleep(60)")
    w = _worker()
    try:
        w.start()
        os.close(w._lifeline)
        assert _gone_within(w.pgid, 3), _alive(w.pgid)
    finally:
        w._lifeline = os.open(os.devnull, os.O_RDONLY)
        w.close(gently=False)


def test_the_engine_does_not_close_stderr_under_its_reader(monkeypatch, fast, tmp_path) -> None:
    """A descendant that escapes the group can hold the stderr pipe open; its descriptor must stay
    the reader's until the reader sees its end, or a later pipe could take its number."""
    pidfile = tmp_path / "escaped.pid"
    _fake(monkeypatch, f"""
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"],
                                 start_new_session=True)
        open({str(pidfile)!r}, "w").write(str(child.pid))
        hello(); request(); sys.exit(1)
    """)
    w = _worker()
    with pytest.raises(worker_client.WorkerFault), w:
        w.start()
        _ask(w)
    try:
        assert w._drain.is_alive() and not w.proc.stderr.closed
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
    w._drain.join(timeout=5)
    assert not w._drain.is_alive() and w.proc.stderr.closed


def test_a_frame_error_quotes_the_worker_briefly() -> None:
    with pytest.raises(frames.FrameError) as e:
        frames.decode(json.dumps({"v": 1, "type": "x" * 100_000}).encode())
    assert len(str(e.value)) < 200
    with pytest.raises(frames.FrameError) as e:
        frames.decode(json.dumps({**GOOD, **{f"k{i}": 0 for i in range(5_000)}}).encode())
    assert len(str(e.value)) < 300


def test_a_frame_with_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(frames.FrameError):
        frames.decode(b'{"v": 1, "type": "refused", "id": 1, "reason": "\\ud800"}')


def test_a_document_of_too_many_items_is_refused(monkeypatch, fast, tmp_path) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    monkeypatch.setattr(docling_arm, "MAX_DOCUMENT_ITEMS", 2)
    spawned = _fake(monkeypatch, """
        hello()
        while True:
            r = request()
            if r is None or r["type"] == "bye":
                sys.exit(0)
            items(r, [{"label": "text", "text": "t", "page": 1, "lang": None, "height": None,
                       "left": None, "layer": "BODY"}] * 2)
    """)
    with pytest.raises(convert.ConversionRefused, match="more than 2 items"):
        docling_arm.DoclingExtractor(tmp_path, batch_pages=1).extract(
            _pdf(tmp_path / "long.pdf", 3), "reference-frozen", log=lambda *_: None)
    assert len(spawned) == 1


def test_a_worker_s_words_reach_the_terminal_without_their_escapes(monkeypatch, fast,
                                                                  tmp_path) -> None:
    _fake(monkeypatch, "hello(); r = request(); sys.stderr.write('\\x1b]0;owned\\x07 died\\n'); "
                       "send({'v': 1, 'type': 'refused', 'id': r['id'], "
                       "'reason': 'bad \\x1b]52;c;ZXZpbA==\\x07 member \\x9b2J'}); bye()")
    with pytest.raises(convert.ConversionRefused) as e:
        _convert(_source(tmp_path))
    assert "\x1b" not in str(e.value) and "\x9b" not in str(e.value) and "member" in str(e.value)
    _fake(monkeypatch, "hello(); request(); sys.stderr.write('\\x1b]0;owned\\x07 died'); "
                       "sys.stderr.flush(); sys.exit(1)")
    with pytest.raises(convert.Quarantined) as e:
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert "\x1b" not in str(e.value) and "died" in str(e.value)


def test_a_worker_running_another_docling_is_refused_before_it_gets_the_file(monkeypatch, fast,
                                                                            tmp_path) -> None:
    spawned = _fake(monkeypatch, "send({'v': 1, 'type': 'hello', 'docling': None}); "
                                 "converted(request()); bye()")
    with pytest.raises(worker_client.WorkerUnavailable, match="not the engine's 9.9"):
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_the_record_follows_the_decision_s_record_action(monkeypatch, fast, tmp_path) -> None:
    """The planned model checks `Decision.record`; the driver must act on it, not on the verdict."""
    real = q.quarantine_decision

    def decide(outcomes, record, retry):
        d = real(outcomes, record, retry)
        return q.Decision(d.verdict, "keep") if d.verdict == "quarantined" else d

    monkeypatch.setattr(q, "quarantine_decision", decide)
    _fake(monkeypatch, "hello(); request(); sys.exit(1)")
    with pytest.raises(convert.Quarantined):
        _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert q.listing(tmp_path / "store") == []


def test_the_worker_answers_an_unreturnable_conversion_with_a_refusal() -> None:
    from substrate.extract import worker

    good = {"v": 1, "type": "converted", "id": 1, "markdown": "x", "pages": None,
            "status": "SUCCESS", "seconds": 0.5}
    for frame in ({**good, "markdown": "\ud800"}, {**good, "seconds": float("nan")},
                  {**good, "markdown": "x" * (frames.MAX_MARKDOWN + 1)},
                  {"v": 1, "type": "items", "id": 1, "items": [{**ITEM, "height": 1e12}]},
                  {"v": 1, "type": "items", "id": 1, "items": [{**ITEM, "text": "\udc80"}]}):
        reply = frames.decode(worker.wire(frame).rstrip(b"\n"))
        assert reply["type"] == "refused" and reply["id"] == 1, reply


def test_docling_failing_after_it_read_the_pages_is_a_refusal(monkeypatch, tmp_path) -> None:
    from substrate.extract import worker

    class Converter:
        def convert(self, path, page_range):
            document = type("D", (), {"iterate_items": lambda self, **_kw: iter(
                [(type("I", (), {"label": "text", "text": "t", "prov": object()})(), 1)])})()
            return type("R", (), {"document": document})()

    monkeypatch.setattr(worker, "_pdf_converter", None)
    monkeypatch.setattr(worker, "_converter", lambda models: Converter())
    monkeypatch.setenv("DOCLING_ARTIFACTS_PATH", str(tmp_path))
    reply = worker.extract_pdf({"id": 2, "path": "/x.pdf", "first": 1, "last": 1})
    assert reply["type"] == "refused" and "could not walk pages 1-1" in reply["reason"], reply


def test_the_stop_handlers_cover_the_conversion_and_not_the_writes(monkeypatch, fast,
                                                                   tmp_path) -> None:
    from substrate.markdown import ingest

    seen = []
    real = ingest.ingest_markdown

    def recording(*a, **kw):
        seen.append(signal.getsignal(signal.SIGTERM))
        return real(*a, **kw)

    monkeypatch.setattr(ingest, "ingest_markdown", recording)
    _fake(monkeypatch, OK)
    before = signal.getsignal(signal.SIGTERM)
    _run_cli(["ingest", str(_source(tmp_path)), "--out", str(tmp_path / "out")])
    assert seen == [before], seen


def test_an_engine_valueerror_in_the_pdf_path_is_not_a_converter_refusal(monkeypatch,
                                                                         tmp_path) -> None:
    def broken(self, *a, **kw):
        raise ValueError("an engine bug")

    monkeypatch.setattr(docling_arm.DoclingExtractor, "extract", broken)
    models = tmp_path / "models"
    from substrate import paths

    (models / paths.LAYOUT_MODELS).mkdir(parents=True)
    (models / paths.TABLE_MODELS).mkdir()
    pdf = _pdf(tmp_path / "survey.pdf", 1)
    with pytest.raises(ValueError, match="an engine bug"):
        cli.main(["ingest", str(pdf), "--doc-class", "reference-frozen", "--docling-models",
                  str(models), "--out", str(tmp_path / "out")])


def test_a_worker_that_cannot_start_says_so_and_leaves_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert"}))
    monkeypatch.setattr(convert, "docling_version", lambda: "9.9")
    made: list[str] = []
    real = worker_client.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(**kw))
        return made[-1]

    def unavailable(_cpu, _lifeline):
        raise worker_client.WorkerUnavailable("this interpreter does not know its own executable")

    monkeypatch.setattr(worker_client.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(worker_client, "_command", unavailable)
    rc, err = _run_cli(["ingest", str(_source(tmp_path)), "--out", str(tmp_path / "out")])
    assert rc == 2 and "does not know its own executable" in err and "extra" not in err, err
    assert made and not any(Path(d).exists() for d in made)


def test_the_docling_probe_imports_only_what_exists() -> None:
    import ast
    import importlib

    tree = ast.parse((ROOT / "probe_docling.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("substrate"):
            module = importlib.import_module(node.module)
            assert all(hasattr(module, a.name) for a in node.names), (node.module, node.names)


# ---------------------------------------------------------------- the payloads, from a worker

@pytest.mark.parametrize(("script", "said"), [
    ("send({'v': 1, 'type': 'converted', 'id': r['id'], 'bytes': 100, 'pages': 1, "
     "'status': 'S', 'seconds': 0}); raw(b'short'); os.close(1); time.sleep(60)",
     "ended after 5 of the 100 bytes"),
    ("send({'v': 1, 'type': 'converted', 'id': r['id'], 'bytes': 2, 'pages': 1, "
     "'status': 'S', 'seconds': 0}); raw(b'\\xff\\xfe'); bye()", "not UTF-8"),
    ("send({'v': 1, 'type': 'converted', 'id': r['id'], 'bytes': 64 * 1024 * 1024 + 1, "
     "'pages': 1, 'status': 'S', 'seconds': 0}); bye()", "out of bounds"),
    ("items(r, [{'label': 'text', 'text': 'a', 'page': 1, 'lang': None, 'height': None, "
     "'left': None, 'layer': 'B'}], count=2); bye()", "does not end"),
    ("send({'v': 1, 'type': 'items', 'id': r['id'], 'count': 1, 'bytes': 70000}); "
     "raw(b'{' + b'x' * 69999); bye()", "does not end"),
])
def test_a_payload_that_disagrees_with_its_header_is_a_protocol_fault(monkeypatch, fast, script,
                                                                      said) -> None:
    _fake(monkeypatch, f"hello(); r = request(); {script}")
    with pytest.raises(worker_client.WorkerFault, match=said) as e, _worker() as w:
        w.start()
        w.request({"type": "convert", "format": "asciidoc", "path": "/x"}, 2,
                  ("converted", "items"))
        w.finish()
    assert e.value.kind == "protocol"


def test_a_payload_longer_than_its_header_said_is_a_protocol_fault(monkeypatch, fast) -> None:
    _fake(monkeypatch, "hello(); r = request(); send({'v': 1, 'type': 'converted', 'id': r['id'], "
                       "'bytes': 2, 'pages': 1, 'status': 'S', 'seconds': 0}); raw(b'abEXTRA'); bye()")
    with pytest.raises(worker_client.WorkerFault, match="said more after its answer") as e, \
            _worker() as w:
        w.start()
        assert _ask(w)["markdown"] == "ab"
        w.finish()
    assert e.value.kind == "protocol"


# ---------------------------------------------------------------- the framing, at its edges

@pytest.mark.parametrize("field", ["seconds", "pages"])
def test_a_number_too_large_for_a_float_is_a_frame_error_not_a_crash(field) -> None:
    line = json.dumps(GOOD).replace(f'"{field}": {json.dumps(GOOD[field])}', f'"{field}": 1{"0" * 400}')
    with pytest.raises(frames.FrameError):
        frames.decode(line.encode())
    item = (b'{"label":"text","page":1,"lang":null,"height":1' + b"0" * 400 +
            b',"left":null,"layer":"B","text_bytes":0}\n')
    with pytest.raises(frames.FrameError):
        frames.decode_items(1, item)


def test_a_huge_number_from_a_worker_is_a_protocol_fault(monkeypatch, fast) -> None:
    _fake(monkeypatch, """
        hello(); r = request()
        raw(('{"v":1,"type":"converted","id":' + str(r["id"]) + ',"bytes":0,"pages":1,'
             '"status":"S","seconds":1' + '0' * 400 + '}\\n').encode())
        bye()
    """)
    with pytest.raises(worker_client.WorkerFault, match="could not trust") as e, _worker() as w:
        w.start()
        _ask(w)
    assert e.value.kind == "protocol"


@pytest.mark.parametrize(("size", "ok"), [(frames.MAX_LINE, True), (frames.MAX_LINE + 1, False)])
def test_a_header_line_is_taken_up_to_exactly_the_bound(monkeypatch, fast, size, ok) -> None:
    """A line of exactly MAX_LINE bytes with its newline is the largest the worker may send, and
    its newline arrives in a later write than the rest, as a pipe may deliver it."""
    _fake(monkeypatch, f"""
        hello(); r = request()
        head = '{{"v":1,"type":"refused","id":%d,"reason":"' % r['id']
        pad = {size} - len(head) - len('"}}') - 1
        raw((head + 'x' * pad + '"}}').encode()); time.sleep(0.2); raw(b'\\n'); bye()
    """)
    with _worker() as w:
        w.start()
        if ok:
            with pytest.raises(worker_client.WorkerFault, match="refused frame|out of bounds"):
                _ask(w)
        else:
            with pytest.raises(worker_client.WorkerFault, match="over 65536 bytes"):
                _ask(w)


def test_empty_items_and_empty_texts_are_frames(monkeypatch) -> None:
    assert frames.decode_items(0, b"") == []
    data = frames.encode_items(1, [{**ITEM, "text": ""}])
    header, payload = data.split(b"\n", 1)
    assert frames.decode_items(frames.decode(header)["count"], payload) == [{**ITEM, "text": ""}]


def test_a_reply_before_its_request_is_a_protocol_fault(monkeypatch, fast) -> None:
    """Bytes the worker left after one answer are not the next one's. Both answers go in one write
    under PIPE_BUF, so the second is in the engine's buffer when the next request would be sent: an
    early answer that arrives only after that request looks like any answer, and is read as data."""
    _fake(monkeypatch, """
        r = (hello(), request())[1]
        def frame(rid):
            return (json.dumps({"v": 1, "type": "converted", "id": rid, "bytes": 2, "pages": 1,
                                "status": "SUCCESS", "seconds": 0.1}) + "\\n").encode() + b"ab"
        raw(frame(r["id"]) + frame(r["id"] + 1)); bye()
    """)
    with pytest.raises(worker_client.WorkerFault, match="said more after its answer"), \
            _worker() as w:
        w.start()
        _ask(w)
        _ask(w)


def test_a_request_the_engine_cannot_frame_is_a_refusal_not_a_fault(monkeypatch, fast,
                                                                   tmp_path) -> None:
    """A file name that is not UTF-8 (on Linux, `os.fsdecode` gives it a lone surrogate) cannot be
    named to the worker: the file's answer, every time, never a fault to quarantine."""
    from substrate.extract.base import sha256_file

    spawned = _fake(monkeypatch, "hello(); bye()")
    src = _source(tmp_path)
    spec = convert.spec_for(src)

    def work(worker):
        return worker.request({"type": "convert", "format": "asciidoc", "path": "/rev\udce9iew"},
                              2, ("converted",))

    with pytest.raises(convert.ConversionRefused, match="cannot be named to the converter"):
        convert.through_worker(src, spec, real=src, checksum=sha256_file(src), models=None,
                               budget=2, work=work,
                               accept=lambda reply, docling: reply,
                               quarantine=lambda: tmp_path / "store", retry_quarantined=False,
                               log=lambda *_: None)
    assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_a_refusal_s_reason_always_fits_its_line() -> None:
    from substrate.extract import worker

    reply = {"v": 1, "type": "refused", "id": 1, "reason": "\x01" * frames.MAX_SENTENCE}
    data = worker.wire(reply)
    assert frames.decode(data[:-1])["reason"] == reply["reason"]


# ---------------------------------------------------------------- what the adversary found

def _waiting_worker(pidfile: Path, go: Path) -> str:
    """Takes the request, says so, and answers once `go` exists."""
    return PRELUDE + textwrap.dedent(f"""
        hello(); r = request()
        open({str(pidfile)!r} + ".tmp", "w").write(str(os.getpgrp()))
        os.rename({str(pidfile)!r} + ".tmp", {str(pidfile)!r})
        while not os.path.exists({str(go)!r}):
            time.sleep(0.05)
        converted(r); bye()
    """)


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
def test_an_engine_started_ignoring_a_stop_keeps_ignoring_it(tmp_path, sig) -> None:
    """`nohup` starts the engine with SIGHUP ignored, and a service manager may do the same for
    SIGTERM: that signal reaching it while the worker converts must not stop the ingest."""
    pidfile, go, out = tmp_path / "worker.pgid", tmp_path / "go", tmp_path / "out"
    code = DRIVER.format(root=str(ROOT), worker=_waiting_worker(pidfile, go),
                         src=str(_source(tmp_path)), out=str(out))
    engine = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
                              preexec_fn=lambda: signal.signal(sig, signal.SIG_IGN))
    deadline = time.monotonic() + 60
    while not pidfile.exists():
        if time.monotonic() > deadline or engine.poll() is not None:
            engine.kill()
            raise AssertionError(f"the worker never started: {engine.communicate()}")
        time.sleep(0.05)
    os.kill(engine.pid, sig)
    time.sleep(0.3)
    go.touch()
    _stdout_text, err = engine.communicate(timeout=60)
    assert engine.returncode == 0, (engine.returncode, err)
    assert "FATAL" not in err and (out / "run.json").exists(), err


def test_an_ignored_stop_stays_ignored_inside_the_handlers(monkeypatch) -> None:
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with worker_client.interruptible():
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
            assert signal.getsignal(signal.SIGTERM) != signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGHUP)
            with worker_client._uninterrupted():
                assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
                os.kill(os.getpid(), signal.SIGHUP)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def _xattr(path: str) -> str:
    """Script lines that set an extended attribute, as a Finder tag or a sync client does."""
    return (f"os.setxattr({path}, 'user.substrate-test', b'1')" if hasattr(os, "setxattr") else
            f"import subprocess; subprocess.run(['/usr/bin/xattr', '-w', 'com.substrate.test', "
            f"'1', {path}], check=True)")


METADATA_ONLY = {
    "chmod": "os.chmod(r['path'], 0o600)",
    "xattr": _xattr("r['path']"),
    "touch": "st = os.stat(r['path']); os.utime(r['path'], ns=(st.st_atime_ns, "
             "st.st_mtime_ns + 10**9))",
}


@pytest.mark.parametrize("change", sorted(METADATA_ONLY))
def test_a_change_to_the_file_s_metadata_alone_is_not_a_changed_file(monkeypatch, fast, tmp_path,
                                                                     change) -> None:
    """Only the bytes decide: the file's mode, an extended attribute or its times changing while
    it converts leave its bytes, and its conversion, what they were."""
    _fake(monkeypatch, f"hello(); r = request(); {METADATA_ONLY[change]}; converted(r); bye()")
    conv = _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store")
    assert "harbour" in conv.markdown and q.listing(tmp_path / "store") == []


def _models(tmp_path: Path) -> Path:
    from substrate import paths

    models = tmp_path / "models"
    (models / paths.LAYOUT_MODELS).mkdir(parents=True)
    (models / paths.TABLE_MODELS).mkdir()
    return models


ITEM_LINE = ('{"label": "text", "text": "The meadow drains toward the harbour.", "page": r["first"], '
             '"lang": None, "height": 10.0, "left": 10.0, "layer": "BODY"}')


def _pdf_worker(after_first: str = "", missing: str = "", before_bye: str = "") -> str:
    """A models worker answering every batch with one item. `after_first` runs after the first
    answer; a batch whose file is gone is answered as docling answers it, refused; `before_bye`
    runs after the last batch, before the engine's `bye`."""
    return f"""
        hello(); n = 0
        while True:
            r = request()
            if r is None or r["type"] == "bye":
                sys.exit(0)
            n += 1
            if not os.path.exists(r["path"]):
                send({{"v": 1, "type": "refused", "id": r["id"],
                      "reason": "docling could not read pages: no such file"}})
            else:
                items(r, [{ITEM_LINE}])
            if n == 1:
                {after_first or "pass"}
            if n == 2:
                {before_bye or "pass"}
    """


@pytest.mark.parametrize(("when", "how"), [
    ("before a later batch", "os.remove"),
    ("before a later batch", "move"),
    ("before assemble", "os.remove"),
    ("before assemble", "move"),
])
def test_a_models_pdf_removed_or_moved_mid_ingest_is_the_changed_file_refusal(monkeypatch, fast,
                                                                               tmp_path, when,
                                                                               how) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    gone = ("os.remove(r['path'])" if how == "os.remove" else
            "os.rename(r['path'], r['path'] + '.moved')")
    spawned = _fake(monkeypatch, _pdf_worker(**{
        "after_first" if when == "before a later batch" else "before_bye": gone}))
    pdf = _pdf(tmp_path / "survey.pdf", 2)
    rc, err = _run_cli(["ingest", str(pdf), "--doc-class", "reference-frozen", "--docling-models",
                        str(_models(tmp_path)), "--batch", "1", "--out", str(tmp_path / "out")])
    assert rc == 3 and "FATAL (changed)" in err and "Traceback" not in err, (rc, err)
    from substrate import blobs

    assert len(spawned) == 1 and q.listing(blobs.root()) == []


def test_a_models_pdf_whose_metadata_changes_mid_ingest_is_accepted(monkeypatch, fast,
                                                                    tmp_path) -> None:
    """`assemble` hashes the file again, and that hash is the whole check, as for any format."""
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    _fake(monkeypatch, _pdf_worker(after_first="os.chmod(r['path'], 0o600); " + _xattr("r['path']")))
    doc = docling_arm.DoclingExtractor(_models(tmp_path), batch_pages=1).extract(
        _pdf(tmp_path / "survey.pdf", 2), "reference-frozen", log=lambda *_: None)
    assert [b.text for b in doc.blocks] == ["The meadow drains toward the harbour."] * 2


@pytest.mark.parametrize("answer", ["converted(r)", "send({'v': 1, 'type': 'refused', "
                                                    "'id': r['id'], 'reason': 'no such file'})"])
@pytest.mark.parametrize("name", ["review.adoc", "table.csv"])
def test_a_document_removed_during_its_conversion_is_the_changed_file_refusal(monkeypatch, fast,
                                                                               tmp_path, answer,
                                                                               name) -> None:
    """Whether the converter answers from what it read first or refuses the missing file."""
    src = tmp_path / name
    src.write_text("harbour meadow culvert\n", "utf-8")
    spawned = _fake(monkeypatch, f"hello(); r = request(); os.remove(r['path']); {answer}; bye()")
    with pytest.raises(convert.FileChanged, match="while it was being ingested"):
        _convert(src, quarantine=lambda: tmp_path / "store")
    assert len(spawned) == 1 and q.listing(tmp_path / "store") == []


def test_an_image_removed_while_apple_s_reader_reads_it_is_the_changed_file_refusal(monkeypatch,
                                                                                  tmp_path) -> None:
    from substrate.extract import apple_arm

    src = tmp_path / "scan.png"
    src.write_bytes(b"not really an image")

    def read(path):
        Path(path).unlink()
        raise apple_arm.AppleReaderError("the file could not be opened")

    monkeypatch.setattr(apple_arm, "read", read)
    _no_spawn(monkeypatch)
    with pytest.raises(convert.FileChanged, match="while it was being ingested"):
        _convert(src)


def _slow_docling(tmp_path: Path, seconds: float) -> Path:
    """A stand-in docling whose converter takes `seconds` to import, as torch's does."""
    root = tmp_path / "slow-docling"
    pkg = root / "docling"
    (pkg / "datamodel").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "datamodel" / "__init__.py").write_text("")
    (pkg / "datamodel" / "pipeline_options.py").write_text("")
    (pkg / "datamodel" / "base_models.py").write_text(textwrap.dedent("""
        import enum
        class ConversionStatus(enum.Enum):
            SUCCESS = "success"
        class InputFormat(enum.Enum):
            ASCIIDOC = "asciidoc"
    """))
    (pkg / "document_converter.py").write_text(textwrap.dedent(f"""
        import time
        time.sleep({seconds})
        from docling.datamodel.base_models import ConversionStatus
        class _Document:
            def export_to_markdown(self):
                return "harbour meadow culvert"
            def num_pages(self):
                return 1
        class _Result:
            status = ConversionStatus.SUCCESS
            document = _Document()
        class DocumentConverter:
            def __init__(self, **kw):
                pass
            def convert(self, path):
                return _Result()
    """))
    return root


def test_the_worker_imports_docling_before_hello_not_on_the_document_s_clock(monkeypatch, fast,
                                                                             tmp_path) -> None:
    """The real worker, with a docling that takes 4 s to import and a document allowed 2 s: the
    import is the start's, under its own deadline, and the document is answered in time."""
    monkeypatch.setattr(worker_client, "HELLO_SECONDS", 30)
    slow = _slow_docling(tmp_path, 4)

    def command(cpu_seconds, lifeline):
        return [sys.executable, "-B", "-E", "-s", "-P", "-c",
                f"import sys; sys.path.insert(0, {str(slow)!r}); " + worker_client.BOOT,
                str(ROOT), str(cpu_seconds), str(lifeline)]

    monkeypatch.setattr(worker_client, "_command", command)
    with worker_client.Worker(2) as w:
        w.start()
        reply = w.request({"type": "convert", "format": "asciidoc", "path": "/x.adoc"}, 2,
                          ("converted",))
        w.finish()
    assert reply["markdown"] == "harbour meadow culvert"


def test_a_worker_slow_to_say_hello_within_its_limit_is_not_a_fault(monkeypatch, fast,
                                                                    tmp_path) -> None:
    _fake(monkeypatch, "time.sleep(1.5); " + OK)
    assert "harbour" in _convert(_source(tmp_path), quarantine=lambda: tmp_path / "store").markdown


def test_the_first_request_of_a_models_worker_has_the_model_load_allowance(monkeypatch,
                                                                            fast) -> None:
    load = worker_client.MODEL_LOAD_SECONDS
    assert worker_client.request_limit(70, first=True, loads_models=True) == 70 + load
    assert worker_client.request_limit(70, first=False, loads_models=True) == 70
    assert worker_client.request_limit(70, first=True, loads_models=False) == 70
    _fake(monkeypatch, "hello(); bye()")
    cpus = os.cpu_count() or 1
    for loads_models, allowed in ((True, 70 + load), (False, 70)):
        with worker_client.Worker(70, loads_models=loads_models) as w:
            assert w.cpu_seconds == (worker_client.HELLO_SECONDS + allowed) * cpus
            w.start()
            w.finish()


@pytest.mark.parametrize(("loads_models", "sleep", "first", "fault"), [
    (True, 2, True, False),
    (True, 5, True, True),
    (False, 2, True, True),
    (True, 2, False, True),
])
def test_a_models_worker_slow_on_its_first_request_by_less_than_the_allowance_is_no_fault(
        monkeypatch, fast, loads_models, sleep, first, fault) -> None:
    """A limit of 1 s and an allowance of 3: a first request that takes 2 s is the model load; 5 s
    is past both. A worker without models, or a later request, has no allowance."""
    monkeypatch.setattr(worker_client, "MODEL_LOAD_SECONDS", 3)
    slow = f"time.sleep({sleep}); "
    _fake(monkeypatch, "hello(); r = request(); " + ("" if first else "converted(r); r = request(); ")
          + slow + "converted(r); bye()")
    w = worker_client.Worker(10, loads_models=loads_models)
    with contextlib.ExitStack() as stack:
        if fault:
            e = stack.enter_context(pytest.raises(worker_client.WorkerFault))
        with w:
            w.start()
            if not first:
                w.request({"type": "convert", "format": "image", "path": "/x"}, 1, ("converted",))
            w.request({"type": "convert", "format": "image", "path": "/x"}, 1, ("converted",))
            w.finish()
    if fault:
        limit = 4 if loads_models and first else 1
        assert e.value.kind == "hang" and f"gave no answer in {limit} s" in e.value.said


def test_an_image_read_with_models_gets_the_allowance_through_the_driver(monkeypatch, fast,
                                                                         tmp_path) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "convert_deadline", lambda size: 1)
    monkeypatch.setattr(worker_client, "MODEL_LOAD_SECONDS", 3, raising=False)
    spawned = _fake(monkeypatch, "hello(); r = request(); time.sleep(2); converted(r); bye()")
    src = tmp_path / "scan.png"
    src.write_bytes(b"pixels")
    assert "harbour" in _convert(src, models=_models(tmp_path)).markdown and len(spawned) == 1


_SELECTOR = worker_client.selectors.DefaultSelector


class _FailingSelector:
    """Selectors the worker makes, kept; the second cannot be made: out of descriptors."""

    made: list = []
    fail = True

    def __new__(cls):
        if cls.fail and len(cls.made) == 1:
            raise OSError(24, "Too many open files")
        cls.made.append(_SELECTOR())
        return cls.made[-1]


class _FailingThread(threading.Thread):
    def start(self):
        raise RuntimeError("can't start new thread")


@pytest.mark.parametrize("failing", ["selector", "thread"])
def test_a_start_that_fails_after_the_spawn_kills_the_group_and_closes_everything(monkeypatch, fast,
                                                                                  failing) -> None:
    _fake(monkeypatch, "hello(); time.sleep(60)")
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*a, **kw):
        spawned.append(real_popen(*a, **kw))
        return spawned[-1]

    pipes: list[tuple[int, int]] = []
    real_pipe = os.pipe

    def pipe():
        pipes.append(real_pipe())
        return pipes[-1]

    monkeypatch.setattr(worker_client.subprocess, "Popen", popen)
    monkeypatch.setattr(worker_client.os, "pipe", pipe)
    made: list[str] = []
    real_mkdtemp = worker_client.tempfile.mkdtemp
    monkeypatch.setattr(worker_client.tempfile, "mkdtemp",
                        lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    _FailingSelector.made, _FailingSelector.fail = [], failing == "selector"
    monkeypatch.setattr(worker_client.selectors, "DefaultSelector", _FailingSelector)
    if failing == "thread":
        monkeypatch.setattr(worker_client.threading, "Thread", _FailingThread)
    expected = worker_client.WorkerUnavailable if failing == "selector" else RuntimeError
    with pytest.raises(expected):
        worker_client.Worker(10)
    monkeypatch.undo()
    (proc,) = spawned
    lifeline = pipes[0][1]  # the first pipe made is the lifeline; Popen makes the others
    assert proc.stdin.closed and proc.stdout.closed and proc.stderr.closed
    assert _FailingSelector.made and all(sel.get_map() is None for sel in _FailingSelector.made)
    with pytest.raises(OSError):
        os.fstat(lifeline)
    assert proc.returncode is not None and _gone_within(proc.pid, 3)
    assert not Path(made[0]).exists()


# ---------------------------------------------------------------- what the confirming round found

def test_a_symlinked_pdf_pointed_at_a_copy_of_its_old_bytes_mid_ingest_is_changed(monkeypatch, fast,
                                                                                  tmp_path) -> None:
    """The worker read the link's target; `assemble` hashes the link. Rewriting the target while
    the link moves to a copy of its old bytes must not pass as the bytes that were converted."""
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    target = _pdf(tmp_path / "survey-target.pdf", 2)
    link = tmp_path / "survey.pdf"
    link.symlink_to(target)
    copy = tmp_path / "survey-copy.pdf"
    swap = (f"import shutil; shutil.copyfile({str(target)!r}, {str(copy)!r}); "
            f"open({str(target)!r}, 'ab').write(b'%changed'); os.remove({str(link)!r}); "
            f"os.symlink({str(copy)!r}, {str(link)!r})")
    _fake(monkeypatch, _pdf_worker(after_first=swap))
    with pytest.raises(convert.FileChanged):
        docling_arm.DoclingExtractor(_models(tmp_path), batch_pages=1).extract(
            link, "reference-frozen", log=lambda *_: None)


@pytest.mark.parametrize("answer", ["converted(r)", "send({'v': 1, 'type': 'refused', "
                                                    "'id': r['id'], 'reason': 'not a file'})"])
def test_a_file_replaced_by_a_fifo_is_changed_without_blocking_on_it(monkeypatch, fast, tmp_path,
                                                                     answer) -> None:
    src = _source(tmp_path)
    _fake(monkeypatch, f"hello(); r = request(); os.remove(r['path']); os.mkfifo(r['path']); "
                       f"{answer}; bye()")
    outcome: list[BaseException] = []

    def run():
        try:
            _convert(src, quarantine=lambda: tmp_path / "store")
        except BaseException as e:  # noqa: BLE001
            outcome.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(20)
    if t.is_alive():
        # Release the read the engine is stuck in, so the test can end.
        fd = os.open(src, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(5)
        raise AssertionError("the engine blocked opening the FIFO")
    assert len(outcome) == 1 and isinstance(outcome[0], convert.FileChanged), outcome


def test_a_worker_started_by_an_engine_ignoring_sigterm_still_dies_of_it() -> None:
    """The engine keeps an inherited SIG_IGN, and a spawn passes it on; the worker must not, or
    the engine's gentle stop would only ever end by SIGKILL after the grace period."""
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        w = worker_client.Worker(10)
    finally:
        signal.signal(signal.SIGTERM, previous)
    try:
        w.start()
        os.kill(w.proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while w._peek() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        ended = w._peek()
        assert ended is not None and ended.si_code == os.CLD_KILLED, ended
        assert ended.si_status == signal.SIGTERM
    finally:
        w.close(gently=False)


def test_a_symlinked_pdf_whose_link_alone_is_removed_mid_ingest_is_changed(monkeypatch, fast,
                                                                          tmp_path) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    target = _pdf(tmp_path / "survey-target.pdf", 2)
    link = tmp_path / "survey.pdf"
    link.symlink_to(target)
    _fake(monkeypatch, _pdf_worker(before_bye=f"os.remove({str(link)!r})"))
    with pytest.raises(convert.FileChanged):
        docling_arm.DoclingExtractor(_models(tmp_path), batch_pages=1).extract(
            link, "reference-frozen", log=lambda *_: None)


def test_a_converter_that_would_not_preload_is_a_missing_extra_not_a_second_import(tmp_path) -> None:
    """torch, imported again after a failed import, raises a RuntimeError: the request must answer
    with the first failure, never import again."""
    root = tmp_path / "half-docling"
    pkg = root / "docling"
    (pkg / "datamodel").mkdir(parents=True)
    for name in ("__init__.py", "datamodel/__init__.py", "datamodel/pipeline_options.py"):
        (pkg / name).write_text("")
    (pkg / "datamodel" / "base_models.py").write_text(
        "import enum\nclass ConversionStatus(enum.Enum):\n    SUCCESS = 'success'\n"
        "class InputFormat(enum.Enum):\n    ASCIIDOC = 'asciidoc'\n")
    marker = tmp_path / "imported-once"
    (pkg / "document_converter.py").write_text(textwrap.dedent(f"""
        import os
        if os.path.exists({str(marker)!r}):
            raise RuntimeError("function '_has_torch_function' already has a docstring")
        open({str(marker)!r}, "w").close()
        raise ImportError("No module named 'torch'")
    """))
    command = [sys.executable, "-B", "-E", "-s", "-P", "-c",
               f"import sys; sys.path.insert(0, {str(root)!r}); " + worker_client.BOOT]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker_client, "_command",
                   lambda cpu, lifeline: [*command, str(ROOT), str(cpu), str(lifeline)])
        for frame, expect in (({"type": "convert", "format": "asciidoc", "path": "/x.adoc"},
                               "needs_extra"),):
            with worker_client.Worker(30) as w:
                w.start()
                reply = w.request(frame, 30, ("needs_extra", "converted", "refused"))
                w.finish()
            assert reply["type"] == expect and "torch" in reply["error"], reply
