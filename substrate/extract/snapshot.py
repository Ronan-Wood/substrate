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

The copy is 0400 in a 0700 folder of the engine's own, `<temp>/substrate-<uid>/snap-…/copy/`,
under the source's name, beside a `.lock` held for as long as its ingest runs. It is removed when
the ingest ends, by a SIGTERM or SIGHUP too. One left by an ingest killed outright is removed by
the next ingest's sweep, which looks only inside `substrate-<uid>` and removes only what proves it
is a snapshot (`_abandoned`). A folder killed before its proof was written stays until the system
clears its temporary folder: an empty folder, or one holding an empty lock, never a copy.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_ENTRY = "snap-"
# The names `mkdtemp` gives an entry: the prefix and 8 of its characters.
_ENTRY_NAME = re.compile(r"snap-[a-z0-9_]{8}")
_LOCK = ".lock"
_COPY = "copy"
# Written into the lock once it is held: what a sweep reads as "a snapshot, and its ingest ended".
_MARK = b"substrate snapshot 1\n"
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
    dev: int            # of the file the descriptor opened
    ino: int

    def moved_on(self) -> bool:
        """Whether the file at `source` looks different now from when it was read: another size or
        modification time (a file still being written), or another file at that path. A stat,
        never a read. A save that writes a new file of the same bytes counts as another file; the
        result is a warning only, so that spurious one is accepted."""
        try:
            st = os.stat(self.source)
        except OSError:
            return True
        return ((st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
                != (self.size, self.mtime_ns, self.dev, self.ino))


def _private_root(tmp: Path) -> Path:
    """The engine's own folder under `tmp`: made 0700 if it is missing, and checked each time to be
    a folder, not a link, this user's and closed to everyone else. Raises CopyFailed."""
    root = tmp / f"substrate-{os.geteuid()}"
    try:
        os.mkdir(root, 0o700)
        os.chmod(root, 0o700)                           # whatever the umask did to the mode
    except FileExistsError:
        pass
    except OSError as e:
        raise _temp_unusable(str(tmp), e) from None
    try:
        st = os.lstat(root)
    except OSError as e:
        raise _temp_unusable(str(tmp), e) from None
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) & 0o077):
        # Another user's is not this user's to remove: on a shared /tmp, TMPDIR is the way out.
        way_out = "Remove it, or set" if st.st_uid == os.geteuid() else "Set"
        raise CopyFailed(f"the engine's temporary folder {root} is not its own: it must be a "
                         "folder, not a link, owned by this user and closed to others. "
                         f"{way_out} TMPDIR to a folder of your own.")
    return root


def _sweep(root: Path) -> None:
    """Remove the copies in `root` of ingests that ended without removing their own: each entry
    `_abandoned` proves is one. Anything else is left alone. Nothing here can fail an ingest."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not _ENTRY_NAME.fullmatch(name):
            continue
        path = root / name
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                continue
            if not _abandoned(path / _LOCK):
                continue
        except OSError:
            continue
        _remove(path)


def _abandoned(lock: Path) -> bool:
    """Whether `lock` is a snapshot's whose ingest has ended: a regular file, not a link, holding
    exactly `_MARK`, which an ingest writes only once it holds the lock, and free for this to take.
    A missing, empty or other lock is not one. Raises OSError where it cannot tell."""
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.read(fd, len(_MARK) + 1) != _MARK:
            return False
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False                                    # its ingest is still running
    finally:
        os.close(fd)
    return True


def _remove(folder: Path) -> None:
    """The copy first, so a removal cut short leaves the lock and its mark for the next sweep."""
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
            raise _unreadable(name, e) from None
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
    room = (" The copy needs as much free space in the temporary folder as the file; TMPDIR "
            "names another folder.") if e.errno in (errno.ENOSPC, errno.EDQUOT) else ""
    return CopyFailed(f"the engine could not make its private copy of {name}: "
                      f"{e.strerror or e}.{room}")


def _temp_unusable(where: str, e: OSError) -> CopyFailed:
    return CopyFailed(f"the engine cannot use the temporary folder {where}: {e.strerror or e}. "
                      "TMPDIR names another folder.")


def _unreadable(name: str, e: OSError) -> SourceUnreadable:
    return SourceUnreadable(f"{name} could not be read: {e.strerror or e}.")


def _opened(src: Path) -> tuple[int, os.stat_result]:
    """`src` resolved and opened once, non-blocking, as a regular file at the resolved path.
    Raises SourceUnreadable."""
    try:
        real = src.resolve()
        fd = os.open(real, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError as e:
        raise _unreadable(src.name, e) from None
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
        _close_quietly(fd)
        if isinstance(e, SourceUnreadable):
            raise
        raise _unreadable(src.name, e) from None
    return fd, st


def _close_quietly(fd: int) -> None:
    """For a descriptor whose close can lose nothing: one read from, the lock's, or a copy's being
    thrown away."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _folder(root: Path, stack: contextlib.ExitStack, name: str) -> Path:
    """A new private folder in `root`, locked and marked, with its removal on `stack` and on a
    stop from the moment it exists."""
    from substrate.blobs import _write_all

    try:
        folder = Path(tempfile.mkdtemp(prefix=_ENTRY, dir=root))
    except OSError as e:
        raise _copy_failed(name, e) from None
    stack.enter_context(_removed_on_stop(folder))
    try:
        lock = os.open(folder / _LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        _remove(folder)
        raise _copy_failed(name, e) from None
    # Unwound in reverse: the folder is removed while its lock is held, then the lock let go, then
    # the stop handlers put back.
    stack.callback(_close_quietly, lock)
    stack.callback(_remove, folder)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_all(lock, _MARK)
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
    except BaseException:
        _close_quietly(out)
        raise
    try:
        os.close(out)                                   # where a write can still fail to land
    except OSError as e:
        raise _copy_failed(name, e) from None
    return checksum


@contextlib.contextmanager
def snapshot(src: Path) -> Iterator[Snapshot]:
    """Copy `src` once into a private folder, hashing it as it is copied. Raises SourceUnreadable
    or CopyFailed and no other OSError: any other is reported as CopyFailed. The folder is removed
    when the block ends, and by a SIGTERM or SIGHUP from the moment it exists."""
    with contextlib.ExitStack() as stack:
        try:
            snap = _taken(src, stack)
        except (SourceUnreadable, CopyFailed):
            raise
        except OSError as e:
            raise _copy_failed(src.name, e) from None
        yield snap


def _taken(src: Path, stack: contextlib.ExitStack) -> Snapshot:
    """The snapshot of `src`, its folder's removal on `stack`."""
    fd, st = _opened(src)
    try:
        try:
            tmp = Path(tempfile.gettempdir())
        except OSError as e:
            raise CopyFailed(f"the engine found no temporary folder it can write to: "
                             f"{e.strerror or e}. TMPDIR names one.") from None
        root = _private_root(tmp)
        _sweep(root)
        dest = _folder(root, stack, src.name) / _COPY / src.name
        checksum = _written(fd, st.st_size, dest, src.name)
        # A write in place while the copy was made leaves the size alone but not the time, and
        # the copy may then hold bytes the file never held at once.
        try:
            after = os.fstat(fd)
        except OSError as e:
            raise _unreadable(src.name, e) from None
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise SourceUnreadable(f"{src.name} was written to while it was being read; "
                                   "ingest it again once nothing is writing to it.")
    finally:
        _close_quietly(fd)
    return Snapshot(dest, checksum, src, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
