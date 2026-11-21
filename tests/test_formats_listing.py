"""`substrate formats` in each install state, read the way Scripta reads it.

Scripta's Library parses this listing by shape (`SubstrateCLI.parseFormats`): split a line on
spaces, take the first field as the token, then the run of fields that start with `.` as the
extensions, and skip a line with none. `REFUSED` rows are refusals. Nothing in `contract/` declares
the listing, so these tests are what holds its shape.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

from substrate import cli
from substrate.extract import apple_arm, convert

ROOT = Path(__file__).resolve().parents[1]
BOTH = frozenset({"convert", "convert-ml"})
STATES = [(extras, apple) for extras in (frozenset(), frozenset({"convert"}), BOTH)
          for apple in (False, True)]


def _printed() -> str:
    """`substrate formats`, as this engine prints it."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.cmd_formats(argparse.Namespace()) == 0
    return out.getvalue()


def _listing(monkeypatch, extras: frozenset[str], apple: bool, *, macos: bool = True) -> str:
    """The listing as an engine in this state prints it, on macOS unless `macos` says otherwise."""
    monkeypatch.setattr(convert, "installed_extras", lambda: extras)
    monkeypatch.setattr(apple_arm, "available", lambda: apple)
    monkeypatch.setattr(apple_arm, "on_macos", lambda: macos)
    return _printed()


def _parse(listing: str) -> tuple[dict[str, list[str]], list[tuple[list[str], str]]]:
    """Scripta's rule: (accepted token → extensions, [(refused extensions, reason)])."""
    accepted: dict[str, list[str]] = {}
    refused: list[tuple[list[str], str]] = []
    for line in listing.split("\n"):
        fields = [f for f in line.split(" ") if f]
        if not fields or fields[0] == "format":
            continue
        token, fields = fields[0], fields[1:]
        extensions = []
        while fields and fields[0].startswith("."):
            extensions.append(fields.pop(0).lower())
        if not extensions:
            continue
        if token == "REFUSED":
            rest = " ".join(fields)
            assert rest.startswith("— "), line
            refused.append((extensions, rest.removeprefix("— ")))
        else:
            accepted[token] = extensions
    return accepted, refused


@pytest.mark.parametrize("macos", [True, False], ids=["macos", "linux"])
@pytest.mark.parametrize(("extras", "apple"), STATES)
def test_every_extension_is_listed_once_in_every_state(monkeypatch, extras, apple, macos) -> None:
    accepted, refused = _parse(_listing(monkeypatch, extras, apple, macos=macos))
    listed = [e for exts in accepted.values() for e in exts] + [e for exts, _ in refused for e in exts]
    known = {e for s in convert.ACCEPTED for e in s.extensions} | set(convert.REFUSED)
    assert sorted(listed) == sorted(known), set(listed) ^ known
    assert len(listed) == len(set(listed)), [e for e in listed if listed.count(e) > 1]
    assert {"pdf", "markdown", "text"} <= set(accepted)


def test_the_bundle_lists_what_it_listed_before_the_extras(monkeypatch) -> None:
    """Both extras and Apple's recognizer, as Scripta ships the engine: byte for byte the listing
    `0c1686c` printed, which knew nothing of extras."""
    expected = (ROOT / "tests" / "data" / "formats-bundle.txt").read_text("utf-8")
    assert _listing(monkeypatch, BOTH, True) == expected


def _row(listing: str, token: str) -> str:
    """The note of an accepted row, in the state `_listing` set, which printed `listing`."""
    (row,) = [r for r in convert.table() if r[0] == token]
    assert row[3] in listing
    return row[3]


@pytest.mark.parametrize("macos", [True, False], ids=["macos", "linux"])
@pytest.mark.parametrize(("extras", "apple"), STATES)
def test_the_pdf_row_names_only_the_readers_here(monkeypatch, extras, apple, macos) -> None:
    """The text layer is the core's, so the row is always there; its note may not name a reader the
    engine lacks. Only Apple's recognizer reads a page that is only an image, since docling's PDF
    reader runs no OCR, so without it the note says such a page is refused and offers no extra."""
    note = _row(_listing(monkeypatch, extras, apple, macos=macos), "pdf")
    assert note.startswith("Its own text layer")
    assert ("Apple's recognizer for pages" in note) == apple
    assert ("--docling-models" in note) == ("convert-ml" in extras)
    assert ("is refused" in note) == (not apple)
    # Under --docling-models such a page is read as nothing, not refused, so the note says so.
    assert ("read no word from a page that is only an image" in note) == (
        not apple and "convert-ml" in extras)
    assert "install" not in note and "[convert-ml]" not in note


@pytest.mark.parametrize("macos", [True, False], ids=["macos", "linux"])
@pytest.mark.parametrize(("extras", "apple"), STATES)
def test_the_image_row_names_only_a_reader_that_would_work(monkeypatch, extras, apple,
                                                           macos) -> None:
    """Docling reads an image through Apple Vision, so it counts only on macOS with `convert-ml`,
    and the extra is offered only where installing it gives a reader."""
    listing = _listing(monkeypatch, extras, apple, macos=macos)
    docling = macos and "convert-ml" in extras
    accepted, refused = _parse(listing)
    if apple or docling:
        note = _row(listing, "image")
        assert ("Apple's on-device document recognizer" in note) == apple
        assert ("--docling-models" in note) == docling
        return
    (why,) = [why for exts, why in refused if ".png" in exts]
    assert "image" not in accepted
    assert (convert.install_hint("convert-ml") in why) == macos
    assert ("only on macOS" in why) == (not macos)


def test_the_full_notes_are_the_pieces_the_partial_ones_are_built_from(monkeypatch) -> None:
    """So an edit to a reader's wording reaches every install state, not the bundle's alone."""
    listing = _listing(monkeypatch, BOTH, True)
    assert _row(listing, "pdf") == convert.PDF.note
    assert _row(listing, "image") == convert.IMAGE.note


@pytest.mark.parametrize(("extras", "apple"), STATES)
def test_a_refusal_for_a_missing_extra_names_it_with_the_install_hint(monkeypatch, extras,
                                                                      apple) -> None:
    _, refused = _parse(_listing(monkeypatch, extras, apple))
    for spec in convert.ACCEPTED:
        rows = [why for exts, why in refused if spec.extensions[0] in exts]
        if spec.docling_extra is None or not rows:
            continue
        (why,) = rows
        assert spec.docling_extra not in extras, (spec.token, extras)
        assert f"[{spec.docling_extra}] extra" in why and convert.install_hint(spec.docling_extra) in why


@pytest.mark.parametrize("extras", [frozenset(), frozenset({"convert"}), BOTH],
                         ids=["core", "convert", "both"])
def test_an_image_on_linux_is_refused_for_the_platform(monkeypatch, extras) -> None:
    """docling's image OCR is Apple Vision's, so on Linux no extra reads an image. The row says why,
    and does not send the operator to install one, whichever they have."""
    _, refused = _parse(_listing(monkeypatch, extras, False, macos=False))
    (why,) = [why for exts, why in refused if ".png" in exts]
    assert "only on macOS" in why and "install" not in why and "convert-ml" not in why


def test_this_install_reads_as_the_extras_its_modules_say() -> None:
    """The listing reads distributions; this holds them to what imports here, in whatever install
    runs it. `convert-ml` needs OpenCV, so an install without it reads as `convert` alone."""
    def have(*modules: str) -> bool:
        return all(importlib.util.find_spec(m) is not None for m in modules)

    ml = ("torch", "docling_parse", "docling_ibm_models", "cv2") + (
        ("ocrmac",) if sys.platform == "darwin" else ())
    extras = convert.installed_extras()
    assert ("convert" in extras) == have("docling", "bs4", "openpyxl", "docx", "pptx"), extras
    assert ("convert-ml" in extras) == (have("docling", *ml)), extras


def _refused_for(rc: int, err: str) -> str | None:
    """The extra an ingest refusal says is missing, by the hint both messages share."""
    if rc != 2:
        return None
    named = [x for x in convert.EXTRA_DISTRIBUTIONS if convert.install_hint(x) in err]
    assert len(named) <= 1, err
    return named[0] if named else None


@pytest.mark.parametrize("spec", [s for s in convert.ACCEPTED if s.arm == "docling"],
                         ids=lambda s: s.token)
def test_formats_and_ingest_agree_here(tmp_path, spec) -> None:
    """In this install, as each CI job has it: a format `formats` lists is not refused by ingest for
    a missing extra, and one it refuses is refused by ingest naming the same extra, or none where
    the row names none."""
    accepted, refused = _parse(_printed())
    src = tmp_path / f"fixture{spec.extensions[0]}"
    src.write_bytes(b"placeholder")
    out = io.StringIO()
    args = argparse.Namespace(out=str(tmp_path / "ingest"), doc_class=None)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out):
        rc = cli._ingest_converted(args, src, spec, None)
    said = _refused_for(rc, out.getvalue())
    if spec.token in accepted:
        assert said is None, (spec.token, rc, out.getvalue())
        return
    (why,) = [why for exts, why in refused if spec.extensions[0] in exts]
    hinted = [x for x in convert.EXTRA_DISTRIBUTIONS if convert.install_hint(x) in why]
    assert rc == 2 and said == (hinted[0] if hinted else None), (spec.token, rc, out.getvalue())


@pytest.mark.parametrize("macos", [True, False], ids=["macos", "linux"])
@pytest.mark.parametrize("extras", [frozenset({"convert"}), BOTH], ids=["convert", "both"])
def test_an_image_with_no_apple_reader_names_only_a_reader_that_would_work(monkeypatch, tmp_path,
                                                                         extras, macos) -> None:
    """D4: without `bin/extract-apple`, docling reads an image only on macOS, through Apple Vision,
    and only with the `convert-ml` extra and a models folder. The refusal named only the flag, so an
    operator with neither was sent to a flag that could not work; off macOS, naming the extra sends
    them to install one that reads nothing. Ingest names the reader the row names, or none."""
    listing = _listing(monkeypatch, extras, False, macos=macos)
    monkeypatch.setattr(apple_arm, "BINARY", tmp_path / "no-extract-apple")
    src = tmp_path / "scan.png"
    src.write_bytes(b"\x89PNG pretend")
    out = io.StringIO()
    args = argparse.Namespace(out=str(tmp_path / "ingest"), doc_class=None)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out):
        rc = cli._ingest_converted(args, src, convert.spec_for(src), None)
    err = out.getvalue()
    assert rc == 2 and "Traceback" not in err and convert.image_remedy(extras) in err, err
    assert ("--docling-models DIR" in err) == macos, err
    assert (convert.install_hint("convert-ml") in err) == (macos and "convert-ml" not in extras), err
    assert ("only on macOS" in err) == (not macos), err
    assert ("tools/build-bundled-engine" in err) == macos, err
    accepted, refused = _parse(listing)
    started = "Apple's document reader could not start: [Errno 8] Exec format error"
    monkeypatch.setattr(apple_arm, "read", lambda *a, **k: (_ for _ in ()).throw(
        apple_arm.AppleReaderUnavailable(started)))
    out = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out):
        assert cli._ingest_converted(args, src, convert.spec_for(src), None) == 2
    assert f"{started}. {convert.image_remedy(extras)}" in out.getvalue(), out.getvalue()
    if macos and "convert-ml" in extras:
        assert "--docling-models" in _row(listing, "image")
    else:
        (why,) = [why for exts, why in refused if ".png" in exts]
        assert "image" not in accepted and convert.image_remedy(extras) in why
