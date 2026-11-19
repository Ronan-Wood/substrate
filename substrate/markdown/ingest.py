"""One markdown file → an ingested output directory. The single ingestion implementation.

Extracted so the standalone `ingest-md` CLI command and the manifest-composition path share ONE
body — the A18 silent-loss gate, the spine validation, and the run.json shape must not diverge
between "ingest this file" and "ingest every note in the vault". Same reasoning as the chunker
being shared between the PDF and markdown arms: two copies of a silent-loss guard is one copy too
few.

The reader is a pure parser; this is where the ingest DECISIONS live: enforce the class policy,
validate the spine (strict or lenient per caller), fold in `_meta.md`-supplied class/status/domains
for a reference passage that carries none of its own, run the coverage gate, and write the four
artifacts (document.md, blocks.jsonl, chunks.jsonl, run.json) reconcile later reads.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from substrate import classes, spine
from substrate.quarantine import tag
from substrate.chunk.chunker import chunk
from substrate.extract.base import doc_id_for, doc_id_from  # stdlib-only; no Docling/torch pulled in
from substrate.markdown.emit import emit, frontmatter
from substrate.markdown import reader
from substrate.markdown.reader import content_coverage, read_markdown, uncovered_content_blocks
from substrate.models import Document, Kind, SourceOrigin

# A18 aggregate floor. Markdown ingestion must not silently drop source content end-to-end.
MD_COVERAGE_GATE = 0.99


class CoverageError(RuntimeError):
    """A18 — the ingest would drop source content; refuse rather than write a lossy artifact."""


class UnretrievableError(RuntimeError):
    """The ingest produced NO retrievable unit — a document that indexes and answers nothing.

    Distinct from A18, which asks whether content survived into the chunks. This asks whether
    there are any chunks. Measured 2026-08-04: a JPEG of a two-line sign OCR'd into two blocks
    that Docling labelled as headings and nothing else, so the chunker — which correctly forms no
    chunk for an empty-section heading — emitted zero. Every gate was green: A18 source coverage
    1.0 with no dropped blocks (heading text rides into `captured` by design), the class policy
    satisfied, the spine valid. The result was a document row with `passages 0 · outlines 0`.

    Nothing was lost — the text is in document.md and blocks.jsonl — and that is exactly why no
    loss gate could see it. What is wrong is retrievability: the document reads as ingested and
    can never be returned.

    ONLY RAISED WHEN AN `origin` IS SET, i.e. on every handed-in file (converted, text and
    `--md`). The vault path is
    deliberately untouched: a heading-only note is legal markdown, and so is an empty one, which
    takes its file name as its title (issue #55) and composes holding nothing. This would refuse
    them, and the refusal would land inside the unattended refresh pass of `substrate serve`.
    Widening a gate onto the existing corpus is a separate decision with its own blast radius.
    """


@dataclass
class IngestResult:
    doc_id: str
    out_dir: Path
    status: str
    domains: list[str]
    doc_type: str
    confidence: str
    title: str | None
    body_chars: int
    source_coverage: float
    run: dict = field(default_factory=dict)  # passage/outline counts live here under ["chunk"]


class OutputDirError(ValueError):
    """The output folder is not one this ingest can write into safely: a link or a file stands at
    its name, or it belongs to another user. Nothing is written."""


@contextlib.contextmanager
def _output_dir(out_dir: Path):
    """A descriptor on `out_dir` itself, never on what a link there points at, and only when it is
    this user's (`private.open_owned_dir`) and not writable by every user. Compose names every
    ingest folder, so under an index root another user can write, a link or a folder of theirs at
    that name is planted, and following it hands them the note; a folder anyone can write lets
    them swap a file after it is written. Group write is left alone: a umask of 002 with a group
    of one's own is common, and a group shared on purpose is the operator's call."""
    from substrate import private

    fd = private.open_owned_dir(out_dir)
    if not isinstance(fd, int):
        why = fd[1] if fd else "it is not there"
        raise OutputDirError(f"{out_dir} is not a folder of this user's ({why}); nothing was "
                             f"written into it")
    try:
        if os.fstat(fd).st_mode & 0o002:
            raise OutputDirError(f"{out_dir} can be written by every user on this machine; "
                                 f"nothing was written into it")
        yield fd
    finally:
        os.close(fd)


def _write_at(dir_fd: int, name: str, text: str) -> None:
    """`name` in the folder `dir_fd` holds, replaced whole: written to a new file there, then renamed
    over the name. A link or a hard link planted at `name` is replaced, never written through."""
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"


def _meta_path_for(note_path: Path, vault_root: Path | None) -> Path | None:
    """The `_meta.md` a passage inherits from, as the vault layer resolves it
    (`vault.source_meta_path`), or None with no vault root (a caller outside compose)."""
    if vault_root is None:
        return None
    from substrate import vault as _vault

    return _vault.source_meta_path(note_path, vault_root)


# Each engine key's forms (`reader.canonical_forms`), for the spelling check in `_judge_opening`.
_ENGINE_FORMS: dict[str, str] = {f: k for k in spine.EXACT_SPELLING for f in reader.canonical_forms(k)}


def _judge_opening(doc: Document, src: Path, opening: reader.OpeningBlock,
                   defaulted: tuple[str, ...], vault_root: Path | None, *,
                   why: str | None = None) -> None:
    """Refuse a vault note whose opening a default must not stand in for. A passage is judged with
    its `_meta.md`, where its spine is written. Two refusals, both for MISTAKES a person or an
    editor makes; neither is a defence against a key disguised on purpose, since whoever can
    write a note into a vault can leave its class off altogether (docs/security.md):

      * AN ENGINE KEY SPELLED ANOTHER WAY: a key whose `reader.canonical_forms` include one of
        `spine.EXACT_SPELLING`'s keys, written otherwise (`Status`, `doc-type`, `"class"`). It
        is not read, so it would leave `Status: archived` live and `Class: conversation`
        retrieved by default. What it does is that table's, and today every entry refuses.
      * A DEFAULT OVER A BLOCK THE READER DID NOT TAKE WHOLE. Until compose defaulted an absent
        status, "no status" refused every note whose opening block the reader could not take,
        whatever stopped it. So a note that would take a default is refused when it opens with a
        `---`-like line that the reader did not read as frontmatter whole (`reader.OpeningBlock`):
        a byte-order mark before it, a block that never closes, a line YAML would not read as a
        key (`reader.plain_key`). The test is the shape, not the keys a block might hide.

    `why` is the reader's reason the note's own block is body text, when it gave one."""
    blocks = [(src, opening, why)]
    meta_path = _meta_path_for(src, vault_root)
    if meta_path is not None:
        from substrate import vault as _vault

        try:
            blocks.append((meta_path, reader.opening_block(_vault._read_capped(meta_path)), None))
        except _vault.VaultError as e:
            raise tag(ValueError(str(e)), "source_meta_unreadable") from e

    for path, block, _ in blocks:
        for key, forms in block.keys:
            exact = next((_ENGINE_FORMS[f] for f in forms if f in _ENGINE_FORMS), None)
            if exact is None or key == exact:
                continue
            raise tag(spine.SpineError(
                f"{doc.doc_id}: {path.name} writes `{reader._shown(key)}`, which the engine "
                f"does not read as `{exact}`: it reads only `{exact}`, spelled exactly so. "
                f"Write `{exact}:` if this is the note's {exact}. If it is a property of the "
                "vault's own, it cannot share that name with an engine key; refusing rather "
                "than indexing the note as if it had declared none."
            ), "engine_key_spelled_another_way", exact)
    if defaulted:
        for path, block, reason in blocks:
            reason = block.why or reason
            if block.opens and not block.whole:
                raise tag(spine.SpineError(
                    f"{doc.doc_id}: declares no {' and no '.join(defaulted)}, and {path.name} "
                    "opens with a `---` line the engine does not read as frontmatter"
                    + (f" ({reason})" if reason else "") + ". "
                    "Frontmatter is a line of `---` alone as the file's first characters, "
                    "`key: value` lines, and a line of `---` alone. Refusing to index the note "
                    "on defaults over a block that may say otherwise. If the `---` is a "
                    "horizontal rule, start the file with something else."
                ), "opening_block_unread")


def ingest_markdown(
    src: Path,
    out_dir: Path,
    *,
    doc_class: str | None = None,
    require_status: bool = False,
    require_confidence: bool = False,
    override_status: str | None = None,
    override_doc_type: str | None = None,
    override_confidence: str | None = None,
    raw: str | None = None,
    raw_sha256: str | None = None,
    raw_location: str | None = None,
    override_version: str | None = None,
    extra_domains: list[str] | None = None,
    vault: str | None = None,
    vault_root: Path | None = None,
    tier: int | None = None,
    origin: SourceOrigin | None = None,
    before_write: Callable[[Document], None] | None = None,
) -> IngestResult:
    """Ingest one markdown file into `out_dir`. Raises on any refusal; never writes a partial set.

    Raises: ``ValueError`` (unreadable source), ``classes.ClassPolicyError`` (class gate),
    ``spine.SpineError`` (status contract), ``CoverageError`` (A18). The caller maps these to exit
    codes or per-note failures — the point is that a silently-lossy or mislabeled note NEVER
    reaches the index.

    `require_status` refuses a note that declares no `status` or no `doc_type`; the vault write
    path passes it. Without it an absent one takes its default (`active`, `reference`), and on the
    vault path `run.json` names the fields defaulted (`spine.DEFAULTED_FIELDS`). `vault_root`
    is the root of the vault the note is in; compose passes it, so a passage's `_meta.md` is found
    inside that vault and judged with the note.

    `override_status` / `extra_domains` carry a reference source's `_meta.md` context down onto a
    passage file that has no frontmatter of its own: the passage inherits the source's status and
    domain tags. A value the note declares ITSELF always wins — override only fills an absence.

    `origin` is set when `src` is not the real source — a DOCX/PPTX/HTML/image converted to a
    temporary markdown file by `extract.convert`. It is why the converted formats reach the SAME
    three gates as a vault note rather than getting a second ingest body of their own: this
    function is where the class policy, the spine contract and A18 live, and a parallel
    implementation would be a second place to forget one. See models.SourceOrigin.

    `before_write` runs once every gate has passed and before anything is written to `out_dir`.
    `substrate ingest` stores the original there, so a refused file never enters the blob store
    and an accepted one never yields a note whose original failed to store. An exception from it
    propagates, and nothing is written.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except FileExistsError as e:   # a file, or a link to nothing, at the folder's name
        raise OutputDirError(f"{out_dir} is not a folder; nothing was written into it") from e
    t0 = time.monotonic()

    # Strict on the vault path: a value the reader cannot read under a key it reads refuses the
    # note, naming it. A foreign document's spine is cleared below anyway, and the rest of what it
    # declares is optional, so its unreadable values are only left out.
    doc, body_md, rstats = read_markdown(src, doc_class=doc_class, strict=origin is None)
    unread = rstats.pop("frontmatter_unread", None)
    opening = rstats.pop("opening_block")

    # Identity moves to the REAL artifact before anything reads it. Done here rather than after
    # the gates because `classes.apply` enforces `source_sha256` as a required field and
    # `emit.frontmatter` writes all four of these into document.md — a document that named a
    # deleted temp file would pass every gate while being unreproducible.
    if origin is not None:
        doc.source_path = origin.path or doc.source_path
        doc.source_sha256 = origin.sha256 or doc.source_sha256
        # `note_sha256` names the file at `source_path`, so it moves with it. When the path moves
        # to the real artifact, the temp file's checksum would describe a file nobody can re-read.
        if origin.path:
            if not origin.sha256:
                raise ValueError(f"{origin.path}: an origin that names a file must carry its "
                                 "checksum, or the note's freshness could not be checked")
            doc.note_sha256 = origin.sha256
        doc.extractor = origin.extractor or doc.extractor
        doc.extractor_arm = origin.extractor_arm or doc.extractor_arm
        doc.source_pages = origin.pages or doc.source_pages

        # THE SPINE IS NOT A FOREIGN DOCUMENT'S TO DECLARE, and `origin is not None` is exactly the
        # test for "foreign": `compose` — the vault path, where a note's frontmatter IS authoritative
        # because the operator wrote it — calls this function with no origin at all.
        #
        # Without this, a file handed to `ingest` set its own `status`, `doc_type`, `confidence`,
        # `document_class`, `doc_id` and supersession, because `read_markdown` reads all of them from
        # frontmatter and nothing downstream disagreed. A `.txt` or `.md` opening with
        #
        #     ---
        #     status: active
        #     doc_type: decision
        #     confidence: verified
        #     document_class: reference-frozen
        #     domains: [atlas, security]
        #     ---
        #
        # was promoted, composed and registered into `~/.substrate/scopes.toml` — the registry Claude
        # Code and Zed read — and served back to the operator's agents as a settled, verified,
        # frozen decision. That is the persistent prompt-injection and false-decision primitive Doc 3
        # §3 keeps off the loopback port, reached by dropping a file on the Library instead.
        #
        # `doc_id` is the sharper half and was the easier one to miss: `reconcile` keys its
        # delete-then-insert on it, so a crafted id does not add a document, it REPLACES a real one's
        # index row. Derived from the real artifact here rather than left to `origin.doc_id or ...`,
        # because the text arm sets no `doc_id` on its origin and the old expression then fell
        # through to the document's own.
        #
        # Cleared to ABSENCE, not to a safe-looking value: absence is what `classes.apply` and the
        # upsert defaults are built to resolve (→ `unclassified` / `active` / `reference` /
        # `unjudged`), and writing a real value here would be this function inventing a claim on the
        # operator's behalf — the thing it is refusing to let the document do.
        # Three ways to reach the same shape, cheapest first, and none of them the document's own:
        # the converted arm already computed one; a caller that named the real artifact and its
        # fingerprint gets it without touching disk (the artifact may be gone, and re-hashing a file
        # to recover a value already in hand is waste); otherwise `src` IS the real file — the
        # markdown and text arms read it directly — so hashing it is correct rather than a fallback.
        if origin.doc_id:
            doc.doc_id = origin.doc_id
        elif origin.path and origin.sha256:
            doc.doc_id = doc_id_from(Path(origin.path).stem, origin.sha256)
        else:
            doc.doc_id = doc_id_for(Path(src))
        if not doc_class:
            doc.document_class = ""
        doc.status = None
        doc.doc_type = None
        doc.confidence = None
        doc.supersedes = []
        doc.superseded_by = None
        # `domains` too, and it is the one that looks harmless. It is not a claim about how settled
        # the document is — it is what retrieval FILTERS on, so a file that tags itself
        # `[atlas, security]` chooses which questions it surfaces for. That is the delivery half of
        # the same primitive: the other fields decide whether a passage is believed, this one decides
        # whether it is reached. The operator's own tags are unaffected — `extra_domains` merges as a
        # UNION below and carries the `_meta.md` the app writes, which is the channel a domain is
        # supposed to arrive on.
        doc.domains = []

    # _meta.md context fills only what the note itself did not declare. `domains` is a UNION (a
    # passage may add its own tags to the source's); status is filled only when absent, so a note
    # that explicitly marks itself superseded is never silently reactivated by its source's 'active'.
    # Track which spine values came from the SOURCE rather than the note, so a refusal can name the
    # file the operator must actually edit. An error naming the wrong component with total
    # confidence is a failure this project has already paid for (the AppleFM cache_key bug logged
    # against the embedder), and here it is the EXPECTED migration failure: the real source vaults
    # write `confidence: high`, so a book-sized source would otherwise print one refusal per
    # passage, each blaming a passage file that contains no such value.
    inherited: dict[str, Path | None] = {}
    if override_status and doc.status is None:
        doc.status = override_status
        inherited["status"] = _meta_path_for(src, vault_root)
    # doc_type inherits the same way: a reference passage carries no frontmatter, so its source's
    # `_meta.md` doc_type (reference) fills the absence; a note that declares its own job always wins.
    if override_doc_type and doc.doc_type is None:
        doc.doc_type = override_doc_type
        inherited["doc_type"] = _meta_path_for(src, vault_root)
    # confidence inherits identically: a reference source states once, in its `_meta.md`, how
    # settled its passages are; a passage that declares its own always wins.
    if override_confidence and doc.confidence is None:
        doc.confidence = override_confidence
        inherited["confidence"] = _meta_path_for(src, vault_root)
    # §3b: a passage inherits its source's raw pointer. This is the mechanism by which a chunk can
    # name the artifact it ultimately came from — the passage file itself has no idea.
    doc.raw = doc.raw or raw
    doc.raw_sha256 = doc.raw_sha256 or raw_sha256
    doc.raw_location = doc.raw_location or raw_location
    # A reference passage carries no version of its own; a versioned source's `_meta.md` supplies it,
    # so the reference-versioned class gate (which refuses a passage that cannot state its version)
    # is satisfied by the source metadata rather than by scraping the passage body.
    if override_version and doc.version is None:
        doc.version = override_version
    if extra_domains:
        merged = list(doc.domains)
        for d in extra_domains:
            if d not in merged:
                merged.append(d)
        doc.domains = merged
    doc.vault = vault
    doc.tier = tier

    # Last-resort title: the file's name, for a document with no declared title and no heading — a
    # daily note, a quick capture, a list, a clipping, a one-slide deck. In Obsidian the file name
    # IS the note's title, a name its owner chose, which is why a converted file's is trusted too.
    # It is the name of the file read: compose reads a note in place, `ingest` and `amend` stage it
    # under the name it will have, and a `.md` or `.txt` handed in is read where it is. A converted
    # file's comes on its origin, since `src` is then a temporary. Ordered so a declared title and
    # then the document's own heading always win, and `title_source` records which of the three it
    # was. A name with nothing visible in it gives none, and the class gate refuses the note.
    title_source = "declared" if doc.title else ("heading" if classes.extract_title(doc) else None)
    if not doc.title:
        named = ((origin.title if origin is not None else None)
                 or classes.title_from_filename(src.name))
        doc.title = classes.extract_title(doc) or named
        title_source = title_source or ("filename" if doc.title else None)

    meta = classes.apply(doc)
    # Which spine fields this note leaves to the default: read here, after `_meta.md` has filled
    # what it supplies, so an inherited value is not counted as defaulted. Only on the vault path.
    # A foreign document's spine was cleared above and is never its own to declare, so it has no
    # declaration to be missing.
    defaulted = tuple(f for f in spine.DEFAULTED_FIELDS
                      if origin is None and getattr(doc, f) is None)
    # Before the values are judged: a key the engine did not read under its spelling says more
    # about what is wrong than the value check it leads to (`superseded-by:` leaves a superseded
    # note with no link).
    if origin is None:
        _judge_opening(doc, src, opening, defaulted, vault_root, why=unread)
    try:
        status = spine.validate_status(doc, require_present=require_status)
        doc_type = spine.validate_doc_type(doc, require_present=require_status)
        # Confidence rides its OWN flag, not require_status. The two were split while `compose`
        # demanded a status and could not demand a confidence: 530 of 657 indexed notes declared
        # none, and refusing them would have frozen every scope at its next compose. Compose now
        # demands neither, and `notes.py` (someone is writing this note now) passes both. See
        # spine.validate_confidence.
        confidence = spine.validate_confidence(doc, require_present=require_confidence)
    except spine.SpineError as e:
        # Attach the ORIGIN of the offending value. Which field failed is in the message already;
        # what the operator cannot see is that the value came from a `_meta.md` one directory up.
        for field, meta_path in inherited.items():
            if meta_path is not None and f"{field} " in str(e):
                raise tag(spine.SpineError(f"{e} (inherited from {meta_path})"),
                          *getattr(e, "quarantine_kind", ("other", None))) from e
        raise

    # repair=False: markdown carries no glyph artifacts, so PDF-tier hyphen/ligature repair could
    # only mutate clean authored text (measured: 8 words welded on one round-trip).
    body, estats = emit(doc, repair=False)
    chunks, cstats = chunk(doc)

    # Zero retrievable units. Checked BEFORE A18, because A18 would pass — see UnretrievableError:
    # no content was lost, there is simply nothing to return. Origin-scoped so the vault corpus,
    # where a heading-only note is legal, is untouched.
    if origin is not None and not chunks:
        # The REAL artifact's name, not `src` — on the converted arm `src` is the throwaway
        # markdown, and an error naming a temp file the operator never saw sends them after the
        # wrong component. Same reasoning as the `_meta.md` attribution a few lines up.
        name = Path(origin.path).name if origin.path else src.name
        raise UnretrievableError(
            f"{origin.source_format}: {name} produced 0 chunks from {len(doc.blocks)} block(s) "
            f"and {len(body)} characters of body — the document would index and answer nothing. "
            "Every other gate passes: nothing was lost, there is nothing retrievable. Usual cause "
            "is a source whose every block came through as a heading (an OCR'd sign, a title-only "
            "slide), which forms no passage because an empty-section heading has no body."
        )

    # A18 — end-to-end source→chunks coverage against the SOURCE file (A14's chunk/body ratio can't
    # see a dropped line — it leaves the body too). `captured` is everything the source content
    # should survive INTO: chunk text_with_path (a heading rides along via its path), each
    # fence-language, and every heading block's own text (an empty-section heading forms no chunk).
    captured = (
        [c.text_with_path for c in chunks]
        + [b.lang for b in doc.blocks if b.lang]
        + [b.text for b in doc.blocks if b.kind is Kind.HEADING]
    )
    src_cov, src_missing = content_coverage(body_md, captured)
    drops = uncovered_content_blocks(doc.blocks, captured)
    if src_cov < MD_COVERAGE_GATE or drops:
        raise tag(CoverageError(
            f"A18 markdown coverage: aggregate {src_cov} (gate {MD_COVERAGE_GATE}); "
            f"{len(drops)} content block(s) dropped {drops[:8]}; missing tokens {src_missing}"
        ), "coverage_loss")

    # Opened and judged BEFORE `before_write`, which stores the original: a folder refused here
    # leaves no blob behind. Held across the four writes, so each lands in this one folder.
    with _output_dir(out_dir) as dir_fd:
        if before_write is not None:
            before_write(doc)
        files = {
            "document.md": frontmatter(doc, {"version_source": None}) + body,
            "blocks.jsonl": _jsonl([b.to_json() for b in doc.blocks]),
            "chunks.jsonl": _jsonl([c.to_json() for c in chunks]),
        }

        run = {
            "doc_id": doc.doc_id,
            "source": doc.source_path,
            "source_sha256": doc.source_sha256,
            "note_sha256": doc.note_sha256,
            "pages": doc.source_pages,
            # The ORIGINAL format — "markdown" for a note, "docx"/"image"/… for a converted document.
            # `ingest_arm` is the separate fact and the two must not be conflated: checks.py picks the
            # applicable assertion set (A18/A19 on, A1/A1b off) by which ARM ran, not by what the file
            # started as, and a converted DOCX runs the markdown arm end to end. Conflating them would
            # have silently dropped A18 from every converted document the moment `source_format`
            # stopped saying "markdown".
            "source_format": origin.source_format if origin else "markdown",
            "ingest_arm": "markdown",
            "elapsed_s": round(time.monotonic() - t0, 1),
            "class": meta,
            "extract": {
                "extractor": doc.extractor, "extractor_arm": doc.extractor_arm,
                "layout_model": doc.layout_model,
                "source_coverage": src_cov, "source_coverage_missing": src_missing,
                "content_block_drops": drops, "title_source": title_source, **rstats,
                **(origin.stats if origin else {}),
            },
            "emit": estats,
            "chunk": cstats,
            "coverage": round(cstats["sum_chunk_chars"] / max(len(body), 1), 4),
            # Spine block travels with the ingest so reconcile rebuilds status/domains/supersession into
            # the index; it is folded into reconcile's diff key, so editing a note's status re-indexes.
            "spine": {
                "status": status,
                "domains": list(doc.domains),
                "doc_type": doc_type,
                "confidence": confidence,
                "raw": doc.raw,
                "raw_sha256": doc.raw_sha256,
                "raw_location": doc.raw_location,
                "superseded_by": doc.superseded_by,
                # Normalised on the way OUT as well as in. `reconcile` normalises on the way back, so
                # this is not what stops a bad shape reaching the store — it is what stops the ARTIFACT
                # itself being wrong. run.json is read by more than one consumer and is what a person
                # inspects when a note looks wrong; an artifact that disagrees with the note it
                # describes sends the next reader after the wrong component.
                "supersedes": reader.doc_id_list(doc.supersedes),
            },
        }
        # ONLY WHEN THERE IS ONE TO NAME. `reconcile` hashes this block into its diff key, so a key
        # written for every note would re-index every note already indexed, and re-indexing a
        # note drops its vectors. A note that declares its spine writes the block it always did.
        if defaulted:
            run["spine"]["defaulted"] = list(defaulted)
        if vault is not None or tier is not None:
            run["provenance"] = {"vault": vault, "tier": tier}
        files["run.json"] = json.dumps(run, indent=2, ensure_ascii=False)
        for name, text in files.items():
            _write_at(dir_fd, name, text)

    return IngestResult(
        doc_id=doc.doc_id, out_dir=out_dir, status=status, domains=list(doc.domains),
        doc_type=doc_type, confidence=confidence, title=meta.get("title"),
        body_chars=len(body),
        source_coverage=src_cov, run=run,
    )
