"""The private copy of a source that an ingest reads, converts and stores: one read of the file.

`substrate ingest` copies a PDF or a converted document into a private folder while hashing it, and
every reader, the worker, the coverage probes and the blob store take the copy. So the checksum,
the markdown and the stored original are of the same bytes, whatever happens to the file meanwhile.

The source is opened once, non-blocking. Its path is resolved first, so a link the user named is
followed; after that, no link is followed anywhere in it on macOS (`O_NOFOLLOW_ANY`), and on Linux
none in its last component and, where `/proc` names it, the descriptor must be at the resolved
path. It must be a regular file, and no more is read than its size when it was opened: a FIFO in
its place cannot block the read. A file whose size or modification time moves while it is copied
is refused; one changed after is converted as it was read, and the ingest warns
(`Snapshot.moved_on`).

The copy is 0400 in a 0700 folder under the temporary folder: `substrate-source-…/copy/<name>`,
beside a `.lock` held for as long as its ingest runs. The folder is made under another name and
takes that name only once its lock is held, so no ingest's sweep can mistake a new one for a dead
one. It is removed when the ingest ends, by a SIGTERM or SIGHUP too; one left by an ingest killed
outright is removed by the next, whose lock on it then succeeds.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

PREFIX = "substrate-source-"
_STAGING = "substrate-staging-"
_LOCK = ".lock"
_COPY = "copy"
# A staging folder takes its final name within moments; one this old was left by a killed ingest.
_STAGED_FOR = 3600
# macOS refuses a link anywhere in the path; the two flags together are EINVAL there.
_NOFOLLOW = os.O_NOFOLLOW_ANY if sys.platform == "darwin" else os.O_NOFOLLOW


class SourceUnreadable(OSError):
    """The source could not be read as a regular file: an INPUT problem (exit 2)."""


class CopyFailed(OSError):
    """The engine could not write its private copy (its temporary folder is full, say): the
    engine's environment, never the document (exit 2)."""


@dataclass(frozen=True)
class Snapshot:
    path: Path          # the private copy, under the name the user gave the source
    checksum: str       # sha256 of the copy, taken as it was written
    source: Path
    size: int
    mtime_ns: int

    def moved_on(self) -> bool:
        """Whether the file at `source` looks different now from when it was read, by its size and
        modification time: a file still being written when it was copied. A stat, never a read."""
        try:
            st = os.stat(self.source)
        except OSError:
            return True
        return (st.st_size, st.st_mtime_ns) != (self.size, self.mtime_ns)


def _sweep(tmp: Path) -> None:
    """Remove the copies of ingests that ended without removing their own: one whose lock this
    takes, or whose lock is gone, since a running ingest's folder holds its lock from the moment it
    has its name; and a staging folder `_STAGED_FOR` old. Nothing here can fail an ingest."""
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        staged = name.startswith(_STAGING)
        if not (staged or name.startswith(PREFIX)):
            continue
        path = tmp / name
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                continue
            if staged:
                if time.time() - st.st_mtime < _STAGED_FOR:
                    continue
            elif not _unlocked(path / _LOCK):
                continue
        except OSError:
            continue
        with contextlib.suppress(OSError):
            shutil.rmtree(path)


def _unlocked(lock: Path) -> bool:
    """Whether no ingest holds `lock`: it is gone, or this takes it. Raises OSError otherwise."""
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False                                    # its ingest is still running
    finally:
        os.close(fd)
    return True


def _remove(folder: Path) -> None:
    """The copy first, so a removal cut short leaves the lock for the next sweep to take."""
    shutil.rmtree(folder / _COPY, ignore_errors=True)
    shutil.rmtree(folder, ignore_errors=True)


@contextlib.contextmanager
def _removed_on_stop(folder: Path) -> Iterator[None]:
    """For the block, a SIGTERM or SIGHUP removes `folder` and then ends the process as the
    default action does. A signal the process was started ignoring stays ignored, and a handler
    set inside the block (the conversion's) takes over until it restores this one."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, _frame):
        _remove(folder)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = {}
    try:
        for s in (signal.SIGTERM, signal.SIGHUP):
            # None is a handler set outside Python, which could not be put back: it is left alone.
            if signal.getsignal(s) not in (signal.SIG_IGN, None):
                previous[s] = signal.signal(s, stop)
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def _opened_at(fd: int) -> str | None:
    """The path the kernel has for `fd` where it says (Linux's `/proc`), or None."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _copy(fd: int, size: int, out: int, name: str) -> str:
    """Copy `size` bytes from `fd` to `out`, hashing them. Raises SourceUnreadable or CopyFailed."""
    from substrate.blobs import _write_all

    digest, left = hashlib.sha256(), size
    while True:
        try:
            block = os.read(fd, min(1 << 20, left + 1))
        except OSError as e:
            raise SourceUnreadable(f"{name} could not be read: {e.strerror or e}.") from None
        if not block:
            break
        if len(block) > left:
            raise SourceUnreadable(f"{name} grew while it was being read; ingest it again once "
                                   "nothing is writing to it.")
        left -= len(block)
        digest.update(block)
        try:
            _write_all(out, block)
        except OSError as e:
            raise _copy_failed(name, e) from None
    if left:
        raise SourceUnreadable(f"{name} shrank while it was being read; ingest it again once "
                               "nothing is writing to it.")
    return digest.hexdigest()


def _copy_failed(name: str, e: OSError) -> CopyFailed:
    return CopyFailed(f"the engine could not make its private copy of {name}: {e.strerror or e}. "
                      "The copy needs as much free space in the temporary folder as the file; "
                      "TMPDIR names another folder.")


def _opened(src: Path, real: Path) -> tuple[int, os.stat_result]:
    """`real` opened once, non-blocking, as a regular file at that path. Raises SourceUnreadable."""
    try:
        fd = os.open(real, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError as e:
        raise SourceUnreadable(f"{src.name} could not be read: {e.strerror or e}.") from None
    try:
        at = _opened_at(fd)
        if at is not None and at != str(real):
            raise SourceUnreadable(f"{src.name} moved while it was being opened; ingest it again "
                                   "once nothing is moving it.")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SourceUnreadable(f"{src.name} is not a regular file.")
        # Non-blocking was for the open alone: a network or file-provider read may otherwise
        # answer EAGAIN for an ordinary file.
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except OSError as e:
        os.close(fd)
        if isinstance(e, SourceUnreadable):
            raise
        raise SourceUnreadable(f"{src.name} could not be read: {e.strerror or e}.") from None
    return fd, st


def _folder(tmp: Path, stack: contextlib.ExitStack, name: str) -> Path:
    """A new private folder, locked, with its removal on `stack`, a stop's included."""
    try:
        staging = Path(tempfile.mkdtemp(prefix=_STAGING, dir=tmp))
    except OSError as e:
        raise _copy_failed(name, e) from None
    try:
        lock = os.open(staging / _LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise _copy_failed(name, e) from None
    stack.callback(os.close, lock)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        folder = tmp / (PREFIX + staging.name.removeprefix(_STAGING))
        os.rename(staging, folder)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise _copy_failed(name, e) from None
    stack.enter_context(_removed_on_stop(folder))
    stack.callback(_remove, folder)                     # before the handlers are put back
    try:
        (folder / _COPY).mkdir(mode=0o700)
    except OSError as e:
        raise _copy_failed(name, e) from None
    return folder


def _written(fd: int, size: int, dest: Path, name: str) -> str:
    """`dest` written with `size` bytes from `fd`, read only, hashed. Returns the checksum."""
    try:
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise _copy_failed(name, e) from None
    try:
        checksum = _copy(fd, size, out, name)
        try:
            os.fchmod(out, 0o400)
        except OSError as e:
            raise _copy_failed(name, e) from None
    finally:
        os.close(out)
    return checksum


@contextlib.contextmanager
def snapshot(src: Path) -> Iterator[Snapshot]:
    """Copy `src` once into a private folder, hashing it as it is copied. Raises SourceUnreadable
    or CopyFailed. The folder is removed when the block ends, and by a SIGTERM or SIGHUP from the
    moment it has its name."""
    real = src.resolve()
    fd, st = _opened(src, real)
    with contextlib.ExitStack() as stack:
        try:
            try:
                tmp = Path(tempfile.gettempdir())
            except OSError as e:
                raise _copy_failed(src.name, e) from None
            _sweep(tmp)
            dest = _folder(tmp, stack, src.name) / _COPY / src.name
            checksum = _written(fd, st.st_size, dest, src.name)
            # A write in place while the copy was made leaves the size alone but not the time, and
            # the copy may then hold bytes the file never held at once.
            after = os.fstat(fd)
            if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise SourceUnreadable(f"{src.name} was written to while it was being read; "
                                       "ingest it again once nothing is writing to it.")
        finally:
            os.close(fd)
        yield Snapshot(dest, checksum, src, st.st_size, st.st_mtime_ns)
