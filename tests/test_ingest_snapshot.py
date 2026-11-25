"""An ingest reads its source once, into a private copy, and the checksum, the conversion, the probe
and the stored original are all of that copy, whatever happens to the file meanwhile. A LaTeX
source that includes another file is refused, since the copy holds the source alone.

The workers are `test_convert_worker`'s scripted ones, so nothing here needs docling, except the
tests that run docling's own LaTeX reader, where docling is installed and opted in.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_convert_worker as _worker_tests  # noqa: E402
from test_convert_worker import (  # noqa: E402
    ROOT,
    _fake,
    _models,
    _no_spawn,
    _pdf,
    _pdf_worker,
    _run_cli,
)

from substrate import blobs, classes  # noqa: E402
from substrate.extract import convert, snapshot, worker_client  # noqa: E402

fast = _worker_tests.fast
ORIGINAL = "harbour meadow culvert\n"
# The worker answers with exactly the bytes it was handed, so the markdown says what it read.
ECHO = "hello(); r = request(); converted(r, open(r['path']).read()); bye()"
DOCLING = os.environ.get("SUBSTRATE_DOCLING_TESTS") == "1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _made(monkeypatch) -> list[Path]:
    """The snapshot folders this test's ingests make: each staged, then renamed."""
    made: list[Path] = []
    mkdtemp, rename = tempfile.mkdtemp, os.rename

    def staged(*a, **kw):
        made.append(Path(mkdtemp(*a, **kw)))
        return str(made[-1])

    def renamed(old, new, *a, **kw):
        if Path(old) in made:
            made.append(Path(new))
        return rename(old, new, *a, **kw)

    monkeypatch.setattr(snapshot.tempfile, "mkdtemp", staged)
    monkeypatch.setattr(os, "rename", renamed)
    return made


# ---------------------------------------------------------------- the copy

def test_the_copy_is_private_named_as_the_source_and_gone_after(tmp_path) -> None:
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    with snapshot.snapshot(src) as snap:
        assert snap.path.name == src.name and snap.path != src
        assert snap.checksum == _sha(src.read_bytes()) == _sha(snap.path.read_bytes())
        assert stat.S_IMODE(snap.path.stat().st_mode) == 0o400
        assert stat.S_IMODE(snap.path.parent.stat().st_mode) == 0o700
        folder = snap.path.parent
    assert not folder.exists()


def test_the_copy_of_a_link_is_named_as_the_link(tmp_path) -> None:
    target = tmp_path / "survey-v3.pdf"
    target.write_bytes(b"%PDF-1.7 pretend")
    link = tmp_path / "current.pdf"
    link.symlink_to(target)
    with snapshot.snapshot(link) as snap:
        assert snap.path.name == "current.pdf"


def test_the_copy_is_removed_however_the_ingest_ends(tmp_path) -> None:
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    for stop in (RuntimeError("an engine bug"), worker_client.Interrupted(15), KeyboardInterrupt()):
        with pytest.raises(type(stop)), snapshot.snapshot(src) as snap:
            folder = snap.path.parent
            raise stop
        assert not folder.exists(), type(stop)


def test_a_copy_whose_ingest_ended_is_removed_and_a_running_one_kept(tmp_path) -> None:
    """A folder whose lock nobody holds is a dead ingest's; one whose lock is held is running."""
    tmp = Path(tempfile.gettempdir())
    dead = Path(tempfile.mkdtemp(prefix=snapshot.PREFIX, dir=tmp))
    alive = Path(tempfile.mkdtemp(prefix=snapshot.PREFIX, dir=tmp))
    odd = tmp / f"{snapshot.PREFIX}99999999999999999999-²"
    odd.mkdir()
    for d in (dead, alive):
        (d / ".lock").touch()
        (d / "review.adoc").write_text(ORIGINAL)
    held = os.open(alive / ".lock", os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        src = tmp_path / "review.adoc"
        src.write_text(ORIGINAL, "utf-8")
        with snapshot.snapshot(src):
            pass
        # A folder holds its lock from the moment it has its name, so one without is a dead one.
        assert not dead.exists() and alive.exists() and not odd.exists()
    finally:
        os.close(held)
        import shutil

        for d in (dead, alive, odd):
            shutil.rmtree(d, ignore_errors=True)


def test_a_fifo_in_the_source_s_place_is_refused_without_blocking(tmp_path) -> None:
    src = tmp_path / "review.adoc"
    os.mkfifo(src)
    outcome: list[BaseException] = []

    def take():
        try:
            with snapshot.snapshot(src):
                pass
        except BaseException as e:  # noqa: BLE001
            outcome.append(e)

    t = threading.Thread(target=take, daemon=True)
    t.start()
    t.join(20)
    if t.is_alive():
        os.close(os.open(src, os.O_WRONLY | os.O_NONBLOCK))
        t.join(5)
        raise AssertionError("the snapshot blocked opening the FIFO")
    assert len(outcome) == 1 and isinstance(outcome[0], snapshot.SourceUnreadable), outcome


@pytest.mark.parametrize("kind", ["cannot be opened", "a folder"])
def test_a_source_that_is_not_a_readable_file_is_an_input_refusal(monkeypatch, fast, tmp_path,
                                                                  kind) -> None:
    src = tmp_path / "review.adoc"
    if kind == "a folder":
        src.mkdir()
    else:
        src.write_text(ORIGINAL, "utf-8")
        src.chmod(0)
    _no_spawn(monkeypatch)
    try:
        rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    finally:
        if kind != "a folder":
            src.chmod(0o600)
    assert rc == 2 and "FATAL (read)" in err and "Traceback" not in err, err


def test_a_copy_that_cannot_be_written_is_the_engine_s_problem(monkeypatch, fast, tmp_path) -> None:
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    real_open, real_write, copies = os.open, os.write, set()

    def opening(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if snapshot.PREFIX in str(path) and str(path).endswith(src.name) and flags & os.O_CREAT:
            copies.add(fd)
        return fd

    def full(fd, data):
        if fd in copies:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    made = _made(monkeypatch)
    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "write", full)
    _no_spawn(monkeypatch)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert rc == 2 and "FATAL (engine)" in err and "No space left" in err, err
    assert copies and made and not any(d.exists() for d in made)


def test_a_stop_during_the_writes_removes_the_copy_and_ends_the_ingest(tmp_path) -> None:
    """Outside the conversion a SIGTERM ends the ingest as it always has, once the copy is gone."""
    seen = tmp_path / "copy-folder"
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    worker = _worker_tests.PRELUDE + textwrap.dedent(ECHO)
    driver = textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {str(ROOT)!r})
        from substrate import cli
        from substrate.extract import convert, worker_client
        from substrate.markdown import ingest
        worker_client._command = lambda cpu, lifeline: [sys.executable, "-c", {worker!r},
                                                        {str(ROOT)!r}, str(lifeline)]
        convert.installed_extras = lambda: frozenset({{"convert"}})
        convert.docling_version = lambda: "9.9"
        real = ingest.ingest_markdown
        def stopping(tmp, out, **kw):
            import tempfile, glob
            folders = glob.glob(os.path.join(tempfile.gettempdir(), "substrate-source-*"))
            open({str(seen)!r}, "w").write("\\n".join(folders))
            os.kill(os.getpid(), signal.SIGTERM)
            return real(tmp, out, **kw)
        ingest.ingest_markdown = stopping
        sys.exit(cli.main(["ingest", {str(src)!r}, "--out", {str(tmp_path / "out")!r}]))
    """)
    run = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True,
                         timeout=120, env={"PATH": os.environ.get("PATH", ""),
                                           "HOME": str(tmp_path), "TMPDIR": str(tmp_path)})
    assert run.returncode == -signal.SIGTERM, (run.returncode, run.stderr[-1500:])
    folders = [Path(f) for f in seen.read_text().splitlines() if f]
    assert folders and not any(f.exists() for f in folders), folders


# ---------------------------------------------------------------- the bytes that were read

def _rewrite(src: Path, how: str) -> str:
    """Script lines that change `src` in one way while the worker has it."""
    p = repr(str(src))
    return {
        "appended": f"open({p}, 'a').write('tide pool\\n')",
        "rewritten, size and mtime kept": f"st = os.stat({p}); open({p}, 'r+b').write("
                                          f"open({p}, 'rb').read().upper()); os.utime({p}, "
                                          "ns=(st.st_atime_ns, st.st_mtime_ns))",
        "removed": f"os.remove({p})",
        "moved": f"os.rename({p}, {p} + '.moved')",
        "replaced by a FIFO": f"os.remove({p}); os.mkfifo({p})",
        "metadata alone": f"os.chmod({p}, 0o600); st = os.stat({p}); os.utime({p}, "
                          "ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))",
    }[how]


HOWS = ["appended", "rewritten, size and mtime kept", "removed", "moved", "replaced by a FIFO",
        "metadata alone"]


@pytest.mark.parametrize("when", ["before the worker starts", "while the worker converts",
                                  "before the probe"])
@pytest.mark.parametrize("how", HOWS)
def test_the_markdown_the_checksum_and_the_original_are_of_the_bytes_read_once(
        monkeypatch, fast, tmp_path, how, when) -> None:
    """Whenever the file changes, and however, the note is of the bytes the ingest read: its
    markdown, its checksum, and the original stored under that checksum."""
    src = tmp_path / "survey.csv"   # a format with a probe, so the probe's read is tested too
    src.write_text(ORIGINAL, "utf-8")
    change = _rewrite(src, how)
    if when == "while the worker converts":
        _fake(monkeypatch, f"hello(); r = request(); {change}; "
                           "converted(r, open(r['path']).read()); bye()")
    else:
        _fake(monkeypatch, ECHO)
        namespace = {"os": os}

        def act():
            exec(change, namespace)  # noqa: S102 — the test's own fixed lines

        if when == "before the worker starts":
            real_start = worker_client.Worker.start
            monkeypatch.setattr(worker_client.Worker, "start",
                                lambda self: (act(), real_start(self))[1])
        else:
            real_verify = convert.verify_conversion
            monkeypatch.setattr(convert, "verify_conversion",
                                lambda *a, **kw: (act(), real_verify(*a, **kw))[1])
    out = tmp_path / "out"
    rc, err = _run_cli(["ingest", str(src), "--out", str(out)])
    assert rc == 0, err
    run = json.loads((out / "run.json").read_text())
    checksum = _sha(ORIGINAL.encode())
    assert run["source_sha256"] == checksum, run
    assert "harbour meadow culvert" in (out / "document.md").read_text()
    assert "tide pool" not in (out / "document.md").read_text().lower()
    assert blobs.verified(checksum).read_bytes() == ORIGINAL.encode()
    # Every change but one moves the file's size or modification time, which is all a stat sees.
    warned = ("WARNING (source): survey.csv changed while it was read; this note is of the bytes "
              "read when the ingest began.")
    assert (warned in err) == (how != "rewritten, size and mtime kept"), err


def test_the_worker_is_handed_the_private_copy(monkeypatch, fast, tmp_path) -> None:
    seen = tmp_path / "seen.json"
    _fake(monkeypatch, f"""
        import hashlib
        hello(); r = request()
        data = open(r["path"], "rb").read()
        open({str(seen)!r}, "w").write(json.dumps({{"path": r["path"],
            "sha256": hashlib.sha256(data).hexdigest(), "mode": os.stat(r["path"]).st_mode}}))
        converted(r, data.decode()); bye()
    """)
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert rc == 0 and "WARNING (source)" not in err, err
    got = json.loads(seen.read_text())
    assert got["path"] != str(src) and Path(got["path"]).name == src.name
    assert got["sha256"] == _sha(ORIGINAL.encode()) and stat.S_IMODE(got["mode"]) == 0o400
    assert not Path(got["path"]).exists()


def test_a_linked_document_s_note_is_named_after_the_link(monkeypatch, fast, tmp_path) -> None:
    target = tmp_path / "review-v3.adoc"
    target.write_text(ORIGINAL, "utf-8")
    link = tmp_path / "review.adoc"
    link.symlink_to(target)
    _fake(monkeypatch, ECHO)
    out = tmp_path / "out"
    rc, err = _run_cli(["ingest", str(link), "--out", str(out)])
    assert rc == 0, err
    assert json.loads((out / "run.json").read_text())["doc_id"].startswith("review-")


@pytest.mark.parametrize("how", ["appended", "removed", "replaced by a FIFO"])
def test_a_models_pdf_changed_between_batches_is_read_as_it_was(monkeypatch, fast, tmp_path,
                                                               how) -> None:
    monkeypatch.setattr(convert, "installed_extras", lambda: frozenset({"convert", "convert-ml"}))
    monkeypatch.setattr(worker_client, "pdf_batch_deadline", lambda pages: 2)
    monkeypatch.setattr(classes, "apply", lambda doc: {"title": "Survey"})
    pdf = _pdf(tmp_path / "survey.pdf", 2)
    original = pdf.read_bytes()
    _fake(monkeypatch, _pdf_worker(after_first=_rewrite(pdf, how).replace("'tide pool\\n'",
                                                                          "'%tide'")))
    out = tmp_path / "out"
    rc, err = _run_cli(["ingest", str(pdf), "--doc-class", "reference-frozen", "--docling-models",
                        str(_models(tmp_path)), "--batch", "1", "--out", str(out)])
    assert rc == 0, err
    run = json.loads((out / "run.json").read_text())
    assert run["source_sha256"] == _sha(original) and run["source"] == str(pdf)
    assert blobs.verified(_sha(original)).read_bytes() == original
    assert run["extract"]["blocks"] == 2


def _text_layer(monkeypatch, seen: list[Path]) -> None:
    """A text-layer reader that records the path it is given and assembles one block from it."""
    from substrate.extract import pdftext_arm
    from substrate.extract.assemble import assemble
    from substrate.models import Block, Kind

    def extract(self, pdf, doc_class, *, pages=None, log=print):
        seen.append(pdf)
        block = Block(id="b000001", kind=Kind.TEXT, text="The meadow drains toward the harbour.",
                      page=1, label="text", level=None, lang=None, height=10.0, left=10.0,
                      furniture_claimed=False)
        return assemble(pdf, doc_class, [block], total_pages=1, pages_processed=1, seconds=0.1,
                        extractor="pdf-text", extractor_arm="pdf-text", layout_model="none")

    monkeypatch.setattr(pdftext_arm.TextLayerExtractor, "extract", extract)
    monkeypatch.setattr(classes, "apply", lambda doc: {"title": "Survey"})


def test_the_text_layer_reader_reads_the_copy_and_the_note_names_the_file(monkeypatch,
                                                                          tmp_path) -> None:
    seen: list[Path] = []
    _text_layer(monkeypatch, seen)
    target = tmp_path / "survey-v3.pdf"
    target.write_bytes(b"%PDF-1.7 pretend")
    pdf = tmp_path / "current.pdf"
    pdf.symlink_to(target)
    out = tmp_path / "out"
    rc, err = _run_cli(["ingest", str(pdf), "--doc-class", "reference-frozen", "--out", str(out)])
    assert rc == 0, err
    assert seen and seen[0] != pdf and seen[0].name == "current.pdf"
    run = json.loads((out / "run.json").read_text())
    assert run["source"] == str(pdf) and run["doc_id"].startswith("current-")
    assert blobs.verified(run["source_sha256"]).read_bytes() == b"%PDF-1.7 pretend"


def test_apple_s_reader_reads_the_copy(monkeypatch, fast, tmp_path) -> None:
    from substrate.extract import apple_arm

    seen = []

    def read(path, **kw):
        seen.append((Path(path), Path(path).read_bytes()))
        raise apple_arm.AppleReaderError("no text")

    monkeypatch.setattr(apple_arm, "read", read)
    src = tmp_path / "scan.png"
    src.write_bytes(b"pixels")
    _no_spawn(monkeypatch)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert rc == 3 and seen and seen[0][0] != src and seen[0][1] == b"pixels", (rc, err, seen)


# ---------------------------------------------------------------- the copy: names, locks, paths

def test_a_source_named_as_the_lock_is_copied_beside_it(tmp_path) -> None:
    src = tmp_path / ".lock"
    src.write_text(ORIGINAL, "utf-8")
    with snapshot.snapshot(src) as snap:
        assert snap.path.read_text() == ORIGINAL and snap.path.parent.name == "copy"


def test_a_new_copy_is_never_swept_before_its_lock_is_held(monkeypatch, tmp_path) -> None:
    """Another ingest's sweep, run between this one making its lock and taking it, finds no folder
    of its own name there to remove."""
    real_open, real_flock, locks = os.open, fcntl.flock, set()

    def opening(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if str(path).endswith(".lock") and flags & os.O_CREAT:
            locks.add(fd)
        return fd

    def flock(fd, op):
        if fd in locks:
            locks.discard(fd)
            snapshot._sweep(Path(tempfile.gettempdir()))
        return real_flock(fd, op)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(snapshot.fcntl, "flock", flock)
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    with snapshot.snapshot(src) as snap:
        assert snap.path.read_text() == ORIGINAL
    assert not locks


def test_a_source_moved_while_it_was_opened_is_refused(monkeypatch, tmp_path) -> None:
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    monkeypatch.setattr(snapshot, "_opened_at", lambda fd: str(tmp_path / "elsewhere.adoc"))
    made = _made(monkeypatch)
    with pytest.raises(snapshot.SourceUnreadable, match="moved while it was being opened"):
        with snapshot.snapshot(src):
            pass
    assert not made


def test_the_descriptor_s_path_is_read_where_the_kernel_gives_it(tmp_path) -> None:
    """Linux names an open file's path in `/proc`, which the check compares; macOS has no `/proc`
    and refuses a link anywhere in the path instead (`O_NOFOLLOW_ANY`)."""
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    fd = os.open(src, os.O_RDONLY)
    try:
        at = snapshot._opened_at(fd)
    finally:
        os.close(fd)
    assert at == (str(src.resolve()) if sys.platform.startswith("linux") else None), at


def test_a_name_that_would_act_on_the_terminal_is_printed_inert(monkeypatch, fast,
                                                                tmp_path) -> None:
    src = tmp_path / "re\x1b]0;owned\x07view.adoc"
    src.write_text(ORIGINAL, "utf-8")
    src.chmod(0)
    _no_spawn(monkeypatch)
    try:
        rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    finally:
        src.chmod(0o600)
    assert rc == 2 and "FATAL (read): re]0;ownedview.adoc could not be read" in err, err
    assert "\x1b" not in err and "\x07" not in err, err


def test_a_copy_made_while_the_file_was_written_is_refused(monkeypatch, tmp_path) -> None:
    """A write in place during the copy, at the same size: the copy may hold bytes the file never
    held at once, so it is refused rather than converted."""
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    real_read, wrote = os.read, []

    def reading(fd, n):
        block = real_read(fd, n)
        if block and not wrote:
            wrote.append(True)
            st = os.stat(src)
            src.write_bytes(ORIGINAL.upper().encode())
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        return block

    made = _made(monkeypatch)
    monkeypatch.setattr(os, "read", reading)
    with pytest.raises(snapshot.SourceUnreadable, match="was written to while it was being read"):
        with snapshot.snapshot(src):
            pass
    assert wrote and made and not any(d.exists() for d in made)


def test_a_staging_folder_left_by_a_killed_ingest_is_removed_once_old(tmp_path) -> None:
    tmp = Path(tempfile.gettempdir())
    old = Path(tempfile.mkdtemp(prefix=snapshot._STAGING, dir=tmp))
    fresh = Path(tempfile.mkdtemp(prefix=snapshot._STAGING, dir=tmp))
    then = os.stat(old).st_mtime - snapshot._STAGED_FOR - 60
    os.utime(old, (then, then))
    try:
        src = tmp_path / "review.adoc"
        src.write_text(ORIGINAL, "utf-8")
        with snapshot.snapshot(src):
            pass
        assert not old.exists() and fresh.exists()
    finally:
        import shutil

        for d in (old, fresh):
            shutil.rmtree(d, ignore_errors=True)


def test_a_handler_set_outside_python_is_left_alone(monkeypatch, tmp_path) -> None:
    """`getsignal` says None for a handler Python did not set, which could not be put back."""
    real_get, real_set, set_ = signal.getsignal, signal.signal, []
    monkeypatch.setattr(signal, "getsignal", lambda s: None if s in (signal.SIGTERM, signal.SIGHUP)
                        else real_get(s))
    monkeypatch.setattr(signal, "signal", lambda s, h: (set_.append(s), real_set(s, h))[1])
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    with snapshot.snapshot(src) as snap:
        assert snap.path.read_text() == ORIGINAL
    assert not set_, set_


@pytest.mark.parametrize("fails", ["fchmod", "F_SETFL"])
def test_a_failing_call_on_the_copy_or_the_source_is_an_exit_2(monkeypatch, fast, tmp_path,
                                                               fails) -> None:
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    if fails == "fchmod":
        def fchmod(fd, mode):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(os, "fchmod", fchmod)
    else:
        real = fcntl.fcntl

        def fcntl_(fd, cmd, *a):
            if cmd == fcntl.F_SETFL:
                raise OSError(5, "Input/output error")
            return real(fd, cmd, *a)

        monkeypatch.setattr(snapshot.fcntl, "fcntl", fcntl_)
    _no_spawn(monkeypatch)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    said = "FATAL (engine)" if fails == "fchmod" else "FATAL (read)"
    assert rc == 2 and said in err and "Traceback" not in err, err


def test_a_stop_while_the_copy_is_written_removes_it(tmp_path) -> None:
    seen = tmp_path / "copy-folder"
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    driver = textwrap.dedent(f"""
        import glob, os, signal, sys, tempfile
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from substrate.extract import snapshot
        real = snapshot._copy
        def stopping(*a):
            folders = glob.glob(os.path.join(tempfile.gettempdir(), "substrate-source-*"))
            open({str(seen)!r}, "w").write("\\n".join(folders))
            os.kill(os.getpid(), signal.SIGTERM)
            return real(*a)
        snapshot._copy = stopping
        with snapshot.snapshot(Path({str(src)!r})):
            pass
    """)
    run = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True,
                         timeout=120, env={"PATH": os.environ.get("PATH", ""),
                                           "HOME": str(tmp_path), "TMPDIR": str(tmp_path)})
    assert run.returncode == -signal.SIGTERM, (run.returncode, run.stderr[-1500:])
    folders = [Path(f) for f in seen.read_text().splitlines() if f]
    assert folders and not any(f.exists() for f in folders), folders


def test_a_store_failure_names_the_source_not_the_copy(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from substrate import cli

    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    copy = tmp_path / "copy" / "review.adoc"

    def put(path, expected):
        raise OSError(28, "No space left on device", str(copy))

    monkeypatch.setattr(blobs, "put", put)
    doc = SimpleNamespace(source_path=str(src), source_sha256=_sha(ORIGINAL.encode()),
                          note_sha256=_sha(ORIGINAL.encode()))
    with pytest.raises(blobs.BlobError) as e:
        cli._store_original(doc, copy=copy)
    assert str(e.value).startswith(f"{src}: No space left") and str(copy) not in str(e.value)


def test_the_copy_is_removed_before_the_stop_handlers_are_put_back(monkeypatch, tmp_path) -> None:
    before, during = signal.getsignal(signal.SIGTERM), []
    real = snapshot._remove
    monkeypatch.setattr(snapshot, "_remove", lambda folder: (
        during.append(signal.getsignal(signal.SIGTERM)), real(folder))[1])
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    with snapshot.snapshot(src) as snap:
        folder = snap.path.parent.parent
    assert during and during[0] != before and not folder.exists(), during
    assert signal.getsignal(signal.SIGTERM) == before


def test_no_temporary_folder_is_the_engine_s_problem(monkeypatch, fast, tmp_path) -> None:
    def none():
        raise FileNotFoundError(2, "No usable temporary directory found")

    monkeypatch.setattr(snapshot.tempfile, "gettempdir", none)
    src = tmp_path / "review.adoc"
    src.write_text(ORIGINAL, "utf-8")
    _no_spawn(monkeypatch)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert rc == 2 and "FATAL (engine)" in err and "Traceback" not in err, err


# ---------------------------------------------------------------- LaTeX: one file only

def _latex(folder: Path, body: str, files: dict[str, str] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (files or {}).items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    src = folder / "paper.tex"
    src.write_text(f"\\documentclass{{article}}\\begin{{document}}Harbour words.\n{body}\n"
                   "\\end{document}\n")
    return src


@pytest.mark.parametrize(("body", "command", "line"), [
    ("\\input{chapter}", "\\input", 2),
    ("\\include{chapter}", "\\include", 2),
    ("% \\input{old}", "\\input", 2),
    ("Words.\n\n\\input chapter", "\\input", 4),
])
def test_a_latex_source_that_includes_another_file_is_refused(monkeypatch, fast, tmp_path, body,
                                                              command, line) -> None:
    src = _latex(tmp_path / "p", body, {"chapter.tex": "Chapter words.", "old.tex": "Old."})
    _no_spawn(monkeypatch)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert rc == 3, err
    assert (f"FATAL (conversion): latex: paper.tex uses {command} on line {line}, and a LaTeX "
            "source of several files is not supported yet. Flatten it into one file") in err, err


@pytest.mark.parametrize(("data", "line"), [
    (b"a\n\\input{x}", 2), (b"a\r\n\\input{x}", 2), (b"a\r\\input{x}", 2),
    (b"a\r\n\r\nb\r\\input{x}", 4),
])
def test_the_refusal_counts_lines_as_docling_does(tmp_path, data, line) -> None:
    src = tmp_path / "paper.tex"
    src.write_bytes(data)
    with pytest.raises(convert.ConversionRefused, match=f"on line {line},"):
        convert.refuse_includes(src)


@pytest.mark.parametrize(("data", "refused"), [
    (b"a\\\\input b", False), (b"\\\\\\\\include b", False), (b"a\\\\\\input{x}", True),
    (b"\\inputx", False), (b"\\input@path", True),
])
def test_a_command_is_a_backslash_not_after_another(data, refused) -> None:
    """`\\\\` is a line break, so the word after it is text, not a command."""
    assert (convert.latex_include(data) is not None) is refused


def test_a_latex_source_with_only_figures_is_converted(monkeypatch, fast, tmp_path) -> None:
    src = _latex(tmp_path / "p", "\\includegraphics{figure.png}\\inputencoding{utf8}",
                 {"figure.png": "x"})
    _fake(monkeypatch, ECHO)
    out = tmp_path / "out"
    rc, err = _run_cli(["ingest", str(src), "--out", str(out)])
    assert rc == 0, err
    assert json.loads((out / "run.json").read_text())["source_sha256"] == _sha(src.read_bytes())


def test_converting_a_latex_source_that_includes_is_refused_whoever_calls(fast, tmp_path) -> None:
    src = _latex(tmp_path / "p", "\\input{chapter}", {"chapter.tex": "Chapter words."})
    with pytest.raises(convert.ConversionRefused, match="uses \\\\input on line 2"):
        convert.to_markdown(src, convert.spec_for(src), log=lambda *_: None,
                            checksum=_sha(src.read_bytes()))


# ---------------------------------------------------------------- against docling's own reader

_DOCLING_READ = textwrap.dedent('''
    import json, os, sys
    sys.path[:0] = [sys.argv[1]]
    from substrate.extract import worker
    worker.preload()
    out = {}
    for case, path in json.loads(sys.argv[2]).items():
        reply = worker.convert_document({"v": 1, "id": 1, "type": "convert", "format": "latex",
                                         "path": path})
        out[case] = reply.get("markdown", "") if reply["type"] == "converted" else None
    print(json.dumps(out))
''')

# Every file docling could be asked to include, each holding its own marker word.
_TARGETS = ["t1.tex", "t2.tex", "t3.tex", "t1.txt", "sub/t1.tex", "sub/t2.tex", "real.tex",
            "caf\u00e9.tex", "t.tex", "@.tex", "\uff20.tex", "ch~one.tex", "ch&one.tex",
            "ch--one.tex", "ch\u00a0one.tex", "ch\u2013one.tex"]

# Include forms, and whether the engine refuses each. Docling reads a form's targets from the
# source's own folder, as it did before the private copy; every form whose targets reach its
# markdown must be refused, since the copy would not hold them.
_FORMS = {
    "input": ("\\input{t1}", True),
    "include": ("\\include{t1}", True),
    "suffix": ("\\input{t1.tex}", True),
    "other-suffix": ("\\input{t1.txt}", True),
    "spaced": ("\\input {t1}", True),
    "padded": ("\\input{ t1 }", True),
    "subfolder": ("\\input{sub/t1}", True),
    "nested": ("\\input{t2}", True),
    "several": ("\\input{t1}\\input{t2}\\include{t3}", True),
    "symlink": ("\\input{linked}", True),
    "cp1252-name": ("\\input{caf\u00e9}", True),
    "bare": ("\\input t1", True),
    "macro-name": ("\\newcommand{\\chap}{t1}\\input{\\chap}", True),
    "let": ("\\let\\inc\\input \\inc{t1}", True),
    "newline": ("\\input\n{t1}", True),
    "percent-newline": ("\\input%\n{t1}", True),
    "space-newline-space": ("\\include \n {t1}", True),
    "comment-in-group": ("\\input{%\nt1}", True),
    "crlf": ("\\input\r\n{t1}", True),
    "cr-only": ("a % b\r\\input{t1}", True),
    "input-at-path": ("\\makeatletter\\def\\input@path{{sub/}}\\makeatother", True),
    "input-fullwidth-at": ("\\input\uff20path", True),
    "tilde": ("\\input{ch~one}", True),
    "ampersand": ("\\input{ch&one}", True),
    "dashes": ("\\input{ch--one}", True),
    "escaped-percent": ("\\% \\input{t1}", True),
    "commented": ("% \\input{t1}", True),
    "verbatim": ("\\begin{verbatim}\\input{t1}\\end{verbatim}", True),
    "iffalse": ("\\iffalse \\input{t1} \\fi", True),
    "line-break-then-command": ("a\\\\\\input{t1}", True),
    "includeonly": ("\\includeonly{t1}", False),
    "line-break-then-word": ("a\\\\input{t1}", False),
    "figure": ("\\includegraphics{t1}", False),
    "inputencoding": ("\\inputencoding{utf8}", False),
    "csname": ("\\csname input\\endcsname{t1}", False),
    "at-input": ("\\@input{t1}", False),
}


def _marker(case: str, n: int) -> str:
    return "MK" + "".join(c for c in case if c.isalnum()) + f"N{n}"


def _form(folder: Path, case: str, line: str) -> Path:
    for n, name in enumerate(_TARGETS):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(f"{_marker(case, n)} words.")
    (folder / "t2.tex").write_text(f"{_marker(case, 1)} words. \\input{{sub/t2}}")
    (folder / "linked.tex").symlink_to(folder / "real.tex")
    newline = {"crlf": "\r\n", "cr-only": "\r"}.get(case, "\n")
    body = newline.join(["\\documentclass{article}\\begin{document}Harbour words.", line,
                         "\\end{document}", ""])
    src = folder / "paper.tex"
    src.write_bytes(body.encode("cp1252" if case == "cp1252-name" else "utf-8"))
    return src


def test_every_include_docling_s_latex_reader_follows_is_refused(tmp_path) -> None:
    """Runs docling's reader over every form on the locked docling, so an upgrade that teaches the
    reader a new form fails here before it is released."""
    if not DOCLING:
        return
    sources = {case: _form(tmp_path / case, case, line) for case, (line, _) in _FORMS.items()}
    run = subprocess.run([sys.executable, "-B", "-E", "-s", "-P", "-c", _DOCLING_READ, str(ROOT),
                          json.dumps({c: str(p) for c, p in sources.items()})],
                         capture_output=True, text=True, timeout=600,
                         env={"PATH": os.environ.get("PATH", ""), "HF_HUB_OFFLINE": "1"})
    assert run.returncode == 0, run.stderr[-2000:]
    markdown = json.loads(run.stdout.strip().splitlines()[-1])
    followed = {}
    for case, src in sources.items():
        md = markdown[case]
        assert md is not None and "Harbour words" in md, (case, md)
        followed[case] = [name for n, name in enumerate(_TARGETS) if _marker(case, n) in md]
        refused = convert.latex_include(src.read_bytes()) is not None
        assert refused == _FORMS[case][1], (case, refused)
        assert refused or not followed[case], (case, followed[case])
    # The forms docling is known to follow, so the test is of a reader that reads includes.
    assert {"input", "newline", "crlf", "nested", "bare"} <= {c for c, f in followed.items() if f}


def test_docling_converting_the_copy_opens_nothing_beside_the_source(monkeypatch, tmp_path) -> None:
    """The real worker's reader, run on what the engine hands it: a figure beside the source,
    which docling opens when it is handed the source's own path, is never opened, named by its
    relative path or its absolute one."""
    if not DOCLING:
        return
    folder = tmp_path / "paper"
    figures = f"\\includegraphics{{figure.png}}\\includegraphics{{{folder / 'figure.png'}}}"
    src = _latex(folder, figures, {"figure.png": "x", "refs.bib": "x"})
    from PIL import Image

    Image.new("RGB", (4, 4)).save(folder / "figure.png")
    seen = tmp_path / "seen.json"
    monkeypatch.setattr(convert, "docling_version", lambda: "9.9")     # the scripted hello's
    _fake(monkeypatch, f"""
        sys.path[:0] = [sys.argv[1]]
        folder = os.path.realpath({str(folder)!r}) + os.sep
        opened = []
        sys.addaudithook(lambda e, a: e == "open" and isinstance(a[0], (str, bytes))
                         and os.path.realpath(os.fsdecode(a[0])).startswith(folder)
                         and opened.append(os.path.basename(os.fsdecode(a[0]))))
        hello()
        from substrate.extract import worker
        worker.preload()
        r = request()
        reply = worker.convert_document(r)
        handed = sorted(set(opened))
        opened.clear()
        worker.convert_document({{**r, "path": {str(src)!r}}})
        open({str(seen)!r}, "w").write(json.dumps({{"type": reply["type"], "opened": handed,
                                                   "from_the_source": sorted(set(opened))}}))
        converted(r, reply.get("markdown", "")); bye()
    """)
    rc, err = _run_cli(["ingest", str(src), "--out", str(tmp_path / "out")])
    assert seen.exists(), (rc, err[-2000:])
    got = json.loads(seen.read_text())
    assert got["type"] == "converted" and got["opened"] == [], (got, rc, err[-1500:])
    # The hook sees what docling opens: handed the source itself, it opens the figure.
    assert "figure.png" in got["from_the_source"], got
