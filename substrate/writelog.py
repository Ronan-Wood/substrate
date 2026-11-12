"""The engine's record of the notes it wrote for granted clients: `~/.substrate/writes.jsonl`.

A note's `written_by` / `amended_by` lines are labels: anyone who can edit the file can write
them. This log is the record. The engine appends to it around every write it runs for a GRANTED
client (`grants.Decision.client`), and search and expand report a note's provenance from it rather
than from the note's own lines. The owner's writes (stdio, an `open` socket, `frontmatter fix`) are
not a client acting under a grant, and are not logged.

**Outside the vault,** because a vault syncs and is edited: a record there would be edited,
conflicted and copied along with the notes it describes. One file for every scope, beside
`grants.toml`, 0600, appended to with `O_APPEND`. Nothing rewrites or deletes a line.

**Two records per write,** under one `flock`: `intent` is appended and fsynced BEFORE the note is
touched (if that fails the write is refused, so a write the log cannot record does not run), then
`done`, same `seq`, after it, or `void` when the write was refused inside (nothing was written).
`sha256_after` is what makes a crash decidable: an `intent` with neither landed when the note's
bytes hash to it, or when a later write of the same note replaced exactly those bytes (its
`sha256_before`), and did not land when the note still holds what it held before.
`substrate writes check` appends the `done` or `void` it decides.

**`seq` exposes a gap, a repeat or a reordering** by accident or by a sync tool. It is not a
defence against a process running as the user, which can rewrite the whole file: the same
boundary as the grant tokens (`grants`).

**Provenance is looked up at query time,** by joining the index's `documents.source_path` and
`note_sha256` against the log, so it needs no schema change and sees a write at once. Stdlib only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from substrate import scopes

FILE = "writes.jsonl"
RECORD_VERSION = 1
PHASES = ("intent", "done", "void")
# Why a write is `void`: refused inside the logged section, or found by `check` not to have landed.
VOID_REASONS = ("refused", "did_not_land")
OPS = ("ingest", "amend")

# What a passage's `provenance.state` says about the indexed bytes of its note.
#   engine:   exactly what an engine write produced, for `client`
#   edited:   the engine wrote this note for `client`, and these bytes are not that write's
#   none:     the log has no landed write for this note: by hand, by the owner, or before the log
#   unknown:  the log could not be read, or it has writes for the note and the index holds no
#             checksum to compare with them
STATES = ("engine", "edited", "none", "unknown")
# What `provenance.stamp` says about the note's own `amended_by` (else `written_by`) line; null when
# the note was read and has none.
#   matches:       the last engine write of these bytes' note was for that client
#   contradicted:  it was for another client
#   unchecked:     no verdict was reached: the log has no engine write to compare the stamp with
#                  (a note stamped before the log existed, or by hand), the note was not read (an
#                  agent-mode server, a note that changed since it was indexed), or the log could
#                  not be read
STAMP_VERDICTS = ("matches", "contradicted", "unchecked")
# The frontmatter keys the engine writes for a granted write (`notes.stamp_new` / `stamp_amend`).
STAMP_KEYS = ("written_by", "amended_by")
# Which of them names the LAST writer: an amend's, else the note's first.
_CLAIM_ORDER = tuple(reversed(STAMP_KEYS))

# How long a write waits for the log's lock before it is refused.
LOCK_WAIT_SECONDS = 30.0
# A record is a few hundred bytes. A line past this is not one the engine wrote.
MAX_LINE_BYTES = 16 * 1024
# The largest note whose stamp is read. The whole note is hashed, so its stamp is read only from
# the bytes the index holds; a larger note's stamp is unchecked.
STAMP_NOTE_BYTES = 1024 * 1024
# How many notes' stamps one process remembers, by the file's identity, size and mtime.
STAMP_CACHE_ENTRIES = 4096
# The tail read to find the last intent's `seq` without scanning the whole log.
_TAIL_BYTES = 64 * 1024


class WriteLogError(OSError):
    """The log cannot be opened, read or appended to, or a write's identity is incomplete."""


def home() -> Path:
    return scopes.engine_home()


def path() -> Path:
    return home() / FILE


def _lock_path() -> Path:
    return home() / f".{FILE}.lock"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- files

def _open_regular(where: Path | str, flags: int, *, owned: bool, mode: int = 0o600) -> int:
    """`where` opened with `flags`, never through a symlink and never blocking on a FIFO, refused
    unless it is a regular file (and, when `owned`, one this user owns). Raises OSError."""
    fd = os.open(where, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{where} is not a regular file")
        if owned and st.st_uid != os.geteuid():
            raise OSError(f"{where} is not owned by this user")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_note(where: str, limit: int) -> bytes | None:
    """The bytes of the regular file at `where`, or None when it cannot be read or is over
    `limit`. For a note in a vault: never followed through a symlink, never a FIFO."""
    try:
        fd = _open_regular(where, os.O_RDONLY, owned=False)
    except (OSError, ValueError):
        return None
    try:
        if os.fstat(fd).st_size > limit:
            return None
        data = os.pread(fd, limit + 1, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if len(data) > limit else data


def note_sha256(where: str, limit: int) -> str | None:
    data = read_note(where, limit)
    return None if data is None else sha256(data)


# --------------------------------------------------------------------------- writing

def _open_log() -> int:
    """The log, open for appending and for reading its tail, created 0600."""
    try:
        home().mkdir(parents=True, exist_ok=True)
        return _open_regular(path(), os.O_RDWR | os.O_CREAT | os.O_APPEND, owned=True)
    except OSError as e:
        raise WriteLogError(f"cannot open the write log {path()}: {e}") from e


def _last_seq(fd: int) -> tuple[int, bool]:
    """(the `seq` of the last intent in the log, whether its last line is unterminated).

    From the last intent, not the largest number on any line: `decide_unfinished` appends `done`
    and `void` lines for old writes, and a tail of those would otherwise hand the next write a
    `seq` already used. Read from the tail, and from the whole file when the tail holds no intent."""
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return 0, False
        torn = os.pread(fd, 1, size - 1) != b"\n"
        start = max(0, size - _TAIL_BYTES)
        for begin in (start, 0) if start else (0,):
            data = os.pread(fd, size - begin, begin)
            seqs = [rec["seq"] for rec in map(_parse, data.split(b"\n"))
                    if rec is not None and rec.get("phase") == "intent"
                    and isinstance(rec.get("seq"), int) and not isinstance(rec["seq"], bool)]
            if seqs or begin == 0:
                return max(seqs, default=0), torn
        return 0, torn
    except OSError as e:
        raise WriteLogError(f"cannot read the write log {path()}: {e}") from e


def _append(fd: int, record: dict, *, torn: bool = False) -> None:
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    # An unterminated last line is a crash mid-append. It is ended first, so the record after it
    # is a line of its own rather than the tail of a garbled one.
    data = (b"\n" if torn else b"") + line
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short write ({written} of {len(data)} bytes)")
        os.fsync(fd)
    except OSError as e:
        raise WriteLogError(f"cannot append to the write log {path()}: {e}") from e


@dataclass(frozen=True)
class Identity:
    """Who a write ran for, bound by the server from the verified grant."""

    client: str
    grant: str
    scope: str
    vault: str


@contextlib.contextmanager
def _locked():
    """The log's one lock, beside it: held across a write's intent, the write and its done, and
    across `decide_unfinished`."""
    stack = contextlib.ExitStack()
    try:
        home().mkdir(parents=True, exist_ok=True)
        stack.enter_context(scopes.flocked(_lock_path(), wait=LOCK_WAIT_SECONDS))
    except OSError as e:
        raise WriteLogError(f"cannot lock the write log beside {path()}: {e}") from e
    with stack:
        yield


@contextlib.contextmanager
def recorded(who: Identity | None, *, op: str, target: Path, doc_id: str | None,
             before: str | None, after: str):
    """Around one write: for a granted one (`who`), `intent` appended and fsynced before the body
    runs and `done` after it returns; for the owner's (`who` None), nothing.

    If `intent` cannot be appended, or `who` names no client or grant, WriteLogError is raised and
    the body never runs. If the body raises, no `done` is appended: the hash settles it. If `done`
    cannot be appended the note is written regardless, and `substrate writes check` settles it."""
    if who is None:
        yield
        return
    if op not in OPS:
        raise ValueError(f"unknown write op {op!r}")
    if not who.client or not who.grant:
        raise WriteLogError("this write names no client or grant, so the log cannot record it")
    rel = _relative(target, who.vault)
    with _locked():
        fd = _open_log()
        try:
            seq, torn = _last_seq(fd)
            intent = {"v": RECORD_VERSION, "seq": seq + 1, "phase": "intent", "at": _now(),
                      "op": op, "client": who.client, "grant": who.grant, "scope": who.scope,
                      "vault": who.vault, "path": rel, "doc_id": doc_id,
                      "sha256_before": before, "sha256_after": after}
            _append(fd, intent, torn=torn)
            try:
                yield
            except Exception:
                # Refused inside: nothing was written, and the log says so, so no check has to
                # decide it later.
                try:
                    _append(fd, _terminal(seq + 1, "void", "refused"))
                except WriteLogError as e:
                    sys.stderr.write(f"substrate: the write to {target} was refused, and its "
                                     f"`void` record was not appended ({e}). `substrate writes "
                                     f"check` decides it.\n")
                raise
            try:
                _append(fd, _terminal(seq + 1, "done"))
            except WriteLogError as e:
                sys.stderr.write(f"substrate: the note {target} was written, and its `done` record "
                                 f"was not ({e}). `substrate writes check` settles it.\n")
        finally:
            os.close(fd)


def _terminal(seq: int, phase: str, reason: str | None = None) -> dict:
    rec = {"v": RECORD_VERSION, "seq": seq, "phase": phase, "at": _now()}
    if reason is not None:
        rec["reason"] = reason
    return rec


def _relative(target: Path, vault: str) -> str:
    """`target` relative to the vault's real path, as `/`-separated text."""
    real = Path(os.path.realpath(vault))
    t = Path(os.path.realpath(target))
    try:
        return t.relative_to(real).as_posix()
    except ValueError:
        raise WriteLogError(f"{target} is not inside the vault {vault}") from None


# --------------------------------------------------------------------------- reading

@dataclass
class Write:
    seq: int
    op: str
    client: str
    scope: str
    key: str
    doc_id: str | None
    before: str | None
    after: str
    at: str
    done: bool = False
    void: str | None = None      # the reason, once a `void` record names this write


@dataclass
class Log:
    """The log as read: every write in `seq` order, and what was wrong with the file, by line."""

    writes: list[Write] = field(default_factory=list)
    damage: list[str] = field(default_factory=list)
    torn: bool = False
    by_key: dict[str, list[Write]] = field(default_factory=dict)
    _by_seq: dict[int, Write] = field(default_factory=dict)
    _line: int = 0

    def feed(self, line: bytes) -> None:
        """One complete line of the log, without its newline."""
        self._line += 1
        bad = self._read(line)
        if bad:
            self.damage.append(f"line {self._line}: {bad}")

    def _read(self, line: bytes) -> str | None:
        """Take one line in; return what is wrong with it, if anything."""
        if not line.strip():
            return "empty"
        rec = _parse(line)
        if rec is None:
            return "not a record"
        if rec.get("v") != RECORD_VERSION:
            return f"record version {_shown(rec.get('v'))}, which this engine does not read"
        seq, phase = rec.get("seq"), rec.get("phase")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or phase not in PHASES:
            return "no usable seq and phase"
        if phase != "intent":
            w = self._by_seq.get(seq)
            if w is None:
                return f"`{phase}` for seq {seq}, which has no intent"
            if phase == "done":
                # A second `done` is what two checks settling one write at once leave.
                w.done = True
            else:
                reason = rec.get("reason")
                w.void = reason if reason in VOID_REASONS else "refused"
            if w.done and w.void:
                return f"seq {seq} is both done and void"
            return None
        w = _write(rec)
        if w is None:
            return "an intent missing a field, or naming an unusable path"
        if seq in self._by_seq:
            return f"seq {seq} repeated"
        last = self.writes[-1].seq if self.writes else 0
        self.writes.append(w)
        self._by_seq[seq] = w
        self.by_key.setdefault(w.key, []).append(w)
        if seq < last:
            return f"seq {seq} after {last}, out of order"
        if seq > last + 1:
            return f"seq {seq} after {last}, {seq - last - 1} missing"
        return None


def _shown(value: object, cap: int = 64) -> str:
    """A value from the log or a note, safe to print (`reader._shown`)."""
    from substrate.markdown.reader import _shown as shown

    return shown(value if isinstance(value, str) else repr(value), cap)


def _parse(line: bytes) -> dict | None:
    if not line or len(line) > MAX_LINE_BYTES:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef"
                                                                for c in value)


def _write(rec: dict) -> Write | None:
    """An intent as provenance and `check` read it. Only what they use is required: a damaged
    `grant`, which neither reads, does not drop the write, and a damaged `doc_id` or
    `sha256_before` only loses what it was used for."""
    required = ("op", "client", "scope", "vault", "path", "at")
    if not all(isinstance(rec.get(k), str) and rec[k] for k in required):
        return None
    if rec["op"] not in OPS or not _hex(rec.get("sha256_after")):
        return None
    before = rec.get("sha256_before")
    doc_id = rec.get("doc_id")
    try:
        key = _key(rec["vault"], rec["path"])
    except (ValueError, OSError):
        return None
    return Write(seq=rec["seq"], op=rec["op"], client=rec["client"], scope=rec["scope"],
                 key=key, doc_id=doc_id if isinstance(doc_id, str) and doc_id else None,
                 before=before if _hex(before) else None, after=rec["sha256_after"],
                 at=rec["at"])


def _key(vault: str, rel: str) -> str:
    return os.path.normpath(os.path.join(os.path.realpath(vault), rel))


def note_key(source_path: str) -> str:
    """The key a note's index row is looked up under: its real path."""
    return os.path.realpath(source_path)


def _read_fd() -> int | None:
    try:
        return _open_regular(path(), os.O_RDONLY, owned=True)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise WriteLogError(f"cannot open the write log {path()}: {e}") from e


def read() -> Log:
    """The whole log. An absent log is an empty one; one that cannot be read raises
    WriteLogError."""
    log = Log()
    fd = _read_fd()
    if fd is None:
        return log
    try:
        _consume(log, fd, 0)
    finally:
        os.close(fd)
    return log


def _consume(log: Log, fd: int, offset: int) -> int:
    """Feed every complete line from `offset` on; return the offset after the last one."""
    try:
        size = os.fstat(fd).st_size
        data = os.pread(fd, size - offset, offset) if size > offset else b""
    except OSError as e:
        raise WriteLogError(f"cannot read the write log {path()}: {e}") from e
    end = data.rfind(b"\n")
    complete, rest = (data[:end], data[end + 1:]) if end >= 0 else (b"", data)
    if end >= 0:
        for line in complete.split(b"\n"):
            log.feed(line)
    # A last line with no newline yet is a crash mid-append, or an append still in flight. It is
    # not read, and it is read again from here when more arrives.
    log.torn = bool(rest)
    return offset + end + 1 if end >= 0 else offset


class Cache:
    """The log as one serving process last read it. Unchanged (same file, size and mtime), it is
    used as it is. Grown, it is re-read from where the last read stopped, once the bytes before that
    point still hash as they did; otherwise (replaced, shrunk, or rewritten in place) it is read
    whole. `view()` holds the cache's lock while the caller reads the log, so no other thread's
    re-read changes it under them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._drop()

    @contextlib.contextmanager
    def view(self):
        """The log, or None when it cannot be read, for the body of the `with`."""
        with self._lock:
            yield self._refresh()

    def _refresh(self) -> Log | None:
        try:
            fd = _read_fd()
        except WriteLogError:
            return self._drop()
        if fd is None:
            self._drop()
            self._log = Log()
            return self._log
        try:
            st = os.fstat(fd)
            stamp = (str(path()), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            if self._log is not None and stamp == self._stamp:
                return self._log
            same = (self._log is not None and self._stamp is not None
                    and self._stamp[:3] == stamp[:3] and st.st_size >= self._offset
                    and hashlib.sha256(os.pread(fd, self._offset, 0)).digest()
                    == self._prefix.digest())
            if not same:
                self._log, self._offset, self._prefix = Log(), 0, hashlib.sha256()
            start = self._offset
            self._offset = _consume(self._log, fd, start)
            self._prefix.update(os.pread(fd, self._offset - start, start))
            self._stamp = stamp
            return self._log
        except (OSError, ValueError):
            return self._drop()
        finally:
            os.close(fd)

    def _drop(self) -> None:
        self._log: Log | None = None
        self._stamp: tuple | None = None
        self._offset = 0
        self._prefix = hashlib.sha256()


CACHE = Cache()


# --------------------------------------------------------------------------- provenance

class _Unread:
    def __repr__(self) -> str:
        return "UNREAD"


UNREAD = _Unread()

_STAMPS: OrderedDict[tuple, tuple[str, str | None]] = OrderedDict()
_STAMPS_LOCK = threading.Lock()


def _claim(data: bytes) -> str | None | _Unread:
    """The stamp `data` states: the last non-blank `amended_by`, else the last non-blank
    `written_by`, as the reader reads the frontmatter."""
    from substrate.markdown import reader

    try:
        pairs = reader.frontmatter_pairs(reader.normalized_text(data))
    except (UnicodeDecodeError, ValueError):
        return UNREAD
    for key in _CLAIM_ORDER:
        values = [v.strip() for k, v in pairs or () if k == key and v.strip()]
        if values:
            return values[-1]
    return None


def stamp_of(source_path: str | None, indexed_sha256: str | None) -> str | None | _Unread:
    """The note's own claim about who last wrote it, as the reader reads its frontmatter. None
    when it has none; UNREAD when it cannot be read for it.

    Read only from the bytes `indexed_sha256` names, so the stamp describes the same version of
    the note as `provenance.state`: a note that changed since it was indexed, that has no checksum
    in the index, or that is over STAMP_NOTE_BYTES reads as UNREAD. Remembered by the file's
    device, inode, size and mtime, so a search does not re-read and re-hash an unchanged note."""
    if not source_path or not source_path.lower().endswith(".md"):
        return None
    if not indexed_sha256:
        return UNREAD
    try:
        st = os.stat(source_path, follow_symlinks=False)
    except (OSError, ValueError):
        return UNREAD
    if not stat.S_ISREG(st.st_mode) or st.st_size > STAMP_NOTE_BYTES:
        return UNREAD
    key = (source_path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    with _STAMPS_LOCK:
        hit = _STAMPS.get(key)
        if hit is not None:
            _STAMPS.move_to_end(key)
    if hit is None:
        data = read_note(source_path, STAMP_NOTE_BYTES)
        if data is None:
            return UNREAD
        claim = _claim(data)
        if claim is UNREAD:
            return UNREAD
        hit = (sha256(data), claim)
        with _STAMPS_LOCK:
            _STAMPS[key] = hit
            while len(_STAMPS) > STAMP_CACHE_ENTRIES:
                _STAMPS.popitem(last=False)
    sha, claim = hit
    return claim if sha == indexed_sha256 else UNREAD


def provenance_record(state: str, write: Write | None, stamp: str | None,
                      stamped_by: str | None) -> dict:
    """One passage's `provenance`: one key set whatever the state."""
    return {
        "state": state,
        "client": write.client if write else None,
        "op": write.op if write else None,
        "at": write.at if write else None,
        "stamp": stamp,
        "stamped_by": stamped_by,
    }


def _replaced_later(w: Write, writes: list[Write]) -> bool:
    """A later write of the same note replaced exactly the bytes `w` named, so `w` landed."""
    return any(x.seq > w.seq and x.before == w.after for x in writes)


def _landed(writes: list[Write], indexed: str | None) -> list[Write]:
    """The writes that landed: with a `done`, whose bytes are the indexed ones, or whose bytes a
    later write of the same note replaced. Never a `void` one."""
    return [w for w in writes if not w.void
            and (w.done or w.after == indexed or _replaced_later(w, writes))]


def _judged(landed: list[Write], indexed: str) -> tuple[str, Write | None]:
    """(state, the write it is about) for the indexed bytes of a note, given its landed writes.

    Judged against the write that PRODUCED these bytes, not merely the latest one: the index lags
    every write until the next refresh pass, so the bytes it holds are often an earlier write's.
    Newest first, the first write whose `sha256_after` is these bytes is the engine's. A write
    whose `sha256_before` is these bytes came after them, so only the writes before it can be the
    one they were edited from. With neither, the bytes are an edit nobody recorded, and the latest
    write is the closest the log can name, not proof it is the one that was edited."""
    limit = len(landed)
    for i in range(len(landed) - 1, -1, -1):
        w = landed[i]
        if w.after == indexed:
            return "engine", w
        if w.before == indexed and limit == len(landed):
            limit = i
    prior = landed[:limit]
    return ("edited", prior[-1]) if prior else ("none", None)


def provenance(log: Log | None, source_path: str | None, indexed_sha256: str | None,
               stamp: str | None | _Unread) -> dict:
    """What the log says about the indexed bytes of one note, and whether its stamp agrees.

    Found by the note's real path only. A note that moved, or whose vault did, reads `none`: the
    log has no write at that path, which is true. Matching by doc_id instead would let any note
    that declares another's id borrow its history."""
    stamped_by = None if stamp is None or stamp is UNREAD else stamp
    unchecked = None if stamp is None else "unchecked"
    if log is None or not source_path:
        return provenance_record("unknown", None, unchecked, stamped_by)
    writes = log.by_key.get(note_key(source_path), [])
    if writes and not indexed_sha256:
        return provenance_record("unknown", None, unchecked, stamped_by)
    state, last = _judged(_landed(writes, indexed_sha256), indexed_sha256)
    if stamp is None:
        verdict = None
    elif stamp is UNREAD or last is None:
        # No engine write to compare the stamp with: one from before the log, or typed by hand.
        verdict = "unchecked"
    else:
        verdict = "matches" if last.client == stamp else "contradicted"
    return provenance_record(state, last, verdict, stamped_by)


def lookup(db, doc_ids) -> dict[str, dict]:
    """The provenance of each of `doc_ids`, from the index rows on `db` (a sqlite connection), the
    log and each note. The one place both the server and `substrate query --json` compute it. An
    agent-mode server does not call it: it reads no vault and no log."""
    ids = sorted(set(doc_ids))
    if not ids:
        return {}
    rows: dict[str, tuple[str | None, str | None]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for r in db.execute(f"SELECT doc_id, source_path, note_sha256 FROM documents "
                            f"WHERE doc_id IN ({marks})", part):
            rows[r[0]] = (r[1], r[2])
    out = {}
    with CACHE.view() as log:
        for d in ids:
            if d not in rows:
                out[d] = unknown()
                continue
            source, sha = rows[d]
            out[d] = provenance(log, source, sha, stamp_of(source, sha))
    return out


def unknown() -> dict:
    """The provenance of a passage nothing was read for: an agent-mode snapshot, which reads no
    vault and no log, or a passage with no index row. Its stamp is `unchecked`, not null: nothing
    was read, which is not the same as read and found to have none."""
    return provenance_record("unknown", None, "unchecked", None)


# --------------------------------------------------------------------------- settling

@dataclass
class Check:
    """What `substrate writes check` found. `needs_attention` is THE rule for its exit status: an
    intent still unsettled, whose note is in a state the log cannot decide (neither the bytes it
    held before nor the bytes the write named). Everything else is history, reported with counts:
    damage, writes refused or that did not land, unchecked stamps, contradicted stamps, scopes not
    composed yet or whose index cannot be read."""

    log: Log
    settled: list[Write]
    did_not_land: list[Write]
    undecided: list[Write]
    contradicted: list[dict] = field(default_factory=list)
    unchecked_stamps: int = 0
    skipped_scopes: list[str] = field(default_factory=list)
    unreadable_scopes: list[str] = field(default_factory=list)

    @property
    def refused(self) -> int:
        return sum(1 for w in self.log.writes if w.void == "refused")

    @property
    def needs_attention(self) -> list[Write]:
        return self.undecided


def decide_unfinished(scope: str | None = None) -> Check:
    """Every intent with neither `done` nor `void`, decided under the log's lock, so no write is in
    flight while it runs. It landed (the note holds its `sha256_after`, or a later write replaced
    exactly those bytes): `done` is appended. It did not (a new note that is absent, or a note
    that still holds its `sha256_before`): `void` is appended. Anything else stays undecided.
    The log is read again afterwards, under the same lock, so the caller judges what was just
    written."""
    from substrate.notes import MAX_NOTE_BYTES

    with _locked():
        log = read()
        settled, lost, undecided = [], [], []
        for w in log.writes:
            if w.done or w.void or (scope is not None and w.scope != scope):
                continue
            if _replaced_later(w, log.by_key.get(w.key, [])):
                settled.append(w)
                continue
            data = read_note(w.key, MAX_NOTE_BYTES)
            now = None if data is None else sha256(data)
            if now == w.after:
                settled.append(w)
            elif (w.before is None and not os.path.lexists(w.key)) or (
                    w.before is not None and now == w.before):
                lost.append(w)
            else:
                undecided.append(w)
        if settled or lost:
            fd = _open_log()
            try:
                first = True
                for w in settled:
                    _append(fd, _terminal(w.seq, "done"), torn=log.torn and first)
                    first = False
                for w in lost:
                    _append(fd, _terminal(w.seq, "void", "did_not_land"), torn=log.torn and first)
                    first = False
            finally:
                os.close(fd)
            log = read()
    return Check(log, settled, lost, undecided)
