"""Which converted file is retried, which is quarantined, and the record that says so.

A conversion runs in a worker process (`worker_client`). When the worker hangs, crashes or answers
something the engine cannot trust (a FAULT), the engine kills it and tries once more in a fresh
one; a second fault QUARANTINES the file: `ingest` refuses it at once from then on, exit 3, until
`--retry-quarantined` asks again or the converter changes. Docling's own answer about the file (a
refusal, a missing extra, a format it does not name) is never a fault, so it is never retried and
never quarantines: it would say the same thing again.

This is not compose's quarantine (`substrate.quarantine`), which leaves a note out of an index. It
withholds one file from one ingest, loudly, and names the way out.

`quarantine_decision` is the rule, as a pure function with no I/O, so a model can drive it
unchanged. Its Lean model and differential harness are `experiments/lean-convert-quarantine/`
(VERIFIED.md).

The RECORD is `<blob store>/convert/quarantine/<source_sha256>.json`: the checksum, the format,
whether a models folder was named, the docling version, the faults and when. Nothing from the
document: no name, no path, no text, no stderr.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# What a worker can do wrong, and the outcomes that end an attempt with docling's own answer.
FAULTS = ("hang", "crash", "protocol")
ANSWERS = ("ok", "refused", "needs_extra", "unsupported", "changed")
# Attempts after the first. A second fault in a row quarantines.
RETRIES = 1

RECORD_VERSION = 1
RECORD_KEYS = frozenset({"v", "source_sha256", "format", "models", "docling", "faults", "at"})
_MAX_RECORD = 64 * 1024

# The record that is there for this file, as the decision sees it.
ABSENT, MATCHING, OTHER = "absent", "matching", "other"


@dataclass(frozen=True)
class Decision:
    """`verdict`: blocked · attempt · converted · refused · needs_extra · unsupported · changed ·
    quarantined. `record`: keep, write (the faults so far) or delete."""

    verdict: str
    record: str = "keep"


_VERDICT = {"ok": "converted", "refused": "refused", "needs_extra": "needs_extra",
            "unsupported": "unsupported", "changed": "changed"}


def quarantine_decision(outcomes: Sequence[str], record: str, retry_quarantined: bool) -> Decision:
    """What to do after `outcomes`, the attempts so far in order, for a file whose record is
    `record` (ABSENT, MATCHING this converter, or OTHER). Pure.

    An answer ends the run with its own verdict, and only a conversion that went through clears the
    record. Faults are retried until RETRIES + 1 of them, then quarantine. A matching record stops
    the file before any attempt unless the operator asked to retry it. Raises ValueError for a
    sequence no driver should produce: an outcome after an answer, more attempts than RETRIES + 1
    (an answer after that many faults included), or any attempt on a file the record blocks.
    """
    if record not in (ABSENT, MATCHING, OTHER):
        raise ValueError(f"unknown record state {record!r}")
    for i, outcome in enumerate(outcomes):
        if outcome not in FAULTS and outcome not in ANSWERS:
            raise ValueError(f"unknown outcome {outcome!r}")
        if outcome in ANSWERS and i != len(outcomes) - 1:
            raise ValueError("an outcome after an answer")
    if len(outcomes) > RETRIES + 1:
        raise ValueError(f"{len(outcomes)} attempts, past the {RETRIES + 1} allowed")
    if record == MATCHING and not retry_quarantined:
        if outcomes:
            raise ValueError("an attempt on a file its record blocks")
        return Decision("blocked")
    if not outcomes:
        return Decision("attempt")
    last = outcomes[-1]
    if last in ANSWERS:
        if last == "ok":
            return Decision("converted", "delete" if record != ABSENT else "keep")
        return Decision(_VERDICT[last])
    if len(outcomes) <= RETRIES:
        return Decision("attempt")
    return Decision("quarantined", "write")


# ---------------------------------------------------------------- the record

def directory(store: Path) -> Path:
    return store / "convert" / "quarantine"


def _path(store: Path, checksum: str) -> Path:
    from substrate import blobs

    if not blobs.is_checksum(checksum):
        raise ValueError(f"{checksum!r} is not a SHA-256 checksum")
    return directory(store) / f"{checksum}.json"


def _valid(record: object, checksum: str) -> bool:
    return (type(record) is dict and set(record) == RECORD_KEYS
            and record["v"] == RECORD_VERSION and record["source_sha256"] == checksum
            and type(record["format"]) is str and type(record["models"]) is bool
            and (record["docling"] is None or type(record["docling"]) is str)
            and type(record["faults"]) is list and record["faults"]
            and all(f in FAULTS for f in record["faults"]) and type(record["at"]) is str)


def read(store: Path, checksum: str) -> dict | None:
    """The file's record, or None when there is none. A record that is not one this engine wrote
    (not a regular file, too large, not the expected shape) reads as None, so it cannot block a
    file; the next fault replaces it."""
    path = _path(store, checksum)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_RECORD or st.st_uid != os.geteuid():
            return None
        data = os.read(fd, _MAX_RECORD + 1)
    finally:
        os.close(fd)
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return record if _valid(record, checksum) else None


def state(record: dict | None, *, fmt: str, models: bool, docling: str | None) -> str:
    """ABSENT, MATCHING when the record is this converter's, else OTHER."""
    if record is None:
        return ABSENT
    same = record["format"] == fmt and record["models"] == models and record["docling"] == docling
    return MATCHING if same else OTHER


def write(store: Path, checksum: str, *, fmt: str, models: bool, docling: str | None,
          faults: Sequence[str]) -> Path:
    """Write the record atomically: a private temporary file in the same folder, renamed over any
    record there. Two ingests of one file racing each leave a whole record; the last rename wins."""
    from substrate import blobs

    folder = blobs._ensure(store, "convert", "quarantine")
    final = _path(store, checksum)
    record = {"v": RECORD_VERSION, "source_sha256": checksum, "format": fmt, "models": models,
              "docling": docling, "faults": list(faults),
              "at": datetime.now(UTC).isoformat(timespec="seconds")}
    if not _valid(record, checksum):
        raise ValueError(f"not a record this engine writes: {record}")
    fd, tmp = tempfile.mkstemp(prefix=".record-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, sort_keys=True)
            f.flush()
            os.fchmod(f.fileno(), 0o400)
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    blobs._fsync_dir(folder)
    return final


def remove(store: Path, checksum: str) -> bool:
    try:
        os.unlink(_path(store, checksum))
    except FileNotFoundError:
        return False
    return True


def listing(store: Path) -> list[dict]:
    """Every record in the store, for `blobs status`: checksum, format, fault, at."""
    folder = directory(store)
    try:
        names = sorted(os.listdir(folder))
    except (FileNotFoundError, NotADirectoryError):
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        checksum = name.removesuffix(".json")
        try:
            record = read(store, checksum)
        except ValueError:
            continue
        if record is not None:
            out.append({"checksum": checksum, "format": record["format"],
                        "fault": record["faults"][-1], "at": record["at"]})
    return out
