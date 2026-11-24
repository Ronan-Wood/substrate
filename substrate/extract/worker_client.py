"""The engine's side of the conversion worker: start it, ask it, bound it, and kill it.

One worker per converted document, reused for that document's page batches, never shared and
never kept. It leads its own session and process group, so whatever docling or torch start dies
with it. Every read and every write has a deadline on `time.monotonic()`, and stderr is drained for
the worker's whole life into a bounded ring, so no pipe can stall the engine. What the worker
sends is decoded as `convert_frames` data and nothing more.

The boundary protects the engine from a converter that hangs, crashes, floods or answers garbage,
and keeps docling out of the engine's own process. It is NOT a sandbox: the worker runs as the
user, and a document that gets code to run inside docling runs it with the user's privileges.
"""

from __future__ import annotations

import contextlib
import math
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from substrate.extract import convert_frames as frames

# Seconds. The worker's start, up to its `hello`, which includes importing docling (and torch):
# measured 2.3 to 11.7 s under load, so this bounds an interpreter that never starts rather than
# a slow one. Its time is never charged to the document.
HELLO_SECONDS = 120
# Seconds, added to the first request of a worker that loads docling's models: their load is the
# worker's, not the document's. Measured 2026-11-23 on a 1-page PDF and a 3-line image with the
# weights evicted from the page cache, at load average 6 to 8: 6.3 s for the PDF models, 6.8 to
# 8.0 s for the image's with its OCR; 5.6 to 6.6 s warm, so the disk is not the cost. 60 s is
# 7.5 times the slowest.
MODEL_LOAD_SECONDS = 60
# A document's conversion: a base, and more for a larger file, capped.
CONVERT_BASE_SECONDS = 60
CONVERT_SECONDS_PER_MIB = 10
CONVERT_CEILING_SECONDS = 900
# One batch of a PDF read with docling's models: Apple's reader's formula (`apple_arm`).
PDF_BATCH_BASE_SECONDS = 60
PDF_BATCH_SECONDS_PER_PAGE = 20
PDF_BATCH_CEILING_SECONDS = 900
# From SIGTERM to SIGKILL after a fault, and from `bye` to the worker's exit after an answer.
KILL_GRACE_SECONDS = 5
STDERR_RING = 64 * 1024

# The worker's bootstrap: put the engine's package root first on its path and run it. A constant;
# the root is an argument, never part of the code.
BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
        "from substrate.extract.worker import main; raise SystemExit(main(sys.argv[1:]))")

_STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def request_limit(limit: int, *, first: bool, loads_models: bool) -> int:
    """A request's deadline in seconds: the document's `limit`, and on the first request of a
    worker that loads docling's models, the allowance for loading them."""
    return limit + (MODEL_LOAD_SECONDS if first and loads_models else 0)


def convert_deadline(size_bytes: int) -> int:
    return min(CONVERT_BASE_SECONDS + math.ceil(CONVERT_SECONDS_PER_MIB * size_bytes / 2**20),
               CONVERT_CEILING_SECONDS)


def pdf_batch_deadline(pages: int) -> int:
    return min(PDF_BATCH_BASE_SECONDS + PDF_BATCH_SECONDS_PER_PAGE * pages, PDF_BATCH_CEILING_SECONDS)


def package_root() -> Path:
    """The folder holding the `substrate` package: the repository in a clone, site-packages in an
    install. `substrate` is a namespace package, so it is found from this file, not `__file__`."""
    return Path(__file__).resolve().parents[2]


class WorkerUnavailable(RuntimeError):
    """No worker could be started, or one started and never said hello, or said it runs another
    docling than the engine's: the engine's environment or the engine itself, never the document,
    which had not been sent. Exit 2, never quarantined."""


class WorkerFault(RuntimeError):
    """The worker hung, crashed or answered something untrustworthy while it had the document.
    `kind` is one of `conversion_quarantine.FAULTS`; `said` names what happened and the limit."""

    def __init__(self, kind: str, said: str, stderr: str = ""):
        super().__init__(said)
        self.kind, self.said, self.stderr = kind, said, stderr


class Interrupted(BaseException):
    """SIGTERM or SIGHUP reached the engine while a worker ran. Handled as Ctrl-C is."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _heeded(signals) -> list[int]:
    """The signals among `signals` this process does not ignore. One it was started ignoring
    (`nohup`, a service manager) stays ignored: the engine never turns it back into a stop."""
    return [s for s in signals if signal.getsignal(s) != signal.SIG_IGN]


@contextlib.contextmanager
def interruptible():
    """SIGTERM and SIGHUP raise `Interrupted` for the length of the block, so the worker's group
    is killed on the way out, unless this process ignores them. Scripta stops an ingest with
    SIGTERM and then kills the ingest's own process group, which the worker, in a session of its
    own, is not in."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_(signum, _frame):
        raise Interrupted(signum)

    previous = {}
    try:
        for s in _heeded((signal.SIGTERM, signal.SIGHUP)):
            previous[s] = signal.signal(s, raise_)
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


@contextlib.contextmanager
def _uninterrupted():
    """Hold Ctrl-C, SIGTERM and SIGHUP until the block is done, so a second stop cannot cut a
    cleanup short; each is raised again when it ends. The handlers are swapped rather than the
    signals masked: a mask is one thread's, and the kernel may deliver to another. An ignored one
    is left ignored."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []
    previous = {}
    try:
        for s in _heeded(_STOPS):
            previous[s] = signal.signal(s, lambda signum, _frame: held.append(signum))
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        for s in held:
            signal.raise_signal(s)


def environment(tmp: Path, models_env: dict[str, str]) -> dict[str, str]:
    """The worker's environment, built rather than inherited: nothing of the engine's reaches it
    but the locale. Every cache points into the worker's own temporary folder, and HuggingFace is
    offline, so nothing is fetched and nothing lands under the home folder."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp),
        "TMPDIR": str(tmp),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "TORCH_HOME": str(tmp / "torch"),
        "HF_HOME": str(tmp / "hf"),
        "HF_HUB_OFFLINE": "1",
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        if name in os.environ:
            env[name] = os.environ[name]
    env.update(models_env)
    return env


def _command(cpu_seconds: int, lifeline: int) -> list[str]:
    """How the worker is started: this interpreter, which in Scripta's bundle is the signed one.
    `-B` writes no bytecode into the bundle, `-E -s` ignore the environment and the user's site,
    and `-P` keeps the working directory off the path. `lifeline` is a pipe the worker's watchdog
    reads: only the engine holds its other end, so its end of file means the engine is gone."""
    if not sys.executable:
        raise WorkerUnavailable("this interpreter does not know its own executable")
    return [sys.executable, "-B", "-E", "-s", "-P", "-c", BOOT, str(package_root()),
            str(cpu_seconds), str(lifeline)]


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


class Worker:
    """One worker process. A context manager: leaving it kills the worker's group and removes
    its folder, whatever happened. `budget_seconds` is the request time this worker may use;
    `loads_models` says its first request loads docling's models (`request_limit`)."""

    def __init__(self, budget_seconds: int, models_env: dict[str, str] | None = None, *,
                 loads_models: bool = False):
        self.loads_models = loads_models
        self.cpu_seconds = (HELLO_SECONDS + request_limit(budget_seconds, first=True,
                                                          loads_models=loads_models)
                            ) * (os.cpu_count() or 1)
        self._closed = False
        try:
            self.tmp = Path(tempfile.mkdtemp(prefix="substrate-convert-"))
        except OSError as e:
            raise WorkerUnavailable(f"the conversion worker has no folder to run in: {e}") from e
        try:
            lifeline, self._lifeline = os.pipe()
        except BaseException as e:
            shutil.rmtree(self.tmp, ignore_errors=True)
            if isinstance(e, OSError):
                raise WorkerUnavailable(f"the conversion worker could not start: {e}") from e
            raise
        try:
            self.proc = subprocess.Popen(
                _command(self.cpu_seconds, lifeline), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp,
                env=environment(self.tmp, models_env or {}), start_new_session=True,
                close_fds=True, pass_fds=(lifeline,))
        except BaseException as e:
            os.close(self._lifeline)
            shutil.rmtree(self.tmp, ignore_errors=True)
            if isinstance(e, OSError):
                raise WorkerUnavailable(f"the conversion worker could not start: {e}") from e
            raise
        finally:
            os.close(lifeline)
        self.pgid = self.proc.pid
        self._read = self._write = self._drain = None
        try:
            self._out, self._in = self.proc.stdout.fileno(), self.proc.stdin.fileno()
            os.set_blocking(self._out, False)
            os.set_blocking(self._in, False)
            self._read = selectors.DefaultSelector()
            self._read.register(self._out, selectors.EVENT_READ)
            self._write = selectors.DefaultSelector()
            self._write.register(self._in, selectors.EVENT_WRITE)
            self._buf, self._scanned = bytearray(), 0
            self._ring, self._ring_lock = bytearray(), threading.Lock()
            self._next = 0
            # The thread owns the stderr pipe and closes it at its end of file. Closed from here,
            # its descriptor number could be reused while the thread still reads from it.
            self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
            self._drain.start()
        except BaseException as e:
            self._abandon()
            if isinstance(e, OSError):
                raise WorkerUnavailable(f"the conversion worker could not start: {e}") from e
            raise

    # ------------------------------------------------------------ the pipes

    def _drain_stderr(self) -> None:
        pipe = self.proc.stderr
        try:
            while chunk := pipe.read1(65536):
                with self._ring_lock:
                    self._ring += chunk
                    if len(self._ring) > STDERR_RING:
                        del self._ring[:len(self._ring) - STDERR_RING]
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                pipe.close()

    def stderr(self) -> str:
        with self._ring_lock:
            return self._ring.decode("utf-8", "replace")

    def _fault(self, kind: str, said: str) -> WorkerFault:
        return WorkerFault(kind, said, self.stderr())

    def _peek(self):
        """The worker's exit, if it has exited, without reaping it: until it is reaped its group id
        cannot be reused, so the group can still be killed safely."""
        try:
            r = os.waitid(os.P_PID, self.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return None
        return r if r is not None and r.si_pid else None

    def _ended(self, deadline: float) -> str:
        """What the worker did when its output closed, waiting for its exit until `deadline`."""
        while True:
            r = self._peek()
            if r is not None:
                if r.si_code == os.CLD_EXITED:
                    return f"exited with status {r.si_status}"
                name = _signal_name(r.si_status)
                if r.si_status == getattr(signal, "SIGXCPU", -1):
                    return f"was stopped by {name}, its CPU-time backstop of {self.cpu_seconds} s"
                return f"was killed by {name}"
            if time.monotonic() >= deadline:
                return "closed its output and did not exit"
            time.sleep(0.02)

    def _send(self, data: bytes, deadline: float, limit: int) -> None:
        view = memoryview(data)
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise self._fault("hang", f"did not read its request in {limit} s")
            if not self._write.select(left):
                continue
            try:
                view = view[os.write(self._in, view):]
            except BlockingIOError:
                continue
            except OSError:
                raise self._fault("crash", self._ended(time.monotonic() + 1)) from None

    def _fill(self, deadline: float, limit: int, *, during: str) -> None:
        """Read what the worker has sent into the buffer, or raise at the deadline or its end."""
        left = deadline - time.monotonic()
        if left <= 0:
            raise self._fault("hang", f"gave no answer in {limit} s")
        if not self._read.select(left):
            return
        try:
            chunk = os.read(self._out, 1 << 20)
        except BlockingIOError:
            return
        if not chunk:
            if during:
                raise self._fault("protocol", f"ended {during}")
            raise self._fault("crash", self._ended(time.monotonic() + 1))
        self._buf += chunk

    def _line(self, deadline: float, limit: int) -> dict:
        """One JSON line, read no further than its bound. Raises WorkerFault."""
        while True:
            newline = self._buf.find(b"\n", self._scanned, frames.MAX_LINE)
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[:newline + 1]
                self._scanned = 0
                try:
                    return frames.decode(line)
                except frames.FrameError as e:
                    raise self._fault("protocol", f"answered with a frame it could not trust "
                                                  f"({e})") from None
            self._scanned = min(len(self._buf), frames.MAX_LINE)
            if len(self._buf) >= frames.MAX_LINE:
                raise self._fault("protocol", f"sent a line over {frames.MAX_LINE} bytes")
            self._fill(deadline, limit, during="")

    def _exactly(self, n: int, deadline: float, limit: int) -> bytes:
        """The `n` bytes a header announced. Raises WorkerFault."""
        while len(self._buf) < n:
            self._fill(deadline, limit, during=f"after {len(self._buf)} of the {n} bytes it "
                                               "announced")
        with memoryview(self._buf) as view:
            data = bytes(view[:n])
        del self._buf[:n]
        return data

    def _receive(self, deadline: float, limit: int) -> dict:
        """One reply, header and payload. A `converted` reply gains `markdown`, an `items` reply
        `items`. Raises WorkerFault."""
        frame = self._line(deadline, limit)
        try:
            if frame["type"] == "converted":
                frame["markdown"] = frames.text(self._exactly(frame["bytes"], deadline, limit))
            elif frame["type"] == "items":
                frame["items"] = frames.decode_items(
                    frame["count"], self._exactly(frame["bytes"], deadline, limit))
        except frames.FrameError as e:
            raise self._fault("protocol", f"answered with a payload it could not trust ({e})") \
                from None
        return frame

    # ------------------------------------------------------------ the conversation

    def start(self) -> dict:
        """Wait for the worker's `hello`. Raises WorkerUnavailable: the document has not been sent,
        so nothing that goes wrong here is the document's."""
        from substrate.extract.convert import printable

        try:
            frame = self._receive(time.monotonic() + HELLO_SECONDS, HELLO_SECONDS)
        except WorkerFault as e:
            tail = printable(e.stderr).strip()[-2000:]
            raise WorkerUnavailable(
                f"the conversion worker {printable(e.said)} before it was given the file"
                + (f"\nIts last words:\n    {tail}" if tail else "")) from None
        if frame["type"] != "hello":
            raise WorkerUnavailable(f"the conversion worker opened with a {frame['type']} frame, "
                                    "not hello")
        return frame

    def request(self, frame: dict, limit: int, expect: tuple[str, ...]) -> dict:
        """Send one request and return its reply, which must be one of `expect` with the request's
        id. Raises WorkerFault, or FrameError for a request the worker would not accept, which is
        the engine's own and never the worker's fault."""
        if self._buf:
            raise self._fault("protocol", "said more after its answer")
        limit = request_limit(limit, first=self._next == 0, loads_models=self.loads_models)
        self._next += 1
        frame = {"v": frames.PROTOCOL, "id": self._next, **frame}
        data = frames.encode(frame)
        frames.decode(data[:-1])
        deadline = time.monotonic() + limit
        self._send(data, deadline, limit)
        reply = self._receive(deadline, limit)
        if reply["type"] not in expect or reply.get("id") != frame["id"]:
            raise self._fault("protocol", f"answered a {frame['type']} with a {reply['type']} "
                                          f"frame for request {reply.get('id')}")
        return reply

    def finish(self) -> None:
        """End a worker that answered: `bye`, then its output must close with nothing more on it
        and it must exit 0 within the grace period, or nothing it said is used. Raises WorkerFault."""
        deadline = time.monotonic() + KILL_GRACE_SECONDS
        self._send(frames.encode({"v": frames.PROTOCOL, "type": "bye"}), deadline,
                   KILL_GRACE_SECONDS)
        while True:
            if self._buf:
                raise self._fault("protocol", "said more after its answer")
            left = deadline - time.monotonic()
            if left <= 0:
                raise self._fault("hang", f"did not exit within {KILL_GRACE_SECONDS} s of its "
                                          "answer")
            if not self._read.select(left):
                continue
            try:
                chunk = os.read(self._out, 1 << 20)
            except BlockingIOError:
                continue
            if not chunk:
                break
            self._buf += chunk
        ended = self._ended(deadline)
        if ended != "exited with status 0":
            raise self._fault("crash", f"{ended} after its answer")

    # ------------------------------------------------------------ the end

    def _abandon(self) -> None:
        """Undo a start that failed after the spawn: kill the group, reap the worker, close every
        descriptor this side holds, remove the folder."""
        with _uninterrupted():
            self._closed = True
            try:
                self._killpg(signal.SIGKILL)
            finally:
                with contextlib.suppress(OSError):
                    os.close(self._lifeline)
                self.proc.wait()
                # A running drain thread owns stderr, as in `close`; one that never started does not.
                draining = self._drain is not None and self._drain.is_alive()
                for f in (self.proc.stdin, self.proc.stdout) + (() if draining else
                                                                (self.proc.stderr,)):
                    with contextlib.suppress(OSError):
                        f.close()
                if draining:
                    self._drain.join(timeout=1.0)
                for selector in (self._read, self._write):
                    if selector is not None:
                        selector.close()
                shutil.rmtree(self.tmp, ignore_errors=True)

    def _killpg(self, sig: int) -> None:
        # ESRCH: nothing is left. EPERM: macOS says so for a group holding only zombies.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pgid, sig)

    def close(self, *, gently: bool) -> None:
        """Kill the worker's group, reap the worker, and remove its folder. `gently` gives a
        faulted worker SIGTERM and the grace period first; otherwise the group gets SIGKILL now.
        A stop that arrives meanwhile waits until this is done."""
        if self._closed:
            return
        with _uninterrupted():
            self._closed = True
            try:
                if gently and self._peek() is None:
                    self._killpg(signal.SIGTERM)
                    deadline = time.monotonic() + KILL_GRACE_SECONDS
                    while self._peek() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
            finally:
                # Before the worker is reaped, so its group id cannot have been reused.
                self._killpg(signal.SIGKILL)
                with contextlib.suppress(OSError):
                    os.close(self._lifeline)
                self.proc.wait()
                for f in (self.proc.stdin, self.proc.stdout):
                    with contextlib.suppress(OSError):
                        f.close()
                self._drain.join(timeout=1.0)
                self._read.close()
                self._write.close()
                shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, kind, value, _tb) -> None:
        # A fault gets the grace period; an interrupt or any other exception does not wait.
        self.close(gently=kind is not None and issubclass(kind, WorkerFault))
