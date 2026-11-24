"""Any source document → markdown text. The arm that widens ingestion past PDF.

The restriction to PDF was ours, not Docling's: `docling_arm._converter` names
`format_options={InputFormat.PDF: ...}` and Docling refuses everything it was not handed
options for. This module names the rest — and, more importantly, names what it will NOT
accept and why, because a format that imports but cannot convert is worse than one that is
refused: the refusal is a sentence an operator can act on, the empty conversion is a document
that reads as ingested while holding nothing.

**Everything here converts to MARKDOWN and hands off to `markdown.ingest.ingest_markdown`.**
It does not build blocks itself. That is the whole design: `ingest_markdown` is where the class
policy, the spine contract and the A18 coverage gate live, and a second block-builder would be a
second place for those three to be forgotten — the same argument the chunker's PDF/markdown
sharing already carries. A DOCX therefore passes gates the PDF arm does not even run.

## Two gates that are this module's own

A18 measures the markdown against the chunks. It cannot see content that Docling lost on the way
INTO the markdown, because the loss shrinks both sides — the identical blind spot A18 was built to
close one layer down. So:

1. **Non-empty.** A conversion that yields no word at all is refused. This is not hypothetical:
   `blank.png` converts with `status=SUCCESS` and a zero-character body, and every downstream
   gate passes it (coverage over an empty document is 1.0 by construction).
2. **Raw-text coverage.** Where a cheap independent reading of the source exists (the OOXML text
   nodes, the HTML text nodes, the plain bytes of a text-shaped format), the source's word tokens
   must survive into Docling's markdown. Where no such reading exists — an image, an EPUB, a
   LaTeX source — coverage is recorded as `null` with the probe named `unavailable`, NEVER as a
   passing number. Absent evidence is not a clean result; that distinction is `refresh.frozen`'s
   and it is the same one here. A probe that EXISTS and could not run is a third state and is
   named as one (`ooxml-x-failed`, with the exception): recording it under the probe's own name
   made a crashed probe indistinguishable from one that measured clean.

## Doc class

**Absence, for every format this module accepts.** A file extension is evidence about the
CONTAINER, not about the document: `.docx` does not mean "a published edition that will not
change", and defaulting it to one would be the `reference-frozen` incident (classes.py) with a
new trigger. Absence resolves to `unclassified` in `classes.apply`, which is retrieved by default
and drawn as undeclared. `--doc-class` remains available for an operator who knows.

PDF is the ONE exception and it is unchanged rather than chosen: `substrate ingest --pdf` has
always required `--doc-class`, the two reference corpora behind it are exactly where
`reference-versioned`'s version gate earns its keep, and relaxing a live gate is a separate
decision nobody has taken.
"""

from __future__ import annotations

import contextlib
import html as _html
import re
import signal
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from substrate.markdown.reader import _TOKEN, content_coverage

# Word tokens of the source that must survive into the converted markdown, where an independent
# reading of the source exists at all. Deliberately looser than A18's 0.99: the probe and Docling
# are two different readings of the same bytes, so exact parity is not the claim — "the body is
# substantially there" is. A conversion that drops a slide, a sheet or a section falls far below
# this; a rendering difference does not.
RAW_COVERAGE_GATE = 0.95

# The probe names written to `run.json["raw_coverage_probe"]` when there is NO measurement, and
# there are three of them rather than one because "this format has no probe" and "this format's
# probe broke" are different facts about the same `null` coverage. Recording the second under the
# probe's own name is how a DOCX whose probe crashed printed a bare `None`, skipped the 0.95 gate
# and exited 0 with no warning: the loud "UNMEASURED, not verified" line keys on `unavailable`, and
# a crashed `ooxml-w` did not say `unavailable`. Suffixes rather than a flat vocabulary so the probe
# that failed is still named — `ooxml-x-failed` sends an operator to openpyxl, `failed` does not.
UNAVAILABLE = "unavailable"     # this format has no independent reading of the source, by design
FAILED = "-failed"              # the probe raised: a missing package, a file that is not a zip
EMPTY = "-empty"                # the probe read the source and found no word token in it


class UnsupportedFormat(RuntimeError):
    """This engine will not attempt the file. An INPUT problem — nothing was converted."""


class ConversionRefused(RuntimeError):
    """The conversion ran and its result is not trustworthy. A GATE — refuse, do not index."""


class ConverterMissing(ImportError):
    """Docling, or a piece of it, will not load: the converter cannot run here. Not the file's
    fault, so an INPUT-side refusal (exit 2) naming the extra, never a gate."""


class Quarantined(RuntimeError):
    """The worker hung, crashed or answered garbage on this file twice, or did so on an earlier
    ingest of the same bytes with the same converter. A GATE (exit 3) naming the way out."""


class FileChanged(RuntimeError):
    """The file's bytes changed, or it went away, between its checksum and the end of its
    conversion and probe, so the checksum and the conversion may be of different bytes. A GATE
    (exit 3); never retried, never quarantined."""


class ConverterUnsupported(RuntimeError):
    """The installed docling does not name this format: the format table and the converter
    disagree, which an upgrade can cause. Neither the file nor a gate: exit 2."""


@dataclass(frozen=True)
class FormatSpec:
    """One accepted input format: which arm reads it, what it costs, what verifies it."""

    token: str                      # stable name, written to run.json["source_format"]
    extensions: tuple[str, ...]
    arm: str                        # "pdf" | "markdown" | "text" | "docling"
    docling_format: str | None      # InputFormat value; None for the two native arms
    uses_models: bool               # docling's models when --docling-models names them; runs without
    probe: str | None               # raw-text probe id, or None when none exists
    note: str

    @property
    def doc_class_default(self) -> str:
        return "declared (--doc-class required)" if self.arm == "pdf" else "absence → unclassified"

    @property
    def docling_extra(self) -> str | None:
        """The engine extra docling needs to read this format, or None where docling never reads it.

        `convert-ml` for a format docling reads with its models (a PDF or an image, when a models
        folder is named), `convert` for docling's other readers. Without a models folder a PDF or an
        image needs no extra at all: pdfium reads the text layer, and Apple's recognizer
        (`bin/extract-apple`, a binary the bundle build compiles) reads a scan.
        """
        if self.arm in ("markdown", "text"):
            return None
        return "convert-ml" if self.uses_models else "convert"


def needs_extra(spec: FormatSpec, error: Exception) -> str:
    """The refusal for a docling that will not import: the format, the extra, and how to install it.

    It says the extra could not load, not that it is absent: a `convert` install whose reader breaks
    on import is the same refusal, and `uv sync` repairs it as it installs one. `--inexact`, because
    a plain `uv sync` removes every extra it is not named, and would take the other one with it.
    """
    extra = spec.docling_extra
    return (f"{spec.token} needs the engine's [{extra}] extra, and it could not be loaded here "
            f"— {str(error).rstrip('.')}. {install_hint(extra)}")


def install_hint(extra: str) -> str:
    """How to install an extra, said the same way wherever the engine names one."""
    return f"From a clone, `uv sync --inexact --extra {extra}` installs it."


# The distributions each extra installs that its readers need, by name. `formats` reads them as
# installed distributions, without importing anything: an import is what ingest does, and a listing
# must not cost a torch load. `tests/test_dependency_tiers.py` holds each to its tier in the lock.
# An install without OpenCV reads as no `convert-ml`, on purpose: TableFormer, which every models
# run loads, needs it.
EXTRA_DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "convert": ("docling-slim", "docling-core", "beautifulsoup4", "mail-parser", "python-oxmsg",
                "pylatexenc", "openpyxl", "python-docx", "python-pptx", "marko"),
    "convert-ml": ("docling-parse", "docling-ibm-models", "torch", "torchvision", "accelerate",
                   "huggingface-hub", "scipy", "rtree", "opencv-python-headless")
                  + (("ocrmac",) if sys.platform == "darwin" else ()),
}


def installed_extras() -> frozenset[str]:
    """The extras whose every distribution is installed. `convert-ml` holds all of `convert`."""
    from importlib.metadata import PackageNotFoundError, distribution

    def present(name: str) -> bool:
        try:
            distribution(name)
        except PackageNotFoundError:
            return False
        return True

    have = {extra for extra, names in EXTRA_DISTRIBUTIONS.items() if all(map(present, names))}
    if "convert" not in have:
        have.discard("convert-ml")
    return frozenset(have)


def engine_import_fault(error: ImportError) -> bool:
    """Whether the import that failed is the engine's own: a bug to show, not an extra to name."""
    return (error.name or "").partition(".")[0] == "substrate"


# The PDF and image notes name their readers, and `formats` leaves out the ones an engine lacks
# (`_pdf_note`, `_image_note`). The static notes are the full state, built from the same pieces.
_PDF_TEXT = "Its own text layer"
_PDF_APPLE = ", with Apple's recognizer for pages that are only an image"
_PDF_DOCLING = "; docling's layout and table models when --docling-models names them"
_PDF_TAIL = ". Furniture adjudication, inferred heading levels."
_IMAGE_APPLE = "Apple's on-device document recognizer"
_IMAGE_DOCLING = ("docling with Apple Vision OCR and table structure when --docling-models names its "
                  "models")
_IMAGE_TAIL = (". The pixels are the only reading there is, so there is no independent probe — the "
               "non-empty gate is the whole guarantee.")

PDF = FormatSpec("pdf", (".pdf",), "pdf", "pdf", True, None,
                 _PDF_TEXT + _PDF_APPLE + _PDF_DOCLING + _PDF_TAIL)
MARKDOWN = FormatSpec(
    "markdown", (".md", ".markdown"), "markdown", None, False, None,
    "The vault format. Read by the markdown reader, not Docling — frontmatter, spine and doc_id "
    "round-trip, and a project vault ingests with nothing installed.",
)
TEXT = FormatSpec(
    "text", (".txt", ".text"), "text", None, False, "text",
    "Read by the markdown reader, which is a superset: every line routes into a block. A `#` or "
    "`-` line is read as structure it may not have meant — content is preserved (A18), hierarchy "
    "may be over-read.",
)

IMAGE = FormatSpec("image", (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"), "docling",
                   "image", True, None, f"{_IMAGE_APPLE}; {_IMAGE_DOCLING}{_IMAGE_TAIL}")

_CONVERTED = (
    FormatSpec("docx", (".docx", ".docm", ".dotx", ".dotm"), "docling", "docx", False, "ooxml-w",
               "Word (Office Open XML)."),
    FormatSpec("pptx", (".pptx", ".pptm", ".potx", ".ppsx", ".potm", ".ppsm"), "docling", "pptx",
               False, "ooxml-p", "PowerPoint; one page per slide."),
    FormatSpec("xlsx", (".xlsx", ".xlsm"), "docling", "xlsx", False, "ooxml-x",
               "Excel; each sheet renders as a markdown table."),
    FormatSpec("html", (".html", ".htm", ".xhtml"), "docling", "html", False, "html",
               "HTML; scripts and styles are not content and are excluded from the probe too."),
    FormatSpec("csv", (".csv",), "docling", "csv", False, "text",
               "Renders as one markdown table."),
    FormatSpec("vtt", (".vtt",), "docling", "vtt", False, "vtt",
               "WebVTT captions — cue text only; timings are not content."),
    FormatSpec("asciidoc", (".adoc", ".asciidoc", ".asc"), "docling", "asciidoc", False, None,
               "AsciiDoc. No probe: its markup and its content share a token space."),
    FormatSpec("latex", (".tex", ".latex"), "docling", "latex", False, None,
               "LaTeX source. No probe, for the same reason as AsciiDoc — `\\section` is markup, "
               "and a probe that counted it would report loss on a faithful conversion."),
    FormatSpec("email", (".eml",), "docling", "email", False, None,
               "RFC-822 message. No probe: Docling rewrites the headers it keeps (dates are "
               "normalised) and drops the ones it does not."),
    FormatSpec("epub", (".epub",), "docling", "epub", False, None,
               "EPUB. No probe: chapter selection is the backend's decision, so a whole-archive "
               "reading would report loss on a correct conversion."),
    IMAGE,
)

ACCEPTED: tuple[FormatSpec, ...] = (PDF, MARKDOWN, TEXT) + _CONVERTED

# Formats Docling NAMES but this venv cannot honour, each with the specific missing piece. Listed
# rather than left to fall through as "unknown", because "we cannot read .doc" and "we have never
# heard of .doc" are different facts and only one of them tells the operator what to do next.
REFUSED: dict[str, str] = {
    **dict.fromkeys(
        (".doc", ".dot", ".ppt", ".pot", ".pps", ".xls", ".xlt"),
        "legacy Office binary. Docling converts it by shelling out to LibreOffice, which is not on "
        "PATH here. Re-save as .docx / .pptx / .xlsx and ingest that.",
    ),
    **dict.fromkeys(
        (".odt", ".ott", ".ods", ".ots", ".odp", ".otp"),
        "OpenDocument. Docling's backend imports, then raises at open time — it needs the `odfdo` "
        "package, which is not installed in this venv.",
    ),
    **dict.fromkeys(
        (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"),
        "audio. Docling's ASR pipeline needs a speech-recognition model (whisper / "
        "faster-whisper) and ffmpeg; none of them are installed here.",
    ),
    **dict.fromkeys(
        (".mp4", ".avi", ".mov", ".mkv", ".webm"),
        "video. Same missing ASR stack as audio, plus frame extraction.",
    ),
    **dict.fromkeys(
        (".xml", ".nxml"),
        "ambiguous: Docling maps .xml to three unrelated backends (JATS, XBRL, USPTO patents) and "
        "picks between them by sniffing content. None is verified in this engine, so a wrong pick "
        "would produce a plausible document from the wrong reader.",
    ),
    ".json": "Docling's .json is its OWN serialization of an already-converted document, not a "
             "general JSON reader. An arbitrary .json is not a document.",
    ".tar.gz": "METS/Google-Books archive. Not verified in this engine.",
    ".dclx": "Docling archive format. Not verified in this engine.",
    ".boxnote": "Box Notes. Not verified in this engine.",
}

_BY_EXT: dict[str, FormatSpec] = {e: s for s in ACCEPTED for e in s.extensions}


def spec_for(path: Path) -> FormatSpec:
    """The format this engine will read `path` as, from its extension. Raises UnsupportedFormat.

    Extension, not content sniffing, and the reason is that the refusal has to be legible: Docling
    guesses format from magic bytes and falls back to an extension map, which is right for a
    library and wrong for a gate — "we will not read .doc without LibreOffice" is a sentence, and
    "the backend could not parse the input" is not. Docling still sniffs afterwards, and the
    converter is built with `allowed_formats` restricted to the ONE format decided here, so a file
    whose bytes disagree with its extension is refused rather than silently read as something else.
    """
    name = path.name.lower()
    if name.endswith(".tar.gz"):
        raise UnsupportedFormat(f"{path.name}: {REFUSED['.tar.gz']}")
    ext = path.suffix.lower()
    spec = _BY_EXT.get(ext)
    if spec is not None:
        return spec
    if ext in REFUSED:
        raise UnsupportedFormat(f"{path.name}: {REFUSED[ext]}")
    raise UnsupportedFormat(
        f"{path.name}: unknown extension {ext or '(none)'}. Accepted: "
        f"{' '.join(sorted(_BY_EXT))}. Use --md to read it as markdown anyway."
    )


@dataclass
class Conversion:
    """Docling's markdown for one source document, plus the evidence that it is trustworthy."""

    markdown: str
    pages: int | None
    extractor: str
    stats: dict = field(default_factory=dict)
    arm: str = ""                   # run.json["extractor_arm"]: which reader produced the markdown
    # The checksum of the bytes that were converted and probed (`unchanged` held throughout).
    source_sha256: str = ""


# ---------------------------------------------------------------------------
# Raw-text probes — an independent reading of the source, used only to measure.
#
# Independent of DOCLING, which is the axis that matters: the failure being hunted is Docling
# dropping a slide, a sheet or a section, and a reading that does not go through Docling sees that.
# Two share a library with Docling's reader, `ooxml-x` (openpyxl) and `html` (BeautifulSoup), so
# they catch text read and then not rendered, and not the library misreading the file.
# The OOXML probes take the text NODES only (`w:t`, `a:t`, shared strings) rather than stripping
# all tags, because a field instruction (`HYPERLINK "…"`) and a tracked deletion (`w:delText`) are
# markup that Docling correctly omits — counting them would report loss on a faithful conversion.
# ---------------------------------------------------------------------------

_XML_TEXT = {"ooxml-w": r"<w:t\b[^>]*>(.*?)</w:t>", "ooxml-p": r"<a:t\b[^>]*>(.*?)</a:t>"}


def _html_text(src: Path) -> str:
    """A page's text, parsed as Docling's HTML reader parses it, less what is not content.

    THE PROBE READS THE SOURCE. From Docling it takes two things only: the parser call
    (`BeautifulSoup(raw, "html.parser")` on the file's bytes, in `docling/backend/html_backend.py`)
    and the removal of what is never content (script, noscript, style, a template that declares no
    shadow root, an element with a `hidden` attribute, and the string types `_html_reading` leaves
    out). Nothing Docling decides after that is copied: not `soup.body or soup`, not furniture
    before the first heading, not `_is_invisible_tag`, not its repair of the tree. So wherever probe
    and reader differ on anything but `<title>` text, the probe holds MORE, and the difference is a
    refusal, never a pass; copying one more of Docling's choices to stop a refusal would hide the
    loss it reports.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(src.read_bytes(), "html.parser")
    for tag in soup(["script", "noscript", "style", "template"]):
        if tag.decomposed:
            continue
        if tag.name == "template" and str(tag.get("shadowrootmode", "")).lower() in {"open", "closed"}:
            continue
        tag.decompose()
    for tag in soup(hidden=True):
        tag.decompose()
    return _html_reading(soup)


def _html_reading(soup) -> str:
    """The text of the whole soup but every `<title>`, the probe's one exclusion.

    A browser shows a title as page text nowhere, and the markdown does not carry the document's:
    counting it asked for a page's H1 twice, and a correct page was refused at 0.9231. The exposure
    is that title text is never checked, so its loss goes unseen; an unclosed `<title>` takes the
    rest of the page with it, for the probe as for Docling and a browser. Comments, CDATA, a
    doctype, a declaration or a processing instruction are not text, as Docling's walk does not
    take them; the rest are joined with a space.
    """
    from bs4.element import NavigableString, PreformattedString

    for title in soup("title"):
        title.decompose()
    return " ".join(str(s) for s in soup.descendants
                    if isinstance(s, NavigableString) and not isinstance(s, PreformattedString))


# A cue's timing line is any line holding `-->`, which is WebVTT's own test, so it is a substring
# check in `_vtt_text` rather than a branch here.
_VTT_SKIP = re.compile(r"^(WEBVTT|NOTE\b|STYLE$|REGION$|\d+$)")


@dataclass(frozen=True)
class RawProbe:
    """One probe's reading of the source, or the NAMED reason there is none.

    `text is None` is the whole point of the type: it is three different facts and they must not
    arrive as one. `label` is what `run.json["raw_coverage_probe"]` records and what the CLI keys
    its UNMEASURED warning on; `error` carries the probe's own failure so the operator is told
    which piece is missing rather than left with a null.
    """

    text: str | None
    label: str
    error: str = ""

    @property
    def measured(self) -> bool:
        return self.text is not None


def _zip_text(src: Path, members: str, pattern: str) -> str:
    rx = re.compile(pattern, re.S)
    out: list[str] = []
    member_rx = re.compile(members)
    with zipfile.ZipFile(src) as z:
        for name in sorted(z.namelist()):
            if not member_rx.fullmatch(name):
                continue
            xml = z.read(name).decode("utf-8", errors="replace")
            out.extend(_html.unescape(m) for m in rx.findall(xml))
    return "\n".join(out)


def _xlsx_text(src: Path) -> str:
    """Cell VALUES via openpyxl.

    Stated rather than hidden: this cannot catch openpyxl misreading the workbook. It catches what
    it is here for — Docling reading the workbook and then not RENDERING a sheet or a row into the
    markdown, which is the loss an operator would never see.

    The import is lazy and openpyxl is not declared here — it arrives transitively through docling —
    so a venv that has one without the other turns EVERY `.xlsx` into a non-measurement. That is now
    recorded as `ooxml-x-failed` with the ImportError attached (see `raw_text`), because the failure
    mode this probe exists to catch is precisely a sheet quietly not appearing.
    """
    import openpyxl

    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    try:
        return "\n".join(
            " ".join("" if v is None else str(v) for v in row)
            for ws in wb.worksheets
            for row in ws.iter_rows(values_only=True)
        )
    finally:
        wb.close()


def _vtt_text(src: Path) -> str:
    return "\n".join(
        ln for ln in src.read_text("utf-8", errors="replace").splitlines()
        if ln.strip() and "-->" not in ln and not _VTT_SKIP.match(ln.strip())
    )


def _read_probe(src: Path, spec: FormatSpec) -> str | None:
    """The raw dispatch. None only for a probe id with no branch here — a table drift, not a read."""
    if spec.probe in _XML_TEXT:
        members = r"word/document\d*\.xml" if spec.probe == "ooxml-w" else r"ppt/slides/slide\d+\.xml"
        return _zip_text(src, members, _XML_TEXT[spec.probe])
    if spec.probe == "ooxml-x":
        return _xlsx_text(src)
    if spec.probe == "html":
        return _html_text(src)
    if spec.probe == "vtt":
        return _vtt_text(src)
    if spec.probe == "text":
        return src.read_text("utf-8", errors="replace")
    return None


def raw_text(src: Path, spec: FormatSpec) -> RawProbe:
    """An independent reading of the source, or the NAMED reason there is none.

    "No measurement" is carried as `raw_coverage: null` and must never be rendered as 1.0: a format
    nobody measured and a format measured clean are different states, and collapsing them is how a
    green gate comes to mean nothing.

    The three ways to have no measurement are kept apart for the same reason one layer down. A probe
    that RAISED used to return the same bare None as a format that has no probe, while `run.json`
    still named the probe — so the CLI's UNMEASURED warning, which keys on `unavailable`, stayed
    silent and a DOCX/PPTX/XLSX/HTML skipped the 0.95 gate invisibly. `_xlsx_text` imports openpyxl
    lazily and is the sharpest case: on a venv without it EVERY `.xlsx` took that path, so the
    sheet-drop gate was off with no distinguishable signal.

    A broken probe still does not get to REFUSE the file — it is an instrument, not a gate, and the
    conversion's own success or failure is the verdict. It gets to be LOUD about measuring nothing.
    """
    if spec.probe is None:
        return RawProbe(None, UNAVAILABLE)
    try:
        text = _read_probe(src, spec)
    except Exception as e:  # noqa: BLE001 — any probe fault is one named non-measurement
        return RawProbe(None, f"{spec.probe}{FAILED}", f"{type(e).__name__}: {e}")
    if text is None:
        return RawProbe(None, f"{spec.probe}{FAILED}", "no probe implementation for this id")
    if not _TOKEN.findall(text):
        # The probe read the source and found no word in it — a `.docx` whose body text lives
        # somewhere this probe does not look. Coverage over an empty probe reading is 1.0 by
        # construction, which is the same false clean the non-empty gate exists to refuse.
        return RawProbe(None, f"{spec.probe}{EMPTY}")
    return RawProbe(text, spec.probe)


def verify_conversion(
    src: Path, spec: FormatSpec, md: str
) -> tuple[float | None, list[str], RawProbe]:
    """The two gates this module owns, over an already-converted body. Raises ConversionRefused.

    Returns `(raw_coverage, missing_tokens, probe)`; coverage is None when nothing measured it, and
    `probe.label`/`probe.error` are then the record of WHY — carried out rather than collapsed,
    because the caller is what turns an unmeasured conversion into a warning.

    Split out of `to_markdown` so it is reachable WITHOUT Docling. These two decisions are the
    reason this module exists, and a gate exercised only through a five-second torch import and a
    seven-second OCR pass is a gate that gets tested once and then never again.
    """
    if not _TOKEN.findall(md):
        raise ConversionRefused(
            f"{spec.token}: {src.name} converted to {len(md)} characters with no word in them. "
            "Docling reported success, so nothing downstream would have refused this — an empty "
            "document passes every coverage gate by construction. Refusing here instead."
            + (" An image with no legible text is the usual cause." if spec.token == "image" else "")
        )

    probe = raw_text(src, spec)
    if not probe.measured:
        return None, [], probe

    cov, missing = content_coverage(probe.text, [md])
    if cov < RAW_COVERAGE_GATE:
        raise ConversionRefused(
            f"{spec.token}: {src.name} lost content in conversion — {cov} of the source's "
            f"word tokens survive into the markdown (gate {RAW_COVERAGE_GATE}), measured "
            f"against the {spec.probe} probe. Missing: {missing}"
        )
    return cov, missing, probe


def _apple_markdown(src: Path, spec: FormatSpec, *, log=print) -> Conversion:
    """An image read by Apple's on-device recognizer, with no models. Raises ConversionRefused.

    `AppleReaderUnavailable` passes through rather than becoming a refusal: a reader missing from the
    engine says nothing about the file, the same split the CLI already draws for a docling that will
    not import.
    """
    from substrate.extract import apple_arm

    t0 = time.monotonic()
    log(f"converting: {src.name}  format={spec.token}  (Apple's document recognizer)")
    try:
        data = apple_arm.read(src)
    except apple_arm.AppleReaderUnavailable:
        raise
    except apple_arm.AppleReaderError as e:
        raise ConversionRefused(
            f"{spec.token}: Apple's document recognizer could not read {src.name} — {e}"
        ) from e
    md = apple_arm.to_markdown(data)
    elapsed = round(time.monotonic() - t0, 1)
    cov, missing, probe = verify_conversion(src, spec, md)

    stats = {
        "converted_chars": len(md),
        "converted_seconds": elapsed,
        "reader": "apple-vision",
        "ocr": True,
        "raw_coverage": cov,
        "raw_coverage_probe": probe.label,
        "raw_coverage_missing": missing,
    }
    if probe.error:
        stats["raw_coverage_probe_error"] = probe.error
    return Conversion(markdown=md, pages=len(data["pages"]) or None,
                      extractor=apple_arm.EXTRACTOR, stats=stats, arm=f"apple-{spec.token}")


def changed(src: Path) -> FileChanged:
    return FileChanged(
        f"{src.name} changed or went away while it was being ingested, so its checksum and its "
        "conversion may be of different bytes. Ingest it again once nothing is writing to it.")


def unchanged(src: Path, checksum: str) -> bool:
    """Whether `src` still holds the bytes `checksum` was taken of, hashed again. The bytes alone
    decide: a chmod, an extended attribute or a touch is no change, and a file that is gone or
    cannot be read is one, as is a path that is no longer a regular file (a FIFO would block the
    read, a device never end it), which is found without reading it. A change made and undone
    between the two hashes is not seen: someone running as this user can arrange that, and so can
    a sync client replaying another person's edit and its undo."""
    import hashlib
    import os
    import stat

    try:
        fd = os.open(src, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        digest = hashlib.sha256()
        while block := os.read(fd, 1 << 20):
            digest.update(block)
        return digest.hexdigest() == checksum
    except OSError:
        return False
    finally:
        os.close(fd)


def printable(text: str) -> str:
    """Text a converter wrote, made safe for a terminal: a document's own words can reach an error
    message, and an escape sequence among them would act on the terminal it is printed to. Control
    characters go, and so do the invisible ones that reorder or hide text (bidi controls, zero
    widths, line and paragraph separators); each line after the first is indented, so none of it
    can pass for one of the engine's own `FATAL` or `WARNING` lines."""
    import unicodedata

    kept = "".join(c for c in text if c in "\n\t" or not (
        ord(c) < 32 or 127 <= ord(c) < 160 or unicodedata.category(c) in ("Cf", "Zl", "Zp")))
    return kept.replace("\n", "\n    ")


def docling_version() -> str:
    """The installed docling's version, read from the `docling-slim` distribution, never imported.
    The engine's, and so the one a conversion is recorded under: a worker must report the same.

    `docling` is a meta-package of the same version that the engine's extras do not install, so
    asking for it raised `PackageNotFoundError` on an engine that converts perfectly well.
    """
    from importlib.metadata import version

    return version("docling-slim")


def to_markdown(src: Path, spec: FormatSpec, *, models: Path | None = None, log=print,
                quarantine=None, retry_quarantined: bool = False) -> Conversion:
    """Convert one non-PDF, non-markdown source to markdown. Raises ConversionRefused.

    `models` is the operator's checked docling models folder (`paths.models_folder`), or None. A format
    that uses models is read by Apple's on-device recognizer without one and by docling with one; every
    other format ignores it, because a .docx needs no weights.

    Docling runs in the conversion worker (`worker_client`), never in this process; the gates run
    here. `quarantine` is a callable returning the blob store root that holds quarantine records,
    or None for none (`conversion_quarantine`). Also raises ConverterMissing, ConverterUnsupported
    and `worker_client.WorkerUnavailable` (exit 2), Quarantined and FileChanged (exit 3), and
    `worker_client.Interrupted` when Ctrl-C, SIGTERM or SIGHUP stops the conversion.

    Every refusal names the format, the file and the specific reason. Nothing partial is returned:
    the caller either gets markdown that cleared both gates, or an exception.
    """
    from substrate.extract.base import sha256_file

    if spec.arm != "docling":
        raise ValueError(f"{spec.token} is read by the {spec.arm} arm, not converted")
    # Read through one resolved path: the checksum is taken once, here, and the same bytes must be
    # there when the conversion and its probe are done, or a sync client rewriting the file
    # mid-ingest would give one file's verdict to another's bytes.
    real = src.resolve()
    checksum = sha256_file(real)
    size = real.stat().st_size
    if spec.uses_models and models is None:
        try:
            conv = _apple_markdown(real, spec, log=log)
        except ConversionRefused:
            if not unchanged(real, checksum):
                raise changed(src) from None
            raise
        if not unchanged(real, checksum):
            raise changed(src)
        conv.source_sha256 = checksum
        return conv
    need_extra(spec)

    from substrate.extract import worker_client

    log(f"converting: {src.name}  format={spec.token}"
        + ("  (OCR — first run loads models)" if spec.uses_models else ""))
    budget = worker_client.convert_deadline(size)

    def work(worker):
        return worker.request({"type": "convert", "format": spec.token, "path": str(real)}, budget,
                              ("converted", "refused", "needs_extra", "unsupported"))

    def accept(reply, docling):
        md = reply["markdown"]
        try:
            gate = verify_conversion(real, spec, md)
        except ConversionRefused as e:
            gate = e
        if not unchanged(real, checksum):
            raise changed(src)
        if isinstance(gate, ConversionRefused):
            raise gate
        cov, missing, probe = gate
        stats = {
            "converted_chars": len(md),
            "converted_seconds": reply["seconds"],
            "docling_status": reply["status"],
            "reader": "docling",
            "ocr": spec.uses_models,
            # `null` when nothing could measure it. See raw_text: absent evidence, not a pass.
            "raw_coverage": cov,
            # The probe AS IT RAN, not as the table declares it: `ooxml-w` only when it produced a
            # reading, `unavailable`/`…-failed`/`…-empty` otherwise. Naming the declared probe
            # beside a null coverage is a run.json that says a gate ran when it did not.
            "raw_coverage_probe": probe.label,
            "raw_coverage_missing": missing,
        }
        # Written only when the probe actually broke — absence means nothing to explain, the same
        # way an undeclared class is absent rather than stored as a value that reads like one.
        if probe.error:
            stats["raw_coverage_probe_error"] = probe.error
        return Conversion(markdown=md, pages=reply["pages"], extractor=f"docling {docling}",
                          stats=stats, arm=f"docling-{spec.token}", source_sha256=checksum)

    return through_worker(src, spec, real=real, checksum=checksum, models=models, budget=budget,
                          work=work, accept=accept, quarantine=quarantine,
                          retry_quarantined=retry_quarantined, log=log)


def need_extra(spec: FormatSpec) -> None:
    """Refuse before any worker starts when the extra docling needs for `spec` is not installed.
    Raises ConverterMissing. The worker refuses a half-installed extra the same way."""
    extra = spec.docling_extra
    if extra is not None and extra not in installed_extras():
        raise ConverterMissing("it is not installed")


_SAID = {"hang": "hung", "crash": "crashed", "protocol": "answered with frames it could not trust"}


def through_worker(src: Path, spec: FormatSpec, *, real: Path, checksum: str,
                   models: Path | None, budget: int, work, accept, quarantine,
                   retry_quarantined: bool, log):
    """Run `work(worker)` in a fresh worker until `conversion_quarantine.quarantine_decision`
    stops: once, and once more after a fault. `work` returns the worker's reply; for a
    `converted` or `items` reply, `accept(reply, docling_version)` runs the gates here, may raise
    FileChanged or ConversionRefused, and returns what this returns. Otherwise this raises the
    verdict. A fault or a refusal on a file whose bytes changed is FileChanged. Ctrl-C, SIGTERM
    and SIGHUP meanwhile raise `worker_client.Interrupted` once the worker's group is dead."""
    from substrate.extract import conversion_quarantine as q
    from substrate.extract import convert_frames as frames
    from substrate.extract import worker_client
    from substrate.paths import model_env

    docling = docling_version()
    store = record = None
    unusable = "no blob store is in use for this ingest"
    if quarantine is not None:
        try:
            store = quarantine()
        except Exception as e:  # noqa: BLE001 — no store is no record, never a refusal
            unusable = printable(str(e)).rstrip(".")
        else:
            record = q.read(store, checksum)
    state = q.state(record, fmt=spec.token, models=models is not None, docling=docling)
    outcomes: list[str] = []
    faults: list[worker_client.WorkerFault] = []
    result = None
    try:
        with worker_client.interruptible():
            while True:
                decision = q.quarantine_decision(outcomes, state, retry_quarantined)
                if decision.record == "delete" and store is not None:
                    # A record that cannot be removed only means the next ingest asks again.
                    with contextlib.suppress(OSError):
                        q.remove(store, checksum)
                note = ""
                if decision.record == "write":
                    note = _record(q, store, unusable, checksum, spec, models, docling, faults)
                if decision.verdict == "blocked":
                    raise Quarantined(
                        f"{src.name} is quarantined: the converter "
                        f"{', then '.join(_SAID[f] for f in record['faults'])} on it "
                        f"({record['at']}). `substrate ingest --retry-quarantined` tries it again.")
                if decision.verdict == "quarantined":
                    tail = printable(faults[-1].stderr).strip()[-2000:]
                    raise Quarantined(
                        f"{src.name} is quarantined: the converter {printable(faults[0].said)}, "
                        f"and in a new worker it {printable(faults[-1].said)}. `substrate ingest "
                        f"--retry-quarantined` tries it again.{note}"
                        + (f"\nThe worker's last words:\n    {tail}" if tail else ""))
                if decision.verdict != "attempt":
                    return _verdict(decision.verdict, src, spec, result)
                if faults:
                    log(f"WARNING (converter): the worker converting {src.name} "
                        f"{printable(faults[-1].said)}; it was stopped, and a new worker tries "
                        "once more.")
                try:
                    with worker_client.Worker(budget, model_env(models) if models else {},
                                              loads_models=spec.uses_models) as w:
                        hello = w.start()
                        if hello["docling"] != docling:
                            raise worker_client.WorkerUnavailable(
                                f"the conversion worker runs docling "
                                f"{printable(str(hello['docling']))}, not the engine's {docling}")
                        reply = work(w)
                        w.finish()
                except frames.FrameError as e:
                    # A request this engine cannot put into a frame (a file name that is not
                    # UTF-8): this file's answer, every time, and never the worker's fault.
                    result = ConversionRefused(f"{spec.token}: {src.name} cannot be named to the "
                                               f"converter ({e})")
                    outcomes.append("refused")
                    continue
                except worker_client.WorkerFault as fault:
                    # A change to the file can be what made the converter fail: then it is the
                    # change that is reported, and nothing is recorded against these bytes.
                    if not unchanged(real, checksum):
                        result = changed(src)
                        outcomes.append("changed")
                        continue
                    faults.append(fault)
                    outcomes.append(fault.kind)
                    continue
                if reply["type"] in ("converted", "items"):
                    try:
                        result = accept(reply, docling)
                    except (FileChanged, ConversionRefused) as e:
                        result = e
                        outcomes.append("changed" if isinstance(e, FileChanged) else "refused")
                        continue
                    outcomes.append("ok")
                    continue
                if reply["type"] == "refused" and not unchanged(real, checksum):
                    # A converter that could not read the file may not have had its bytes, or any.
                    result = changed(src)
                    outcomes.append("changed")
                    continue
                result = reply
                outcomes.append(reply["type"])
    except KeyboardInterrupt:
        raise worker_client.Interrupted(signal.SIGINT) from None


def _record(q, store, unusable, checksum, spec, models, docling, faults) -> str:
    """Write the quarantine record. Returns what to add to the refusal when it could not be."""
    if store is not None:
        try:
            q.write(store, checksum, fmt=spec.token, models=models is not None, docling=docling,
                    faults=[f.kind for f in faults])
            return ""
        except Exception as e:  # noqa: BLE001
            unusable = printable(str(e)).rstrip(".")
    return f" It could not be recorded ({unusable}), so the next ingest tries it again."


def _verdict(verdict: str, src: Path, spec: FormatSpec, result):
    if verdict == "converted":
        return result
    if isinstance(result, Exception):
        raise result
    if verdict == "refused":
        raise ConversionRefused(f"{spec.token}: {src.name}: {printable(result['reason'])}")
    if verdict == "needs_extra":
        raise ConverterMissing(printable(result["error"]))
    if verdict == "unsupported":
        raise ConverterUnsupported(printable(result["error"]))
    raise AssertionError(f"no verdict {verdict!r}")


def _pdf_note(extras: frozenset[str], apple: bool) -> str:
    """The PDF row's note, naming only the readers this engine has: the text layer always.

    Only Apple's recognizer reads a page that is only an image. Docling's PDF reader runs no OCR, so
    the note never offers its models, or the extra they need, for one, and says what they do with it.
    """
    docling = "convert-ml" in extras
    note = _PDF_TEXT + (_PDF_APPLE if apple else "") + (_PDF_DOCLING if docling else "")
    if not apple:
        note += (", which read no word from a page that is only an image. Without them such a page"
                 if docling else ". A page that is only an image")
        note += " is refused: only Apple's recognizer reads one, and it is not in this engine"
    return note + _PDF_TAIL


def _image_note(extras: frozenset[str], apple: bool) -> str | None:
    """The image row's note, naming only the readers this engine has, or None when it has neither."""
    from substrate.extract import apple_arm

    readers = ([_IMAGE_APPLE] if apple else []) + (
        [_IMAGE_DOCLING] if "convert-ml" in extras and apple_arm.on_macos() else [])
    if not readers:
        return None
    note = "; ".join(readers)
    return note[0].upper() + note[1:] + _IMAGE_TAIL


def image_remedy(extras: frozenset[str]) -> str:
    """What reads an image here once Apple's recognizer is missing, as `formats` and ingest say it.

    Docling's image OCR is Apple Vision's, through `ocrmac`, so off macOS nothing does, and the
    extra is named only where installing it gives a working reader.
    """
    from substrate.extract import apple_arm

    if not apple_arm.on_macos():
        return ("Nothing else here reads an image: docling's image OCR is Apple Vision's, which needs "
                "macOS too.")
    docling = "Docling reads an image with Apple Vision's OCR when --docling-models DIR names its models"
    if "convert-ml" in extras:
        return f"{docling}."
    return (f"{docling}, and that needs the engine's [convert-ml] extra, which is not installed here. "
            f"{install_hint('convert-ml')}")


def table() -> list[tuple[str, str, str, str]]:
    """(format, extensions, doc-class default, note) for every format this engine reads, then the
    refusals: a format whose extra is not installed is refused, in the same four columns.

    With both extras and Apple's recognizer, which is the bundle, the table is the one this engine
    printed before the extras existed.
    """
    from substrate.extract import apple_arm

    extras, apple = installed_extras(), apple_arm.available()
    rows, refused = [], {}
    for s in ACCEPTED:
        note = s.note
        if s is IMAGE:
            note = _image_note(extras, apple)
            if note is None:
                why = f"image. {apple_arm.unavailable()} {image_remedy(extras)}"
                refused.setdefault(why, []).extend(s.extensions)
                continue
        elif s is PDF:
            note = _pdf_note(extras, apple)
        elif s.docling_extra and s.docling_extra not in extras:
            why = (f"docling's readers for these are the engine's [{s.docling_extra}] extra, which "
                   f"is not installed here. {install_hint(s.docling_extra)}")
            refused.setdefault(why, []).extend(s.extensions)
            continue
        rows.append((s.token, " ".join(s.extensions), s.doc_class_default, note))
    for ext, why in REFUSED.items():
        refused.setdefault(why, []).append(ext)
    rows += [("REFUSED", " ".join(exts), "—", why) for why, exts in refused.items()]
    return rows
