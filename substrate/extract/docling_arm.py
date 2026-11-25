"""Docling extractor arm — batched, because whole-document conversion does not scale.

Measured 2026-07-21 on DDIA (673pp): a single convert() call costs 10.80 s/page, while a
100-page batch costs 0.22 s/page at identical work density (10.2 vs 9.8 items/page). That
is a ~46x penalty from state accumulation, not a slow accelerator. So batching is structural
here, not an optimization, and one converter is reused across batches to amortize model load.

Docling runs in the conversion worker (`worker.extract_pdf`), which builds the converter once and
answers each batch with its items. This side never imports docling: it turns the items into
blocks and assembles the document, so furniture, heading inference and the PDF gates stay here.
"""

from __future__ import annotations

import time
from pathlib import Path

from substrate.extract.assemble import assemble
from substrate.models import LABEL_MAP, Block, Document, Kind

BATCH_PAGES = 100
# Items, and their text, across all of a document's batches. A batch is bounded by its frame; the
# number of batches comes from the PDF's own page count, so the whole needs bounds of its own.
MAX_DOCUMENT_ITEMS = 1_000_000
# Characters, which can cost four bytes each in memory.
MAX_DOCUMENT_TEXT = 64 * 1024 * 1024


def _page_count(pdf: Path) -> int:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(pdf)) as d:
        return len(d)


def _block(n: int, item: dict) -> Block:
    kind = LABEL_MAP.get(item["label"], Kind.TEXT)
    return Block(
        id=f"b{n:06d}",
        kind=kind,
        text=item["text"],
        page=item["page"],
        label=item["label"],
        level=None,  # docling reports 1 for everything; inferred later
        lang=item["lang"],
        height=item["height"],
        left=item["left"],
        furniture_claimed=("FURNITURE" in item["layer"] or kind is Kind.FURNITURE),
    )


class DoclingExtractor:
    name = "docling"

    def __init__(self, models: Path, batch_pages: int = BATCH_PAGES, *, quarantine=None,
                 retry_quarantined: bool = False):
        """`models` is a folder `paths.models_folder` has checked."""
        self.models = models
        self.batch_pages = batch_pages
        self.quarantine = quarantine
        self.retry_quarantined = retry_quarantined

    def extract(
        self, pdf: Path, doc_class: str, *, pages: tuple[int, int] | None = None, log=print
    ) -> Document:
        """`pdf` must not change while this runs: `substrate ingest` hands it its private copy
        (`snapshot`). Raises ConverterMissing, ConverterUnsupported, ConversionRefused or
        Quarantined (`convert`), and `worker_client.WorkerUnavailable` or `Interrupted`."""
        from substrate.extract import convert, worker_client
        from substrate.extract.base import sha256_file

        convert.need_extra(convert.PDF)
        real = pdf.resolve()
        checksum = sha256_file(real)
        total = _page_count(real)
        first, last = pages or (1, total)
        last = min(last, total)
        batches = [(start, min(start + self.batch_pages - 1, last))
                   for start in range(first, last + 1, self.batch_pages)]
        budget = sum(worker_client.pdf_batch_deadline(b - a + 1) for a, b in batches)

        def work(worker):
            items: list[dict] = []
            text = 0
            for start, end in batches:
                bt = time.monotonic()
                reply = worker.request(
                    {"type": "extract_pdf", "path": str(real), "first": start, "last": end},
                    worker_client.pdf_batch_deadline(end - start + 1),
                    ("items", "refused", "needs_extra", "unsupported"))
                if reply["type"] != "items":
                    return reply
                items += reply["items"]
                text += sum(len(item["text"]) for item in reply["items"])
                if len(items) > MAX_DOCUMENT_ITEMS or text > MAX_DOCUMENT_TEXT:
                    return {"type": "refused", "reason": f"docling read more than "
                            f"{MAX_DOCUMENT_ITEMS} items or {MAX_DOCUMENT_TEXT} characters from it, "
                            "more than the engine assembles"}
                log(f"  pages {start:>4}-{end:<4} {time.monotonic() - bt:6.1f}s  "
                    f"({len(items)} blocks so far)")
            return {"type": "items", "items": items}

        t0 = time.monotonic()

        def accept(reply, docling):
            blocks = [_block(n, item) for n, item in enumerate(reply["items"], 1)]
            return assemble(
                pdf,
                doc_class,
                blocks,
                total_pages=total,
                pages_processed=last - first + 1,
                seconds=time.monotonic() - t0,
                extractor=f"docling {docling}",
                extractor_arm=self.name,
                layout_model="docling-layout-heron",
            )

        return convert.through_worker(
            pdf, convert.PDF, checksum=checksum, models=self.models,
            budget=budget, work=work, accept=accept, quarantine=self.quarantine,
            retry_quarantined=self.retry_quarantined, log=log)
