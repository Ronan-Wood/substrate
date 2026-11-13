"""Agent mode — a scope served to ANOTHER macOS user, from a copy that user cannot write.

WHY. A second account (`atlas-agent`) runs its own agents and should be able to ask the operator's
`atlas` scope questions — and nothing else. It cannot reach the operator's home folder, so it cannot
read the registry, the live index, or a single vault note, and it must not be able to: the registry
names every scope, and the vaults hold all of them.

WHY A SNAPSHOT RATHER THAN THE LIVE INDEX. The live index is a WAL-mode SQLite file, and a WAL
reader is not read-only — it creates `-shm`/`-wal` beside the database, so a user who cannot write
that directory cannot open it at all, and one who could would be writing into the operator's index.
So the operator's own engine PUBLISHES: after a refresh of an allowed scope succeeds it writes
`<scope>.snapshot.db` with `VACUUM INTO` into a shared folder, plus a small manifest, and the agent
opens that file `immutable=1` (`IndexStore(immutable=True)`), touching nothing beside it.

THE SNAPSHOT CARRIES WHAT THE AGENT CANNOT FETCH. The index holds passages, not notes — `expand
mode=note` reads the note from the vault — so the publisher adds a `snapshot_notes` table with every
note's text as it stood at publish time. It also strips what would leak the operator's layout:
absolute source paths become vault-relative.

CALLS STAY OUT UNLESS THE OPERATOR LETS THEM IN. A conversation-class document (a call transcript)
is REMOVED from the snapshot, not filtered — passages, outlines, vectors, entity mentions and note
text — unless its `[publish.<scope>]` entry sets `include_sources = true`. Retrieval withholds
calls by default for accuracy, and a caller lifts that with a flag; across a uid boundary the
operator decides instead, per scope, and the manifest records what was decided.

THE ENFORCEMENT IS SERVER-SIDE. `--scopes` is the whole world in agent mode: any other name gets
the same fixed "scope not available", which names nothing, so the error cannot be used to list what
else exists. A snapshot path is built from an allowed name only — never from a caller's argument —
so there is no path for a `../` to travel into.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from substrate import private, refresh_state, scopes

SNAPSHOT_SUFFIX = ".snapshot.db"
MANIFEST_SUFFIX = ".snapshot.json"
MANIFEST_VERSION = 1
CONFIG_FILENAME = "agent-publish.toml"
# Every folder an entry has named for a scope, kept beside the registry: deleting the entry deletes
# the only record of where its snapshot went, and a snapshot nobody records is never withdrawn.
LEDGER_FILENAME = "agent-published.json"
LEDGER_VERSION = 1

# Older than this and the snapshot says so on every answer. A day: the in-app refresh runs only
# while Scripta is open (substrate/CLAUDE.md), so an older copy means nobody has refreshed, and the
# agent's reader should know that rather than assume it.
DEFAULT_MAX_AGE_SECONDS = 24 * 3600

# Readable by the folder's GROUP, never by every local user. Files created in a directory take its
# group on macOS, so the operator decides who reads the snapshot by owning the folder and setting
# its group — this code never chmods the folder itself.
FILE_MODE = 0o640

# ONE string for every refusal of a name outside `--scopes`, and it names nothing. An error that
# said "unknown scope 'x'; registered: acme, school, …" is how the registry's own refusal reads,
# and in agent mode that sentence is the leak.
UNAVAILABLE = "scope not available"

# The size bound `expand` and `ingest` put on one note, applied to every note a snapshot copies.
MAX_NOTE_BYTES = 8 * 1024 * 1024

_SCOPE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

_SNAPSHOT_DDL = """
CREATE TABLE snapshot_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE snapshot_notes(
    doc_id TEXT PRIMARY KEY,
    -- vault-relative: `<vault>/<path within it>`, never the operator's absolute path.
    path   TEXT NOT NULL,
    text   TEXT NOT NULL,
    -- 0 when the note matched the index at publish time; NULL when it declares a checksum and the
    -- match cannot be judged. A note that had moved on is never here: it is excluded.
    stale  INTEGER
);
"""


class AgentError(RuntimeError):
    """A condition the caller can act on. Messages name only the scope that was asked for."""


def valid_scope_name(name: object) -> bool:
    return isinstance(name, str) and _SCOPE_NAME.fullmatch(name) is not None


def snapshot_paths(directory: Path, scope: str) -> tuple[Path, Path]:
    if not valid_scope_name(scope):
        raise AgentError(UNAVAILABLE)
    return directory / f"{scope}{SNAPSHOT_SUFFIX}", directory / f"{scope}{MANIFEST_SUFFIX}"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _read_manifest(path: Path) -> tuple[dict | None, str | None]:
    """The manifest, or `(None, reason)`. Bounded and fstat'd like the refresh record, because it
    is parsed on every agent call and echoed into the envelope."""
    from substrate.freshness import read_bounded

    if not path.is_file():
        return None, None
    try:
        # NOFOLLOW: the folder is shared, and a manifest planted as a symlink would otherwise have
        # the operator's publisher read a file of the operator's (`annotate` rewrites what it
        # read) into a folder another user can read.
        data = json.loads(read_bounded(path, refresh_state.MAX_STATE_BYTES,
                                       nofollow=True).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — any unreadable manifest is one state, reported
        return None, f"the snapshot manifest could not be read ({type(e).__name__})"
    if not isinstance(data, dict):
        return None, "the snapshot manifest is not in the expected shape"
    return data, None


def _write_atomic(path: Path, body: bytes, mode: int = FILE_MODE) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- publisher (operator)

def _trusted_dir(directory: Path) -> None:
    """Refuse a publish folder another local user could redirect or read into.

    The publisher runs as the OPERATOR and writes into /Users/Shared, which every local user can
    write. So the folder must be a real directory (not a symlink) owned by the operator and not
    writable by anyone else, and every folder on the way to it — each link's folder included, one
    hop at a time (`private.untrusted`) — must be owned by root or the operator and, if others can
    write it, be a root-owned sticky directory like /Users/Shared itself. Otherwise whoever
    created `/Users/Shared/atlas-agent` first could swap the folder for one whose group they
    choose, plant a snapshot the agent would serve, or point the manifest at an operator-only file.
    """
    uid = os.geteuid()
    path = private.absolute(directory)  # `link/..` is the kernel's, not the spelling's
    try:
        written = os.lstat(path)
    except OSError as e:
        raise AgentError(f"{directory} does not exist. Create it first — who may read a "
                         f"snapshot is decided by that folder's owner and group, so it is never "
                         f"created here.") from e
    not_a_folder = (f"{directory} must be a real directory owned by you and writable by nobody "
                    f"else; refusing to publish into it.")
    if stat.S_ISLNK(written.st_mode):
        raise AgentError(not_a_folder)
    r = private.untrusted(path)
    if r.why:
        raise AgentError(f"{r.why}, so {directory} could be redirected; refusing to publish into "
                         f"it.")
    vanished = f"{directory} disappeared while it was checked; refusing to publish into it."
    if not r.exists:
        raise AgentError(vanished)
    try:
        st = os.lstat(r.path)  # the folder the checked resolution names
    except OSError as e:
        raise AgentError(vanished) from e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022:
        raise AgentError(not_a_folder)


@dataclass(frozen=True)
class Target:
    """Where one scope is published, and whether its snapshot carries the scope's sources."""

    dir: Path
    include_sources: bool = False


@dataclass(frozen=True)
class Broken:
    """A `[publish.<scope>]` entry that cannot be used, and why. It is never published, and a copy
    it left that may hold calls is withdrawn (`withdraw_calls`). `dir` is None when the entry
    names no usable folder."""

    dir: Path | None
    error: str


@dataclass(frozen=True)
class Config:
    """`agent-publish.toml`: the entries that can be published, and the ones that cannot."""

    targets: dict[str, Target]
    broken: dict[str, Broken]


def _entry_dir(raw: object) -> Path | str:
    """The entry's folder, or why it is not one. Absolute only: a relative `dir` would mean
    whichever folder the refresh happened to run in."""
    if not isinstance(raw, str) or not raw.strip():
        return "needs a `dir`"
    if "\0" in raw:
        return "has a `dir` holding a NUL"
    try:
        folder = Path(raw).expanduser()
    except (RuntimeError, ValueError) as e:  # `~nobody`, or no home to expand `~` to
        return f"has a `dir` that cannot be expanded ({e})"
    if not folder.is_absolute():
        return "needs an absolute `dir`"
    return folder


def load_config(registry: str | Path | None = None) -> Config:
    """The entries of `agent-publish.toml` beside the registry; empty when it is absent.

        [publish.atlas]
        dir = "/Users/Shared/atlas-agent/substrate"
        include_sources = true      # optional; false leaves every call out of the snapshot

    Opt-in by file, like the registry itself: no file, no publishing, and nothing under
    `/Users/Shared` is ever written by a machine that did not ask for it.

    A malformed ENTRY is that entry's alone (`Config.broken`): one bad value written while turning
    an opt-in off must not stop every other scope's publish and withdrawal. A file that cannot be
    read at all still raises.
    """
    path = scopes.registry_path(registry).parent / CONFIG_FILENAME
    if not path.is_file():
        return Config({}, {})
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ConfigUnknown(f"cannot read {path}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise AgentError(f"cannot read {path}: {e}") from e
    table = data.get("publish", {})
    if not isinstance(table, dict):
        raise AgentError(f"{path}: 'publish' must be a table of scopes.")
    targets: dict[str, Target] = {}
    broken: dict[str, Broken] = {}
    for name, row in table.items():
        folder = _entry_dir(row.get("dir") if isinstance(row, dict) else None)
        usable = folder if isinstance(folder, Path) else None
        if not valid_scope_name(name) or not isinstance(row, dict):
            broken[name] = Broken(usable, f"{path}: [publish.{name}] needs a valid scope name "
                                          f"and a `dir`.")
        elif usable is None:
            broken[name] = Broken(None, f"{path}: [publish.{name}] {folder}.")
        elif not isinstance(include := row.get("include_sources", False), bool):
            broken[name] = Broken(usable, f"{path}: [publish.{name}] include_sources must be "
                                          f"true or false.")
        else:
            targets[name] = Target(usable, include)
    return Config(targets, broken)


# --------------------------------------------------------------------------- withdrawal

def _ledger_path(registry: str | Path | None) -> Path:
    return scopes.registry_path(registry).parent / LEDGER_FILENAME


class LedgerUnreadable(AgentError):
    """The ledger could not be read at all (EIO, EACCES, over the size cap, a planted link): what
    it holds is unknown, so nothing is decided from it and nothing is written over it."""


def _valid_row(name: object, folders: object) -> bool:
    return valid_scope_name(name) and isinstance(folders, list) and all(
        isinstance(d, str) and "\0" not in d and Path(d).is_absolute() for d in folders)


def _read_ledger(path: Path) -> tuple[dict[str, list[str]], str | None]:
    """`(rows, why)`. `why` names bad content — not JSON, not a version-1 ledger, invalid rows —
    and `rows` are then the valid rows it still holds. `({}, None)` when there is no file. Raises
    LedgerUnreadable when the file cannot be read."""
    from substrate.freshness import read_bounded

    try:
        raw = read_bounded(path, refresh_state.MAX_STATE_BYTES, nofollow=True)
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        raise LedgerUnreadable(f"{path} cannot be read ({type(e).__name__}: "
                               f"{e.strerror or e}); nothing was withdrawn from it this pass, "
                               f"and it was left as it is") from e
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}, f"{path} is not JSON"
    published = data.get("published") if isinstance(data, dict) else None
    if not isinstance(published, dict):
        return {}, f"{path} holds no table of published folders"
    rows = {k: list(v) for k, v in published.items() if _valid_row(k, v)}
    if data.get("version") != LEDGER_VERSION:
        return rows, f"{path} is not a version-{LEDGER_VERSION} ledger"
    if len(rows) != len(published):
        return rows, f"{path} holds {len(published) - len(rows)} invalid row(s)"
    return rows, None


def _write_ledger(path: Path, ledger: dict[str, list[str]]) -> None:
    ledger = {k: v for k, v in sorted(ledger.items()) if v}
    _write_atomic(path, (json.dumps({"version": LEDGER_VERSION, "published": ledger}, indent=2)
                         + "\n").encode(), mode=0o600)


def _load_ledger(path: Path) -> tuple[dict[str, list[str]], str | None]:
    """The ledger's valid rows, under the ledger lock. A file with bad content is set aside whole,
    beside itself, and its valid rows are written back, so a good row is never lost and the bad
    content stays for the operator to read; `why` then says so. Raises LedgerUnreadable."""
    rows, why = _read_ledger(path)
    if why:
        aside = path.with_name(f"{path.name}.bad-{_now():%Y%m%dT%H%M%S}-{os.getpid()}")
        os.replace(path, aside)
        _write_ledger(path, rows)
        why = (f"{why}; set aside as {aside.name}, and its {sum(map(len, rows.values()))} valid "
               f"folder(s) kept")
    return rows, why


def _remember(scope: str, directory: Path, registry: str | Path | None) -> str | None:
    """Record that `scope` is published into `directory`: called under the scope's publish lock,
    before the first snapshot file is written there, so a copy is never in a folder the ledger
    does not name. Returns a note when bad content was set aside. Raises LedgerUnreadable: a row
    that cannot be written is a copy that could never be withdrawn."""
    path = _ledger_path(registry)
    folder = str(private.absolute(directory))
    with scopes.exclusive(path):
        ledger, why = _load_ledger(path)
        if folder not in ledger.get(scope, []):
            ledger.setdefault(scope, []).append(folder)
            _write_ledger(path, ledger)
    return why


def _forget(scope: str, folder: str, registry: str | Path | None) -> None:
    path = _ledger_path(registry)
    with scopes.exclusive(path):
        ledger, _ = _load_ledger(path)
        if folder in ledger.get(scope, []):
            ledger[scope].remove(folder)
            _write_ledger(path, ledger)


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.abspath(a) == os.path.abspath(b)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


# What `_withdraw` found.
ABSENT, EMPTY, KEPT, REMOVED = "absent", "empty", "kept", "removed"


def _withdraw(scope: str, directory: Path, *, unless_call_free: bool,
              live: Iterable[tuple[str, Path]] = ()) -> str:
    """Remove `scope`'s snapshot and manifest from `directory`, under the caller's publish lock.

    ABSENT: the folder could not be looked in (gone, an unmounted volume, no permission). EMPTY:
    it holds neither name, or what it holds under them is a LIVE entry's own file — `live` is each
    healthy entry's `(scope, dir)`, and on a volume that ignores case `Demo.snapshot.db` is
    `demo.snapshot.db`; judged by file identity, so a volume that does not ignore case still has
    its own copy withdrawn. KEPT: `unless_call_free`, and the copy's manifest records
    `include_sources: false` (`_withdraw_if_sources_revoked`). REMOVED: removed. A folder holding
    a snapshot name that fails `_trusted_dir` raises AgentError: another user could have pointed
    it anywhere."""
    db_path, manifest_path = snapshot_paths(directory, scope)
    try:
        os.stat(directory)
    except OSError:
        return ABSENT
    present = []
    for p in (db_path, manifest_path):
        try:
            os.lstat(p)
        except FileNotFoundError:
            continue
        except OSError:
            return ABSENT
        present.append(p)
    theirs = [q for name, d in live if name != scope and valid_scope_name(name)
              for q in snapshot_paths(d, name)]
    if not present or any(_same_file(p, q) for p in present for q in theirs):
        return EMPTY
    _trusted_dir(directory)
    if unless_call_free:
        prior, _ = _read_manifest(manifest_path)
        return (REMOVED if _withdraw_if_sources_revoked(False, db_path, manifest_path, prior)
                else KEPT)
    for p in present:
        p.unlink(missing_ok=True)
    return REMOVED


def withdraw_calls(scope: str, folders: Iterable[Path],
                   registry: str | Path | None) -> tuple[list[Path], list[str]]:
    """For an entry that cannot be read: withdraw a copy in each of `folders` that may hold calls.
    `(folders withdrawn from, failures)`."""
    removed, failed = [], []
    if not valid_scope_name(scope):
        return removed, failed
    with _publish_lock(scope, registry):
        for folder in dict.fromkeys(folders):
            try:
                if _withdraw(scope, folder, unless_call_free=True) == REMOVED:
                    removed.append(folder)
            except (AgentError, OSError, ValueError) as e:
                failed.append(f"{scope}: snapshot in {folder} not checked for calls: {e}")
    return removed, failed


class ConfigUnknown(AgentError):
    """`agent-publish.toml` cannot be read right now: missing, not a regular file, or unreadable.
    An editor saving it can leave any of these for a moment, so it is not "no entries"."""


def _config_now(registry: str | Path | None) -> Config | None:
    """The entries as they stand, or None when the file does not parse — which decides nothing
    about where a scope belongs. Raises ConfigUnknown when it cannot be read at all."""
    path = scopes.registry_path(registry).parent / CONFIG_FILENAME
    try:
        st = os.stat(path)
    except OSError as e:
        raise ConfigUnknown(f"{path} is not there right now ({e.strerror or e})") from e
    if not stat.S_ISREG(st.st_mode):
        raise ConfigUnknown(f"{path} is not a regular file")
    try:
        return load_config(registry)
    except ConfigUnknown:
        raise
    except AgentError:
        return None


def withdraw_dropped(registry: str | Path | None = None) -> tuple[list[str], list[str]]:
    """Withdraw every snapshot no `[publish.<scope>]` entry asks for any more: `(done, failed)`,
    a line each. Run once per refresh pass, and once by `refresh-record`.

    Every folder a snapshot was published into for an entry is recorded beside the registry
    (`LEDGER_FILENAME`, by `_remember`). A recorded folder no entry names any more — the entry was
    deleted, every entry was, or its `dir` moved — is withdrawn WHOLE, and only a real removal
    forgets the row: a folder that is empty, absent or unreadable keeps it, quietly, for a later
    pass (an unmounted volume's empty mountpoint among them). Each row is decided under its scope's
    publish lock against the entry as it stands then, so a publish that lands meanwhile is seen.

    Nothing is decided on uncertain state. An `agent-publish.toml` that cannot be read right now
    (missing, not a file, unreadable) skips the sweep, said once; to withdraw every snapshot,
    leave the file empty. One that does not parse, or a malformed entry, decides nothing about
    where a scope belongs: its folders are kept, and only a copy there that may hold calls is
    withdrawn. A ledger that cannot be read skips what it would decide and is left alone; one with
    bad content is set aside and its valid rows kept. A `publish-snapshot --dir` copy is never
    recorded on its own, but one placed in a folder an entry once published to goes with that
    entry's row."""
    path = _ledger_path(registry)
    done: list[str] = []
    failed: list[str] = []
    # Nothing ever published and nothing asked for: not even the lock file is written.
    if not os.path.lexists(path) and not (path.parent / CONFIG_FILENAME).is_file():
        return done, failed
    try:
        with scopes.exclusive(path):
            ledger, why = _load_ledger(path)
    except LedgerUnreadable as e:
        ledger, why = None, str(e)
    if why:
        failed.append(why)
    try:
        config = _config_now(registry)
    except ConfigUnknown as e:
        if ledger:
            failed.append(f"{e}; no snapshot was withdrawn this pass. To withdraw every one, "
                          f"leave the file empty rather than remove it.")
        return done, failed

    calls: dict[str, list[Path]] = {}       # name -> folders whose call-holding copy goes
    if config is not None:
        for name, b in config.broken.items():
            if b.dir is not None:
                calls.setdefault(name, []).append(b.dir)
    for name, folders in sorted((ledger or {}).items()):
        for d in folders:
            try:
                with _publish_lock(name, registry):
                    # Decided here, against the entry as it stands under the lock a publish holds.
                    now = _config_now(registry)
                    if now is None or name in now.broken:
                        calls.setdefault(name, []).append(Path(d))
                        continue
                    target = now.targets.get(name)
                    if target is not None and _same_folder(Path(d), target.dir):
                        continue            # the entry's own folder: its publish decides
                    found = _withdraw(name, Path(d), unless_call_free=False,
                                      live=[(k, t.dir) for k, t in now.targets.items()])
                    if found == REMOVED:
                        _forget(name, d, registry)
            except ConfigUnknown as e:
                failed.append(f"{e}; {name}'s snapshot in {d} was not withdrawn this pass")
                continue
            except (AgentError, OSError, ValueError) as e:
                failed.append(f"{name}: snapshot in {d} not withdrawn: {e}")
                continue
            if found == REMOVED:
                done.append(f"{name}: snapshot in {d} WITHDRAWN — no [publish.{name}] entry "
                            f"names that folder")
    for name, folders in sorted(calls.items()):
        removed, why_not = withdraw_calls(name, folders, registry)
        done += [f"{name}: snapshot in {f} WITHDRAWN — its entry cannot be read, and the copy "
                 f"may hold calls" for f in removed]
        failed += why_not
    return done, failed


@dataclass(frozen=True)
class Published:
    scope: str
    db: Path
    manifest: Path
    rewrote: bool
    excluded: int = 0


@dataclass(frozen=True)
class _NoteCopy:
    """One note as the snapshot will carry it, read BEFORE the fingerprint is taken so the text
    itself is part of what decides whether the snapshot is current."""

    doc_id: str
    path: str
    text: str
    stale: int | None


def _doc_rows(store) -> list[tuple]:
    """The rows the note copies are derived from. Captured ONCE and fingerprinted, so the re-check
    after the copy sees a document that was added, removed, moved to another vault or repointed
    while the notes were being read — `index_version` hashes content, not where it lives."""
    return [tuple(r) for r in store.db.execute(
        "SELECT doc_id, source_path, source_sha256, vault, note_sha256, document_class "
        "FROM documents ORDER BY doc_id")]


def _read_notes(rows: list[tuple],
                roots: dict[str, Path]) -> tuple[list[_NoteCopy], frozenset[str]]:
    """Every note the snapshot will carry, read from the vault — and ONLY from the vault — plus
    the documents to EXCLUDE from the snapshot altogether.

    A document is carried only when its note can be shown to be the bytes compose indexed: the
    path resolves (strictly — it must exist) inside one of the scope's vault roots, it is read at
    that resolved path opened NOFOLLOW, and its checksum still matches the index. Anything else is
    excluded WHOLE, passages and vectors included. Compose indexes a symlinked note THROUGH the
    link, so the chunks can hold an outside file's text; and the link as it stands now — deleted,
    or swapped for an innocent file — says nothing about what compose read through it. Only a
    match against the indexed checksum does, so a note that cannot be matched fails closed. On
    the refresh path compose has just run and everything matches; a note edited since is left
    out until the next refresh. This runs as the operator and the result lands in a file another
    user reads."""
    from substrate.freshness import read_bounded, stale_verdict

    out, excluded = [], set()
    for doc_id, source_path, source_sha256, vault, note_sha256, _class in rows:
        try:
            real = Path(source_path).resolve(strict=True)
            if not any(real.is_relative_to(root) for root in roots.values()):
                raise OSError("outside the scope's vaults")
            raw = read_bounded(real, MAX_NOTE_BYTES, nofollow=True)
        except (OSError, RuntimeError):
            excluded.add(doc_id)
            continue
        # Carried only on a MATCH. `None` is a note whose edit cannot be seen — a row with no
        # `note_sha256` (`index --migrate` over pre-v11 run.json) whose declared checksum hides the
        # body — and carrying it on a falsy verdict is how an edited PDF-derived note reached the
        # snapshot. A recompose gives every row its checksum and brings these back.
        if stale_verdict(raw, source_sha256, note_sha256) is not False:
            excluded.add(doc_id)
            continue
        out.append(_NoteCopy(doc_id, _relative(source_path, roots, vault),
                             raw.decode("utf-8", errors="replace"), 0))
    return out, frozenset(excluded)


def _carried(rows: list[tuple], left_out: frozenset[str]) -> list[tuple]:
    """The document rows a snapshot may carry: every row but the calls it leaves out. The one
    definition `_publish` reads notes for and `_fingerprint` hashes."""
    return [r for r in rows if r[0] not in left_out]


def _fingerprint(store, include_sources: bool, excluded: frozenset[str], left_out: frozenset[str],
                 docs: list[tuple], notes: list[_NoteCopy], index_version: str) -> str:
    """What the snapshot would be built from. Equal fingerprints mean an identical snapshot, so a
    refresh that changed nothing re-verifies the manifest instead of rewriting 50MB.

    Over what the snapshot carries only: `_carried` rows, and the vectors, mentions and
    `index_version` of the notes it holds. Calls a snapshot leaves out are left out of this too: a fingerprint that moved when a
    call was added or edited, and the `published` stamp and rewrite that follow it, would tell
    the agent when calls change though it can read none. A call added since `left_out` was taken
    is carried, so it moves the fingerprint, and the publish refuses rather than copy it."""
    from substrate.store import schema

    # The documents the copy holds: `_fill` removes the rest, their vectors and mentions with them,
    # and a vector whose passage no row holds.
    held = (json.dumps(sorted(n.doc_id for n in notes)),)

    def rows(sql: str) -> list:
        return [list(r) for r in store.db.execute(sql, held)]

    # Identity is rebuilt by every compose from a file `index_version` does not cover, so a roster
    # edit alone must still republish rather than re-verify the old one — as far as it reaches the
    # snapshot, which keeps each mention's surface and kind and nothing else (`_strip_roster`).
    # The note copies are in for the same reason: a note unreadable at the last publish, or edited
    # since compose, must not be carried forward under a fresh `verified`.
    sig = json.dumps([
        index_version,
        rows("SELECT v.embed_model, COUNT(*) FROM chunk_vectors v "
             "JOIN chunks c ON c.chunk_id = v.chunk_id "
             "WHERE c.doc_id IN (SELECT value FROM json_each(?)) GROUP BY 1 ORDER BY 1"),
        # The very rows `_strip_roster` rebuilds the roster from, of the documents held.
        rows("SELECT d.doc_id, d.surface, e.kind FROM document_entities d "
             "JOIN entities e ON e.entity_id = d.entity_id "
             "WHERE d.doc_id IN (SELECT value FROM json_each(?)) ORDER BY 1, 2, 3"),
        [list(r) for r in _carried(docs, left_out)],
        [[n.doc_id, n.path, hashlib.sha256(n.text.encode()).hexdigest(), n.stale]
         for n in notes],
        include_sources, sorted(excluded), schema.SCHEMA_VERSION, MANIFEST_VERSION,
    ])
    return hashlib.sha256(sig.encode()).hexdigest()


def _relative(source: str, roots: dict[str, Path], vault: str | None) -> str:
    p = Path(source)
    root = roots.get(vault or "")
    if root is not None:
        try:
            return f"{vault}/{p.resolve().relative_to(root).as_posix()}"
        except ValueError:
            pass
    return f"{vault or '?'}/{p.name}"


def _fill(tmp: Path, excluded: frozenset[str], notes: list[_NoteCopy], meta: dict) -> None:
    """Turn a raw `VACUUM INTO` copy into a snapshot: drop the excluded, add note text, strip
    paths."""
    from substrate.store.index_store import IndexStore

    store = IndexStore(str(tmp), migrate=False)
    db = store.db
    try:
        # Deleted pages are zeroed, and the whole file is VACUUMed below, so an excluded note's
        # text does not survive in free pages of a file handed to another user.
        db.execute("PRAGMA secure_delete=ON")
        for doc_id in sorted(excluded):
            store.remove(doc_id)
        # A vector no passage holds is no part of a document here, and its `chunk_id` names the
        # document it came from — a call's, for all this copy can tell.
        db.execute("DELETE FROM chunk_vectors WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
        _renumber_rowids(db)
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        _strip_roster(db)
        # `vector_generation` came across with `VACUUM INTO`: the live index's epoch, and an `n`
        # the removals above just moved once per excluded vector, which would tell the agent how
        # much was left out. Redrawn, it says nothing about the source, and no two snapshots share
        # a generation.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'vector_generation'").fetchone():
            db.execute("UPDATE vector_generation SET epoch = lower(hex(randomblob(16))), n = 0")

        # The copy must hold exactly the documents the notes were read for — anything else would
        # keep an operator path, or carry text for a row that is not there. `_publish` refuses a
        # copy taken across a change; this is the last word on it.
        copied = {r[0] for r in db.execute("SELECT doc_id FROM documents")}
        if copied != {n.doc_id for n in notes}:
            raise AgentError("the copied index does not match the notes read for it; not "
                             "published. The next refresh will publish it.")
        db.executescript(_SNAPSHOT_DDL)
        db.execute("BEGIN")
        for n in notes:
            db.execute("INSERT INTO snapshot_notes(doc_id, path, text, stale) VALUES (?,?,?,?)",
                       (n.doc_id, n.path, n.text, n.stale))
            db.execute("UPDATE documents SET source_path=?, markdown_path='', raw_location=NULL "
                       "WHERE doc_id=?", (n.path, n.doc_id))
        db.executemany("INSERT INTO snapshot_meta(key, value) VALUES (?,?)",
                       [(k, str(v)) for k, v in meta.items()])
        db.execute("COMMIT")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("VACUUM")
    finally:
        store.close()


def _renumber_rowids(db) -> None:
    """Renumber every table's rowids 1..N in their existing order.

    `VACUUM` keeps rowids, so the gaps a removal leaves would say how many documents and passages
    were left out and where they sat — the count the manifest declines to state. Gaps the live index
    already had are its edit history, which is not the agent's either. The FTS index keys on
    `chunks.rowid` and is rebuilt after this; `vector_generation` is redrawn after it too."""
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'chunks_fts%' AND sql NOT LIKE '%WITHOUT ROWID%'")]
    db.execute("BEGIN")
    for table in tables:
        count, top = db.execute(f'SELECT COUNT(*), MAX(rowid) FROM "{table}"').fetchone()
        if not count or top == count:
            continue
        db.execute("DROP TABLE IF EXISTS temp.renumber")
        db.execute(f'CREATE TEMP TABLE renumber AS SELECT rowid AS old, '
                   f'ROW_NUMBER() OVER (ORDER BY rowid) AS new FROM "{table}"')
        db.execute("CREATE UNIQUE INDEX temp.renumber_old ON renumber(old)")
        db.execute(f'UPDATE "{table}" SET rowid = -(SELECT new FROM temp.renumber '
                   f'WHERE old = "{table}".rowid)')
        db.execute(f'UPDATE "{table}" SET rowid = -rowid')
    db.execute("DROP TABLE IF EXISTS temp.renumber")
    db.execute("COMMIT")


def _withdraw_if_sources_revoked(include_sources: bool, db_path: Path, manifest_path: Path,
                                 prior: dict | None) -> bool:
    """Remove a published snapshot that may hold calls the scope's entry no longer lets in. True
    if removed.

    Run before anything that can fail, by every path that touches a published scope: a refusal or
    a failed refresh would otherwise leave the copy serving them until a publish succeeds. A
    manifest that does not record `include_sources: false` — missing, unreadable, or written
    before the key existed, when every call was published — counts as holding calls."""
    if include_sources or not db_path.exists():
        return False
    if prior is not None and prior.get("include_sources") is False:
        return False
    db_path.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)
    return True


def _strip_roster(db) -> None:
    """Reduce the identity roster to what this scope's notes themselves show.

    The roster mirrors a file shared across every scope (`identity.py`), so as compose leaves it,
    it names people from other scopes, carries glosses the notes never state, and MERGES aliases
    — "Ada" in one note and "the Countess" in another resolve to one id, which is a judgement only
    the roster holds. None of that is this scope's to hand over, wherever the roster file lives.

    So the snapshot keeps one entity per distinct SURFACE a remaining note matched: named by that
    surface, no gloss, and an id that is a hash of the surface — so it reveals nothing the name
    does not, merges only what the text itself spells the same way, and stays stable across
    publishes so an `entity` filter keeps working. `kind` stays: it is a category, not a fact about
    the person. Nobody no remaining note mentions survives."""
    rows = db.execute("SELECT d.doc_id, d.surface, e.kind FROM document_entities d "
                      "JOIN entities e ON e.entity_id = d.entity_id").fetchall()

    def opaque(surface: str) -> str:
        return "e-" + hashlib.sha256(surface.casefold().encode()).hexdigest()[:12]

    names: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for _, surface, kind in rows:
        key = opaque(surface)
        names[key] = min(names.get(key, surface), surface)
        if kind is not None:
            kinds[key] = min(kinds.get(key, kind), kind)
    db.execute("BEGIN")
    db.execute("DELETE FROM document_entities")
    db.execute("DELETE FROM entities")
    db.executemany("INSERT INTO entities(entity_id, name, kind, gloss) VALUES (?,?,?,NULL)",
                   [(key, name, kinds.get(key)) for key, name in names.items()])
    db.executemany("INSERT OR IGNORE INTO document_entities(doc_id, entity_id, surface) "
                   "VALUES (?,?,?)", [(doc_id, opaque(surface), surface)
                                      for doc_id, surface, _ in rows])
    db.execute("COMMIT")


def _publish_lock(scope: str, registry: str | Path | None):
    """One publish or annotate per scope at a time. Held in the OPERATOR's state directory, never
    the shared folder, so no other user can hold it. Without it a manual `publish-snapshot` racing
    a refresh could finish with the older database under the newer manifest — whose fingerprint
    then matches every later refresh, so the stale copy would be re-verified as fresh for good."""
    return scopes.exclusive(scopes.registry_path(registry).parent / f"agent-snapshot-{scope}")


def publish(scope: str, directory: str | Path, *, registry: str | Path | None = None,
            now: datetime | None = None, include_sources: bool = False,
            record: bool = False) -> Published:
    """Publish one scope's snapshot and manifest into `directory`, which must already exist.
    Conversation-class documents are left out unless `include_sources`. `record`: the folder is
    the scope's entry's own, so it goes in the ledger before anything is written there
    (`withdraw_dropped`)."""
    with _publish_lock(scope, registry):
        return _publish(scope, Path(directory), registry=registry, now=now,
                        include_sources=include_sources, record=record)


def _publish(scope: str, directory: Path, *, registry: str | Path | None,
             now: datetime | None, include_sources: bool, record: bool) -> Published:
    from substrate import vault as _vault
    from substrate.classes import EXCLUDED_CLASSES
    from substrate.store.index_store import IndexStore, SchemaMismatch

    db_path, manifest_path = snapshot_paths(directory, scope)
    _trusted_dir(directory)
    prior, _ = _read_manifest(manifest_path)
    if _withdraw_if_sources_revoked(include_sources, db_path, manifest_path, prior):
        prior = None
    try:
        entry = scopes.resolve(scope, registry)
    except scopes.ScopeError as e:
        raise AgentError(str(e)) from e
    try:
        chain = _vault.resolve_vaults(Path(entry.vault))
    except _vault.VaultError as e:
        raise AgentError(str(e)) from e
    roots = {v.name: v.path.resolve() for v in chain}

    stamp = _stamp(now or _now())
    try:
        source = IndexStore(str(entry.db), migrate=False)
    except SchemaMismatch as e:
        raise AgentError(str(e)) from e
    try:
        if source.stats()["passages"] == 0:
            raise AgentError(f"scope {scope!r} has an EMPTY index; refusing to publish it.")
        rows = _doc_rows(source)
        # Never read, so a call's text is not held in this process for a snapshot it stays out of.
        left_out = frozenset() if include_sources else frozenset(
            r[0] for r in rows if r[5] in EXCLUDED_CLASSES)
        notes, excluded = _read_notes(_carried(rows, left_out), roots)
        if not notes and left_out and not excluded:
            raise AgentError(f"every note in {scope!r} is a source, which a snapshot leaves out "
                             f"unless its [publish.{scope}] entry sets include_sources = true; "
                             f"refusing to publish an empty snapshot.")
        if not notes:
            # The source was not empty, so everything was excluded — most likely the vault roots
            # no longer match the indexed paths (a moved vault, before a recompose). A published
            # empty snapshot would answer "nothing on that", labelled fresh.
            raise AgentError(f"no note in {scope!r} could be matched to the index "
                             f"({len(excluded)} excluded); refusing to publish an empty "
                             f"snapshot. Recompose the scope, then publish.")
        # The version of what the snapshot holds, as the agent's own engine computes it over the
        # copy: calls left out, and the documents excluded below left out too. Taken again for
        # each re-check below, so a compose landing in between is seen.
        held = frozenset(n.doc_id for n in notes)
        index_version = source.index_version_of(held)

        def again() -> str:
            return _fingerprint(source, include_sources, excluded, left_out, _doc_rows(source),
                                notes, source.index_version_of(held))

        fingerprint = _fingerprint(source, include_sources, excluded, left_out, rows, notes,
                                   index_version)
        rewrite = not (prior and prior.get("fingerprint") == fingerprint and db_path.is_file())
        if not rewrite and again() != fingerprint:
            # Re-verifying is a claim that the snapshot matches the live index NOW, so the same
            # re-check guards it: a change landing while the notes were read is not "unchanged".
            raise AgentError(f"the {scope!r} index changed while it was being checked; not "
                             f"re-verified. The next refresh will publish it.")
        if record:
            _remember(scope, directory, registry)
        if rewrite:
            fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{scope}.",
                                            suffix=".tmp")
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                source.db.execute("VACUUM INTO ?", (tmp_name,))
                # A compose that committed between the fingerprint and the copy would leave a
                # snapshot whose recorded version describes different rows. Refused, and the next
                # refresh — which follows that compose — publishes the settled index.
                if again() != fingerprint:
                    raise AgentError(f"the {scope!r} index changed while it was being copied; "
                                     f"not published. The next refresh will publish it.")
                source.close()
                _fill(tmp, excluded | left_out, notes,
                      {"scope": scope, "published": stamp, "index_version": index_version})
                os.chmod(tmp, FILE_MODE)
                os.replace(tmp, db_path)
            finally:
                for leftover in (tmp, Path(f"{tmp}-journal"), Path(f"{tmp}-wal"),
                                 Path(f"{tmp}-shm")):
                    leftover.unlink(missing_ok=True)
    finally:
        source.close()

    manifest = {
        "version": MANIFEST_VERSION,
        "scope": scope,
        "published": stamp if rewrite else prior.get("published"),
        # When the operator's engine last confirmed this snapshot matches the live index. AGE is
        # measured from here, so a scope that refreshes and does not change stays young.
        "verified": stamp,
        "index_version": index_version,
        "fingerprint": fingerprint,
        "sources": [v.name for v in chain],
        # Whether conversation-class documents were copied. No count when they were not: how many
        # calls a scope holds is part of what was left out.
        "include_sources": include_sources,
        # A count, not the ids: this file is the agent's to read, and the point of excluding a
        # document is that nothing about it reaches the agent.
        "excluded_documents": len(excluded),
        # The record as stored, judged on the agent side by `refresh_state.block_for` — so a
        # freeze the operator's engine recorded reaches the agent as the same freeze.
        "refresh": refresh_state.stored_row(scope, registry),
    }
    _write_atomic(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())
    return Published(scope, db_path, manifest_path, rewrite, len(excluded))


def annotate(scope: str, directory: str | Path, *, registry: str | Path | None = None,
             include_sources: bool = False, record: bool = False) -> str:
    """Carry a FAILED refresh to the agent without touching the snapshot: the copy stays as good
    as it was, and its manifest now says the live scope is frozen.

    Returns what happened: `absent` (nothing published to annotate), `marked`, or `withdrawn` —
    the snapshot may hold calls the entry no longer lets in, and a failed refresh does not
    republish, so it is removed instead (`_withdraw_if_sources_revoked`)."""
    directory = Path(directory)
    with _publish_lock(scope, registry):
        db_path, manifest_path = snapshot_paths(directory, scope)
        _trusted_dir(directory)
        prior, _ = _read_manifest(manifest_path)
        if _withdraw_if_sources_revoked(include_sources, db_path, manifest_path, prior):
            return "withdrawn"
        if prior is None:
            return "absent"
        unrecorded = None
        if record:
            # The freeze still reaches the agent when the ledger cannot take the row: it is said
            # after the manifest is marked, not instead of it.
            try:
                _remember(scope, directory, registry)
            except AgentError as e:
                unrecorded = e
        prior["refresh"] = refresh_state.stored_row(scope, registry)
        _write_atomic(manifest_path, (json.dumps(prior, indent=2) + "\n").encode())
        if unrecorded is not None:
            raise AgentError(f"snapshot manifest marked, but its folder is not recorded for "
                             f"withdrawal: {unrecorded}") from unrecorded
        return "marked"


def after_refresh_record(scope: str, outcome: str, *,
                         registry: str | Path | None = None) -> str | None:
    """The hook `refresh-record` calls once the outcome is recorded. Returns a line to print, or
    None when this scope is not published. Raises AgentError; the caller reports it, and a failed
    publish never turns a recorded refresh into a failed one."""
    config = load_config(registry)
    if (broken := config.broken.get(scope)) is not None:
        try:
            ledger, _ = _read_ledger(_ledger_path(registry))
        except LedgerUnreadable:
            ledger = {}                 # the pass's sweep says why
        folders = [Path(d) for d in ledger.get(scope, [])]
        removed, failed = withdraw_calls(
            scope, [broken.dir, *folders] if broken.dir is not None else folders, registry)
        said = "".join(f" The copy in {f}, which may hold calls, is WITHDRAWN." for f in removed)
        raise AgentError(f"{broken.error} Not published.{said}"
                         + "".join(f" {why}." for why in failed))
    target = config.targets.get(scope)
    if target is None:
        return None
    if refresh_state.OUTCOMES[outcome]["success"]:
        p = publish(scope, target.dir, registry=registry, include_sources=target.include_sources,
                    record=True)
        return f"{scope}: snapshot {'published' if p.rewrote else 'verified'} -> {p.db}"
    done = annotate(scope, target.dir, registry=registry, include_sources=target.include_sources,
                    record=True)
    if done == "withdrawn":
        return f"{scope}: snapshot WITHDRAWN — it may hold calls its entry no longer includes"
    if done == "marked":
        return f"{scope}: snapshot manifest marked {outcome}"
    return None


# --------------------------------------------------------------------------- agent side

@dataclass(frozen=True)
class SnapshotEntry:
    """What `_open` hands the tools in agent mode — the fields of a `ScopeEntry` they read, with no
    vault: the agent has none to name."""

    name: str
    db: Path
    composed: str | None = None
    vault: None = None
    index_root: None = None


class AgentScopes:
    """The agent's whole world: the allowed scopes, and the folder their snapshots are in."""

    def __init__(self, allowed: Iterable[str], directory: str | Path, *,
                 max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS):
        names = frozenset(allowed)
        bad = sorted(n for n in names if not valid_scope_name(n))
        if not names or bad:
            raise AgentError(f"--scopes needs one or more plain scope names; refused {bad}.")
        self.allowed = names
        self.directory = Path(directory)
        self.max_age_seconds = max_age_seconds

    def _paths(self, name: object) -> tuple[Path, Path]:
        if not isinstance(name, str) or name not in self.allowed:
            raise AgentError(UNAVAILABLE)
        return snapshot_paths(self.directory, name)

    def open(self, name: object):
        """`(entry, store)` for an allowed scope, opened immutable, or AgentError."""
        from substrate.store.index_store import IndexStore, SchemaMismatch

        db_path, manifest_path = self._paths(name)
        if not db_path.is_file():
            raise AgentError(f"scope {name!r} has no published snapshot yet.")
        try:
            store = IndexStore(str(db_path), migrate=False, immutable=True)
        except SchemaMismatch as e:
            raise AgentError(f"the snapshot for {name!r} cannot be read by this engine "
                             f"(schema v{e.found}, engine v{e.expected}); it must be "
                             f"republished.") from e
        try:
            row = store.db.execute(
                "SELECT value FROM snapshot_meta WHERE key='scope'").fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is None or row[0] != name:
            store.close()
            raise AgentError(f"the file at the {name!r} snapshot path is not that scope's "
                             f"snapshot.")
        manifest, _ = _read_manifest(manifest_path)
        published = manifest.get("published") if manifest else None
        return SnapshotEntry(name=name, db=db_path,
                             composed=published if isinstance(published, str) else None), store

    def refresh(self, name: str, *, now: datetime | None = None) -> dict:
        """The `refresh` block, judged from the manifest, plus `snapshot`: how old the copy is.

        `frozen` keeps its meaning — the operator's last recompose refused — and STALENESS is a
        separate key, because an old snapshot of a healthy scope is a different fact from a fresh
        snapshot of a frozen one, and a reader needs to tell them apart."""
        _, manifest_path = self._paths(name)
        manifest, unreadable = _read_manifest(manifest_path)
        block = refresh_state.block_for(manifest.get("refresh") if manifest else None)
        verified = manifest.get("verified") if manifest else None
        age = None
        if isinstance(verified, str) and len(verified) <= refresh_state.MAX_STAMP_CHARS:
            try:
                age = int(((now or _now()) - datetime.fromisoformat(verified)).total_seconds())
            except (ValueError, TypeError):
                age = None
        stale = None if age is None else age > self.max_age_seconds
        published = manifest.get("published") if manifest else None
        block["snapshot"] = {
            "published": published if isinstance(published, str)
            and len(published) <= refresh_state.MAX_STAMP_CHARS else None,
            "verified": verified if age is not None else None,
            "age_seconds": age,
            "max_age_seconds": self.max_age_seconds,
            # Tri-state like `frozen`: null means the age could not be established — no manifest,
            # or an unreadable one — which is absent evidence, not a young snapshot.
            "stale": stale,
        }
        lead = None
        if unreadable:
            lead = unreadable + "; the snapshot's age is unknown."
        elif stale is None:
            lead = "no snapshot manifest, so this snapshot's age is unknown."
        elif stale:
            lead = (f"STALE SNAPSHOT — last matched the live index {age // 3600}h ago, past the "
                    f"{self.max_age_seconds // 3600}h limit. The operator's refresh has not run "
                    f"since, so these answers may not reflect the vault.")
        if lead:
            block["note"] = lead if not block["note"] else f"{lead} {block['note']}"
        return block

    def sources_left_out(self, name: str) -> bool:
        """True only when the manifest says the snapshot was published without its sources. An
        older or unreadable manifest says nothing either way, so this says False."""
        _, manifest_path = self._paths(name)
        manifest, _ = _read_manifest(manifest_path)
        return bool(manifest) and manifest.get("include_sources") is False

    def note(self, store, doc_id: str, *, cap: int) -> dict:
        """The whole note behind a passage, from the snapshot — never from a vault."""
        row = store.db.execute(
            "SELECT path, text, stale FROM snapshot_notes WHERE doc_id=?", (doc_id,)
        ).fetchone()
        if row is None:
            raise AgentError(f"no note text for {doc_id!r} in this snapshot.")
        text = row["text"]
        return {
            "path": row["path"],
            "text": text[:cap],
            "n_chars": len(text),
            "truncated": len(text) > cap,
            # Judged at PUBLISH time. Always false or null: a note that had moved on from the index
            # is excluded from the snapshot rather than served.
            "stale": None if row["stale"] is None else bool(row["stale"]),
        }

    def scopes_payload(self) -> dict:
        out = []
        for name in sorted(self.allowed):
            db_path, manifest_path = snapshot_paths(self.directory, name)
            manifest, _ = _read_manifest(manifest_path)
            sources = manifest.get("sources") if manifest else None
            out.append({
                "scope": name, "db": str(db_path), "vault": None,
                "composed": (manifest or {}).get("published")
                if isinstance((manifest or {}).get("published"), str) else None,
                "index_present": db_path.is_file(),
                "refresh": self.refresh(name),
                "sources": [s for s in sources if isinstance(s, str)]
                if isinstance(sources, list) else None,
            })
        return {"scopes": out, "registry": None, "snapshot_dir": str(self.directory)}
