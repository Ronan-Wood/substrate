"""The conversion worker: the one process docling runs in.

`substrate ingest` starts one for each document it converts (`worker_client`), speaks `convert/1`
to it over stdio (`convert_frames`), and kills its process group when it hangs, crashes or answers
something the engine cannot trust. The engine's own process never imports docling or torch.

WHAT THE BOUNDARY IS. It keeps docling out of the engine's process, and it keeps a converter that
hangs, crashes, floods its output or answers garbage from taking the ingest with it: the engine
kills it, retries once and then quarantines the file. WHAT IT IS NOT: a sandbox. The worker runs as
the user, with the user's files in reach, so a document that gets code to run inside docling runs
it with the user's privileges, as it would have in the engine's own process.

Started as `python -B -E -s -P -c <bootstrap> ROOT CPU_SECONDS LIFELINE`, in a private temporary
working directory, with an environment the engine builds (`worker_client.environment`).
"""

from __future__ import annotations

import contextlib
import importlib
import os
import signal
import sys
import time
from pathlib import Path

from substrate.extract import convert_frames as frames

# docling's own batch, which the engine's PDF path asks for: the converter is built once a worker.
_pdf_converter = None
# What `preload` could not import, answered as `needs_extra` rather than imported again: a module
# that failed once may fail differently the second time (torch raises a RuntimeError).
_preload_error: ImportError | None = None


def _limits(cpu_seconds: int) -> None:
    """A CPU backstop and no core files. The engine's wall clock is the real limit."""
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _, hard = resource.getrlimit(resource.RLIMIT_CPU)
    soft = cpu_seconds if hard == resource.RLIM_INFINITY else min(cpu_seconds, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))


def _watch_parent(lifeline: int) -> None:
    """A watchdog PROCESS in this worker's group that kills the group once the engine is gone.

    `lifeline` is the read end of a pipe whose write end only the engine holds, so its end of file
    means the engine exited or closed it, whatever then reuses the engine's pid. A process,
    not a thread: a converter stuck in C code that holds the GIL would stop a thread from ever
    running. It holds none of the worker's other descriptors, so the engine still sees the
    worker's stdout close when the worker dies, and it ignores SIGTERM, so the engine's gentle
    stop of the group leaves it watching until the SIGKILL that follows. Linux also asks the kernel
    to kill the worker itself when the engine dies (PR_SET_PDEATHSIG).
    """
    if sys.platform.startswith("linux"):
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
    if os.fork():
        os.close(lifeline)
        return
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.closerange(0, lifeline)
        os.closerange(lifeline + 1, 4096)
        with contextlib.suppress(OSError):
            while os.read(lifeline, 1):
                pass
        os.killpg(os.getpgrp(), signal.SIGKILL)
    finally:
        os._exit(0)


# Imported before `hello`, under its deadline: the time they take is the worker's, not the first
# document's. `document_converter` brings torch with it where `convert-ml` is installed.
PRELOAD = ("docling.datamodel.base_models", "docling.datamodel.pipeline_options",
           "docling.document_converter")


def preload() -> None:
    """Import docling's converter. A piece that will not import is kept for the request that
    needs it, which answers `needs_extra` with it."""
    global _preload_error

    for name in PRELOAD:
        try:
            importlib.import_module(name)
        except ImportError as e:
            _preload_error = e
            return


def _missing(reply: dict) -> dict | None:
    """`needs_extra`, when `preload` could not import docling's converter."""
    if _preload_error is None:
        return None
    return {**reply, "type": "needs_extra", "error": str(_preload_error)[:frames.MAX_SENTENCE]}


def hello() -> dict:
    """The docling this worker will convert with, which the engine requires to be its own."""
    from importlib.metadata import PackageNotFoundError

    from substrate.extract import convert

    try:
        docling = convert.docling_version()
    except PackageNotFoundError:
        docling = None
    return {"v": frames.PROTOCOL, "type": "hello", "docling": docling}


def cpu_accelerator():
    """CPU, NOT THE GPU DOCLING PICKS BY ITSELF — for every pipeline that loads the layout model.

    Measured 2026-09-16 on docling 2.126.0: its automatic device is Metal, and the layout model there
    dies with "Cannot convert a MPS Tensor to float64 dtype as the MPS framework doesn't support
    float64" — every PDF, before a single block is read. The same document converts on CPU in 12.1 s.
    An accelerator that refuses the whole document is not an accelerator. The image pipeline runs the
    same layout model and died the same way until it took this too.
    """
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions

    return AcceleratorOptions(device=AcceleratorDevice.CPU)


def _models() -> Path | None:
    """The models folder the engine checked and named in this worker's environment, or None."""
    value = os.environ.get("DOCLING_ARTIFACTS_PATH")
    return Path(value) if value else None


def convert_document(request: dict) -> dict:
    """One `convert` request, answered: `converted`, `refused`, `needs_extra` or `unsupported`."""
    from substrate.extract import convert

    reply = {"v": frames.PROTOCOL, "id": request["id"]}
    spec = next((s for s in convert.ACCEPTED if s.token == request["format"] and s.arm == "docling"),
                None)
    if spec is None:
        return {**reply, "type": "unsupported", "error": f"{request['format']!r} is not a format "
                "this worker converts"}
    models = _models() if spec.uses_models else None
    if spec.uses_models and models is None:
        return {**reply, "type": "unsupported", "error": f"{spec.token} needs a models folder"}
    if (missing := _missing(reply)) is not None:
        return missing
    try:
        from docling.datamodel.base_models import ConversionStatus, InputFormat
        from docling.document_converter import DocumentConverter
    except ImportError as e:
        return {**reply, "type": "needs_extra", "error": str(e)[:frames.MAX_SENTENCE]}
    try:
        fmt = InputFormat(spec.docling_format)
    except ValueError as e:
        return {**reply, "type": "unsupported", "error": str(e)[:frames.MAX_SENTENCE]}

    options: dict = {}
    try:
        if spec.uses_models:
            from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions
            from docling.document_converter import ImageFormatOption

            opts = PdfPipelineOptions(artifacts_path=str(models))
            opts.do_ocr = True  # an image has no text layer to prefer; OCR is the only reading
            # APPLE VISION, NAMED rather than left to docling's automatic choice. Its fallback,
            # RapidOCR, needs OpenCV, whose macOS wheel carries a GPL FFmpeg; `pyproject.toml`
            # excludes it, and naming the engine here means a stray reinstall cannot switch engines
            # without a code change.
            opts.ocr_options = OcrMacOptions()
            opts.do_table_structure = True
            opts.accelerator_options = cpu_accelerator()
            options = {InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts)}
        t0 = time.monotonic()
        result = DocumentConverter(allowed_formats=[fmt], format_options=options).convert(
            request["path"])
    except ImportError as e:
        # The converter, not the file: docling raises a bare ImportError for a piece of its own it
        # imports only when a pipeline first runs (`ocrmac`, `docling-parse`).
        return {**reply, "type": "needs_extra", "error": str(e)[:frames.MAX_SENTENCE]}
    except Exception as e:  # noqa: BLE001 — every other docling failure is one refusal
        return {**reply, "type": "refused",
                "reason": f"docling could not convert it — {e}"[:frames.MAX_SENTENCE]}

    # PARTIAL_SUCCESS is refused with the failures. It is precisely "some of the document is
    # missing and the rest looks fine", which is the state this engine exists to not produce.
    if result.status is not ConversionStatus.SUCCESS:
        return {**reply, "type": "refused", "reason": (
            f"docling returned {result.status} — refusing a conversion that is not whole. Errors: "
            f"{getattr(result, 'errors', None)}")[:frames.MAX_SENTENCE]}
    try:
        markdown = result.document.export_to_markdown()
    except Exception as e:  # noqa: BLE001 — docling's answer about this file, every time
        return {**reply, "type": "refused",
                "reason": f"docling could not render it — {e}"[:frames.MAX_SENTENCE]}
    pages = None
    try:
        n = result.document.num_pages()
        pages = n if n and n > 0 else None
    except Exception:  # noqa: BLE001 — a page count is provenance, never a gate
        pages = None
    return {**reply, "type": "converted", "markdown": markdown, "pages": pages,
            "status": str(result.status), "seconds": round(time.monotonic() - t0, 3)}


def _content_layers():
    """Include FURNITURE: the engine adjudicates those labels itself rather than let them vanish."""
    try:
        from docling_core.types.doc.labels import ContentLayer
    except ImportError:
        try:
            from docling_core.types.doc import ContentLayer
        except ImportError:
            return None
    return set(ContentLayer)


def _iter(doc):
    layers = _content_layers()
    if layers is not None:
        try:
            yield from doc.iterate_items(included_content_layers=layers)
            return
        except TypeError:
            pass
    yield from doc.iterate_items()


def _text_of(item, doc) -> tuple[str, str | None]:
    """Return (text, code_language). Tables render as markdown; everything else is text."""
    label = str(getattr(item, "label", "")).lower()
    if "table" in label:
        for attempt in (lambda: item.export_to_markdown(doc), lambda: item.export_to_markdown()):
            try:
                return attempt(), None
            except Exception:
                continue
        return (getattr(item, "text", "") or ""), None
    text = (getattr(item, "text", "") or "").strip()
    lang = getattr(item, "code_language", None)
    return text, (str(lang) if lang else None)


def _converter(models: Path):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions(artifacts_path=str(models))
    opts.do_ocr = False  # both reference inputs carry real text layers
    opts.do_table_structure = True
    opts.accelerator_options = cpu_accelerator()
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def extract_pdf(request: dict) -> dict:
    """One page batch of a PDF read with docling's models: its items, for the engine to assemble."""
    global _pdf_converter

    reply = {"v": frames.PROTOCOL, "id": request["id"]}
    models = _models()
    if models is None:
        return {**reply, "type": "unsupported", "error": "the PDF reader needs a models folder"}
    if (missing := _missing(reply)) is not None:
        return missing
    try:
        if _pdf_converter is None:
            _pdf_converter = _converter(models)
        result = _pdf_converter.convert(request["path"], page_range=(request["first"], request["last"]))
    except ImportError as e:
        return {**reply, "type": "needs_extra", "error": str(e)[:frames.MAX_SENTENCE]}
    except Exception as e:  # noqa: BLE001
        return {**reply, "type": "refused",
                "reason": f"docling could not read pages {request['first']}-{request['last']} — "
                          f"{e}"[:frames.MAX_SENTENCE]}
    try:
        items = [_item(item, result.document) for item, _level in _iter(result.document)]
    except Exception as e:  # noqa: BLE001 — docling's answer about these pages, every time
        return {**reply, "type": "refused",
                "reason": f"docling could not walk pages {request['first']}-{request['last']} — "
                          f"{e}"[:frames.MAX_SENTENCE]}
    return {**reply, "type": "items", "items": [item for item in items if item is not None]}


def _item(item, doc) -> dict | None:
    text, lang = _text_of(item, doc)
    if not text:
        return None
    prov = getattr(item, "prov", None)
    page = getattr(prov[0], "page_no", None) if prov else None
    height = left = None
    if prov and getattr(prov[0], "bbox", None):
        bb = prov[0].bbox
        height, left = round(abs(bb.t - bb.b), 1), round(bb.l, 1)
    return {
        "label": str(getattr(item, "label", "")).lower().split(".")[-1][:frames.MAX_SHORT],
        "text": text, "page": page if isinstance(page, int) and page >= 0 else None,
        "lang": lang[:frames.MAX_SHORT] if lang else None, "height": height, "left": left,
        "layer": str(getattr(item, "content_layer", "")).upper()[:frames.MAX_SHORT],
    }


HANDLERS = {"convert": convert_document, "extract_pdf": extract_pdf}


def wire(frame: dict) -> bytes:
    """A reply as the engine will read it, or a refusal when the engine would not accept it: too
    large, a number out of bounds, text that is not UTF-8. That is the document's answer, every
    time, so it must not reach the engine as a fault to retry and quarantine."""
    try:
        if frame["type"] == "converted":
            return frames.encode_converted(frame["id"], frame["markdown"], pages=frame["pages"],
                                           status=frame["status"], seconds=frame["seconds"])
        if frame["type"] == "items":
            return frames.encode_items(frame["id"], frame["items"])
        data = frames.encode(frame)
        frames.decode(data[:-1])
        return data
    except (frames.FrameError, ValueError, UnicodeError) as e:
        return frames.encode({"v": frames.PROTOCOL, "type": "refused", "id": frame["id"],
                              "reason": f"its conversion cannot be returned to the engine: "
                                        f"{e}"[:frames.MAX_SENTENCE]})


def main(argv: list[str] | None = None) -> int:
    """Serve requests until `bye` or end of input. argv: ROOT CPU_SECONDS LIFELINE."""
    argv = sys.argv[1:] if argv is None else argv
    # An engine started ignoring a stop keeps it ignored, and the spawn passes that on: the worker
    # must still die of the engine's SIGTERM, as it would have under the default.
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, signal.SIG_DFL)
    _limits(int(argv[1]))
    _watch_parent(int(argv[2]))
    # Frames go to a private copy of stdout; fd 1 becomes stderr, so a library that prints cannot
    # put a line into the frame stream.
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[out.write(view):]

    preload()
    send(frames.encode(hello()))
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline(frames.MAX_LINE + 1)
        if not line:
            break
        request = frames.decode(line.rstrip(b"\n"))
        if request["type"] == "bye":
            break
        handler = HANDLERS.get(request["type"])
        if handler is None:
            raise frames.FrameError(f"{request['type']} is not a request")
        send(wire(handler(request)))
    # Exit now: the answer is sent, and an interpreter's teardown after torch can outlast the
    # engine's grace period, which would throw a good conversion away.
    sys.stderr.flush()
    os._exit(0)
