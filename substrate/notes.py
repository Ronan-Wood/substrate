"""Adding a note to a vault — plan first, write only on confirmation.

Doc 3a §2: ingest over MCP is a WRITE. "It requires explicit confirmation from the human in the
loop; it is not a fire-and-forget tool call." Adding stays additive-only: `plan`/`commit` never
touch an existing note. Changing one is a separate path, `plan_amend`/`commit_amend` at the end of
this module, with a diff to review and a compare-and-swap on the bytes the diff was taken from.

The gate is two-phase and the token is UNFORGEABLE: `plan()` writes nothing, mints a random
single-use nonce, and records it in a `PlanBook` held by the running process. `commit()` redeems
it — a token this process never issued is refused, and a redeemed one cannot be replayed.

An earlier version derived the token as `sha256(target + content)`. That was worthless as a gate:
both inputs come from the caller, so anyone could compute a valid token without ever calling
`plan()`, and a first-call write with a self-computed checksum succeeded. The docstring claimed a
guarantee the code did not provide, which is the failure docs/principles.md names directly — a stamp
implying a condition was handled is worse than no stamp, because the next reader trusts it.

Be precise about what this DOES buy, because overstating it once already happened here. It makes
a write impossible without a plan having been issued and returned for someone to read, and it
binds that plan to exact bytes at an exact path. It cannot force a human to actually read it —
that is the client's approval UI, and no server-side mechanism can reach it.

**The plan is executed, not guessed.** It runs the real ingestion into a throwaway directory, so
the spine validation, class policy and A18 coverage gate that would refuse this note at compose
refuse it here, before anything is written. What it does NOT run is the per-note A22 sweep, which
lives with the compose command; the plan says so rather than implying it checked everything.

**The spine gate here is STRICTER than compose**, so this path is not simply "compose, earlier":
`require_status=True` and `require_confidence=True`. A note authored now has someone present to
declare its status and doc_type and to judge its settledness, while compose defaults the first two
for a vault of plain notes and must keep accepting a migrated corpus that is 530/657 `unjudged`.
The asymmetry is the point — it stops those numbers growing without a flag day that would refuse
six scopes unattended.

**Never into core.** Doc 2 §2: projects read FROM core, nothing auto-writes INTO it — promoting
something to the tier every context inherits is a deliberate manual act. The destination is
always the scope's own project vault, and a path that escapes it is refused rather than clamped.

**The index is not updated.** Adding a note leaves it in the vault and absent from the index,
which is reported as a field (`index_stale`) and shows up in `status` drift as an unindexed note.
Recomposing as a side effect would run whole-scope gates that can refuse for reasons unrelated to
this note, and the caller could not tell which had happened.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from substrate.vault import (
    MANIFEST, SOURCE_META, SkipList, VaultError, _tier_for, folded,
    folder_fault, folder_parts, inherited_entries, is_source_meta, skip_list,
    writable_folders,
)
from substrate import writelog
from substrate.writelog import WriteLogError

# Where `ingest` writes when the caller names no folder: Doc 2 §4 designates it for model-entered
# synthesis notes. Like any other folder it must be writable in the vault (`_writable`).
DEFAULT_FOLDER = "04-synthesis"


class NoteError(RuntimeError):
    """The note cannot be added: bad destination, a collision, or a gate that would refuse it."""


@dataclass(frozen=True)
class Plan:
    target: Path
    doc_id: str
    status: str
    doc_type: str
    confidence: str
    domains: list[str]
    passages: int
    confirm_token: str
    warnings: list[str]


def _bound(h, bound_to: str) -> None:
    """A plan made for a GRANTED client is bound to that client's grant token as well
    (`grants.Decision.binding`), so another client cannot redeem it, and neither can this one after
    its grant is rotated, or revoked and granted again. Empty for a write no grant authorised,
    which binds as it always has."""
    if bound_to:
        h.update(b"grant\0" + bound_to.encode("utf-8") + b"\0")


def _binding(target: Path, content: bytes, bound_to: str = "") -> str:
    """What a plan is FOR: this exact content, at this exact path. Binding both is the point — a
    binding over content alone would let a redeemed plan write it somewhere else."""
    h = hashlib.sha256()
    _bound(h, bound_to)
    h.update(str(target).encode("utf-8"))
    h.update(b"\0")
    h.update(content)
    return h.hexdigest()


class PlanBook:
    """The plans this process has issued and not yet had redeemed.

    Process-local by design. A token is a random nonce, so it cannot be derived from anything the
    caller knows; it is checked against the binding it was issued for, so it cannot be carried to
    a different note or destination; and it is popped on redemption, so a plan authorises exactly
    one write. Held by the running server rather than persisted: a token surviving a restart would
    authorise a write against a plan nobody in this session ever saw.
    """

    def __init__(self) -> None:
        # token -> (binding, note). `note` is (target, sha256 of the bytes an amend plan's diff
        # was taken from), None for an add. The binding is what authorises a write; `note` lets a
        # commit say WHY it is refused when the note moved under the plan, and refuse a token not
        # issued for this note before it reads or checks anything.
        self._issued: dict[str, tuple[str, tuple[str, str] | None]] = {}

    def issue(self, binding: str, *, note: tuple[str, str] | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self._issued[token] = (binding, note)
        return token

    def note_of(self, token: str) -> tuple[str, str] | None:
        """(target, base sha256) an issued amend plan was for; None for an unknown token or an add."""
        entry = self._issued.get(token)
        return entry[1] if entry else None

    def matches(self, token: str, binding: str) -> bool:
        """Whether `redeem` would accept, without spending the token."""
        entry = self._issued.get(token)
        return entry is not None and secrets.compare_digest(entry[0], binding)

    def discard(self, token: str) -> None:
        self._issued.pop(token, None)

    def redeem(self, token: str, binding: str) -> bool:
        """True only for a token this process issued FOR THIS binding. Single-use either way — a
        token presented against the wrong binding is burned, not left available to retry."""
        issued = self._issued.pop(token, None)
        return issued is not None and secrets.compare_digest(issued[0], binding)


def _skip_list(project_vault: Path) -> SkipList:
    """What compose skips in the project vault: its manifest's `skip`, else the default. Asked of
    the manifest on every plan and commit, so a write is held to the list compose will read.

    The manifest's fault is not passed on: the caller here may be a granted client, and the fault
    is the owner's to read, in `substrate compose`."""
    try:
        return skip_list(project_vault)
    except VaultError as e:
        raise NoteError(
            f"cannot tell what {project_vault / MANIFEST} skips: the file, or the `skip` in it, "
            f"does not read (`substrate compose` says why). A note is written only where compose "
            f"would index it, so nothing is written until it does."
        ) from e


# `_meta.md` IS NEVER WRITTEN THIS WAY, WHATEVER THE VAULT'S `skip` SAYS. Every other refusal of a
# skipped name is about indexing: a note written where compose skips it is never indexed. This one
# is not. `_meta.md` supplies the class and spine of every passage beside it, so writing one
# re-spines notes the plan's diff never shows. Refused only while the list named it, it was
# writable in any vault whose list left it out. Refused in any letter case (`is_source_meta`): on a
# volume that ignores case, `_META.md` is the file `_source_meta` reads.
def _source_meta_refusal(what: str) -> NoteError:
    return NoteError(
        f"{what} is named as a source's {SOURCE_META} (in any letter case), which is never "
        f"written this way, whatever the vault's `skip` list says: it supplies the class and spine "
        f"of every passage beside it, so writing it changes notes the plan's diff does not show. "
        f"Edit it by hand."
    )


@dataclass(frozen=True)
class Writable:
    """The folders a write may go to in one project vault, each as its components (`()` is the
    vault root), with every folder below each, less any top-level folder in `excluded` (folded
    names, `vault.folded`). `declared` says whether the vault's manifest named them
    (`writable_folders`) or they are the default (`_default_writable`).

    "Below" holds for `amend` as it always did (a note anywhere under `02-areas/` was amendable)
    and now for `ingest` too, so one rule says where a note may be added and where it may be
    changed. What else a write must pass (hidden folders, the skip list, `passages/`, symlinks) is
    checked beside this, not by it."""

    folders: tuple[tuple[str, ...], ...]
    declared: bool
    excluded: frozenset[str] = frozenset()

    def excluding(self, parts: tuple[str, ...]) -> str | None:
        """The excluded top-level folder `parts` is under, as the caller spelled it, or None."""
        return parts[0] if parts and folded(parts[0]) in self.excluded else None

    def covers(self, parts: tuple[str, ...]) -> bool:
        # Exactly, as the walk compares: a declared folder exists spelled as written
        # (`_writable`), and on a volume that keeps NFC and NFD names apart an NFD twin beside
        # it is another folder, which the declaration does not name.
        if self.excluding(parts):
            return False
        return any(parts[:len(f)] == f for f in self.folders)

    def shown(self) -> list[str]:
        return ["/".join(f) or "." for f in self.folders]


# THE TWO FOLDERS THE DEFAULT LEAVES OUT, at the top of the vault. Before `writable_folders`
# existed the write path took only `02-areas`, `03-references` and `04-synthesis`, and that list was
# the only thing keeping a client out of these two: `_sources/` holds what was said, call
# transcripts among it, where only a class-frozen call was protected otherwise, and `_archive/` the
# record of what was filed away. A vault opts them in by declaring them, since a declaration
# replaces the default. Compared folded (`vault.folded`), not exactly as `skip` compares: on a
# volume that ignores case a client's `_SOURCES/` would otherwise be created as, and from then on
# be, the folder a later `_sources/call.md` is written into. Owner decision, 2026-11-13.
DEFAULT_UNWRITABLE: frozenset[str] = frozenset({"_sources", "_archive"})


def _default_writable(root: Path, skip: SkipList) -> Writable:
    """Where a write may go in a vault whose manifest declares no `writable_folders`: any folder
    compose does not skip, the root included, except the top-level `DEFAULT_UNWRITABLE`. Skipped
    and hidden folders are refused beside this, for every vault. The one place the default is
    decided."""
    return Writable(folders=((),), declared=False,
                    excluded=frozenset(folded(n) for n in DEFAULT_UNWRITABLE))


def _writable(root: Path, skip: SkipList) -> Writable:
    """The project vault's writable folders: its manifest's `writable_folders`, else the default.
    Read on every plan and commit, as the skip list is. A declaration the engine cannot apply
    writes nothing, and its fault is passed on: the list is not confidential, and the refusal
    names entries by position and an unknown manifest key by name, never a value.

    A declared folder must exist as the write path reaches it (`_walk_folder`: no link followed,
    each name spelled as it is on disk), and is never created: the owner wrote it down as there."""
    try:
        declared = writable_folders(root, skip)
    except VaultError as e:
        raise NoteError(f"{e} Nothing is written until it does.") from e
    if declared is None:
        return _default_writable(root, skip)
    missing = []
    for i, parts in enumerate(declared, 1):
        try:
            fd = _walk_folder(root, parts, create=False)
        except NoteError:
            fd = None
        if fd is None:
            missing.append(i)
        else:
            os.close(fd)
    if missing:
        raise NoteError(
            f"{root / MANIFEST}: 'writable_folders' entries {missing} (counting from 1) are not "
            f"folders in the vault: each must exist, be reached through no symlink, and be spelled "
            f"as it is on disk. A declared folder is not created. Nothing is written until it does.")
    return Writable(folders=declared, declared=True)


def _not_writable(what: str, writable: Writable, parts: tuple[str, ...]) -> NoteError:
    if (top := writable.excluding(parts)) is not None:
        return NoteError(
            f"{what} is under {top + '/'!r}, which a vault that declares no `writable_folders` "
            f"keeps unwritable, as it was before the manifest could say so. A declaration opens "
            f"it: list it in `writable_folders` in the vault's {MANIFEST}. A declaration replaces "
            f"the default, so list every folder that should take a write."
        )
    said = (f"Its {MANIFEST} declares `writable_folders` = {writable.shown()}" if writable.declared
            else f"Its {MANIFEST} declares no `writable_folders`, and the default is "
                 f"{writable.shown()}")
    return NoteError(
        f"{what} is not in a writable folder of this vault. {said}, and a note is written only in "
        f"one of those or a folder below one ('.' is the vault root). Notes are never written "
        f"into the shared core tier: promoting something there is a deliberate manual act."
    )


@dataclass(frozen=True)
class _Dest:
    """Where `ingest` writes: the resolved project vault, the folder's components below it, and
    the note's bare filename. Built only by `_resolve_target`, which checks every part."""

    root: Path
    parts: tuple[str, ...]
    filename: str

    @property
    def target(self) -> Path:
        return self.root.joinpath(*self.parts, self.filename)


def _resolve_target(project_vault: Path, folder: object, filename: object) -> _Dest:
    if folder is None:
        return _default_target(project_vault, filename)
    return _named_target(project_vault, folder, filename)


def _default_target(project_vault: Path, filename: object) -> _Dest:
    """No `folder` given: `DEFAULT_FOLDER`, only where it already exists and is writable. Never
    created, because a folder nobody named would appear in a vault laid out another way (owner
    decision, 2026-11-13). Whether it qualifies is decided first, on the folder alone, so a
    refusal of the note itself (its name, an existing file) still says what it is."""
    root = project_vault.expanduser().resolve()
    writable = _writable(root, _skip_list(root))
    fd = None
    if writable.covers((DEFAULT_FOLDER,)):
        try:
            fd = _walk_folder(root, (DEFAULT_FOLDER,), create=False)
        except NoteError:
            fd = None  # a link, a file, or another spelling: not the folder, so ask for one
    if fd is None:
        listed = f" Writable here: {writable.shown()}." if writable.declared else ""
        raise NoteError(
            f"no `folder` was given, and {DEFAULT_FOLDER!r} does not exist or is not writable in "
            f"this vault, so nothing is written. Name a `folder`, relative to the project vault; "
            f"'.' is the vault root.{listed}")
    os.close(fd)
    return _named_target(project_vault, DEFAULT_FOLDER, filename)


def _named_target(project_vault: Path, folder: object, filename: object) -> _Dest:
    """Check a new note's destination and return it. Writes nothing; a missing folder is allowed,
    and `_create` makes it.

    The folder is the caller's, so every component is checked lexically (`folder_fault`, then
    `_folder_gates`) and then walked on disk, no link followed and each name spelled as it is
    there: a symlinked folder is refused wherever it points, even inside the vault, because a check
    of the caller's string says nothing about where a link leads."""
    if not isinstance(filename, str) or not isinstance(folder, str):
        raise NoteError("`filename` and `folder` must be strings.")
    if not filename.endswith(".md"):
        raise NoteError(f"{filename!r} is not markdown. Only .md notes are added this way; a PDF "
                        "goes through the supervised reference-ingest path.")
    if "\0" in filename or Path(filename).name != filename:
        raise NoteError(f"{filename!r} must be a bare filename, not a path.")
    fault = folder_fault(folder)
    if fault:
        raise NoteError(f"`folder` {folder!r} {fault}. Name a folder relative to the project "
                        f"vault, with `/` between folders, or '.' for the vault root.")
    parts = folder_parts(folder)

    root = project_vault.expanduser().resolve()
    # A skip-list filename in a writable folder is a note that would be written, reported as
    # indexed, and then refused by every subsequent compose — and this path is additive-only, so
    # the MCP surface offers no way to undo it. Refuse at plan time, before anything is on disk.
    # The list is the vault's own: its manifest may declare one. Folded as well as exact: on a
    # volume that ignores case, a `LOG.md` created first is the file a later `log.md` is.
    skip = _skip_list(root)
    if is_source_meta(filename):
        raise _source_meta_refusal(repr(filename))
    if skip.skips_file(filename) or folded(filename) in {folded(n) for n in skip.files}:
        raise NoteError(
            f"{filename!r} is in this vault's skip list, so a note under that name is never "
            f"indexed no matter which folder it is in. Give it an ordinary filename "
            f"(e.g. `{folder}/<area>-digest.md` for a Doc 2 §6a digest)."
        )
    _refuse_hidden_characters(filename, "`filename`")
    _refuse_hidden_characters(folder, "`folder`")
    _folder_gates(root, parts, repr(folder), skip, adding=True, name=filename)

    dest = _Dest(root, parts, filename)
    _refuse_too_long(dest)
    fd = _walk_folder(root, parts, create=False)
    if fd is not None:
        try:
            os.stat(filename, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise NoteError(f"cannot inspect {dest.target}: {e}") from e
        else:
            raise NoteError(
                f"{dest.target} already exists. This path is additive-only: to change an existing "
                f"note use `amend`, which shows the diff and refuses if the note changes before "
                f"the write."
            )
        finally:
            os.close(fd)
    return dest


def _folder_gates(root: Path, parts: tuple[str, ...], what: str, skip: SkipList, *,
                  adding: bool, name: str) -> None:
    """The refusals of a destination folder that `ingest` and `amend` share, in one order.
    `parts` is the folder's components below the project vault, already free of `..`, `.` and
    hidden ones.

    When `adding`, skipped and `passages` folders are refused folded (`vault.folded`) as well as
    exactly, as the default's `_sources/` is: `ingest` may create the folder, and on a volume that
    ignores case a `PASSAGES/` or `PRIVATE/` a client created first is the folder a later
    `passages/` or `Private/` is, which compose then reads under the client's spelling, neither
    skipped nor a source's passages. `amend` creates nothing, so it compares as compose does, and
    an indexed note under `Passages/` stays amendable."""
    def named(p: str, n: str) -> bool:
        return p == n or (adding and folded(p) == folded(n))

    # TIER 1 AND 2 ARE NEVER WRITTEN OR EDITED BY A CLIENT, whatever `writable_folders` says:
    # operator and reference knowledge is promoted by hand (owner decision, 2026-11-13). A note's
    # tier comes from its path's names (`vault._tier_for`), which compose reads as they are on
    # disk and matches as a PREFIX. So each name is asked three ways: as given (what a volume that
    # keeps names as written stores), folded (a case twin a client creates must not become the
    # folder a later operator note lands in), and folded then decomposed (`00-operatoŕ` folds to
    # one character `ŕ`, while a volume storing NFD keeps `00-operator` + U+0301, which compose
    # reads as tier 1).
    raw = (*parts, name)
    if any(_tier_for(form) != 3 for form in (
            raw, tuple(folded(p) for p in raw),
            tuple(unicodedata.normalize("NFD", folded(p)) for p in raw))):
        raise NoteError(
            f"{what} gives a note the operator or reference tier (a name starting `00-operator` "
            f"or `10-reference`), which is promoted by hand: a client never adds or edits such a "
            f"note, whatever the vault's `writable_folders` says."
        )

    if skip.skips_folder(parts) or any(named(p, n) for p in parts for n in skip.folders):
        raise NoteError(
            f"{what} is in this vault's skip list (`skip` in its {MANIFEST}), so a note written "
            f"there is never indexed. Choose another folder."
        )
    # A note under `passages/` takes its class and spine from its source's `_meta.md`, which the
    # plan's staged ingestion never reads, so what it validated is not what compose would index.
    if any(named(p, "passages") for p in parts):
        raise NoteError(
            f"{what} is in a folder of reference passages, whose class and spine come from their "
            f"source's `{SOURCE_META}`, which a plan cannot reproduce. Notes are not written there "
            f"this way."
        )
    writable = _writable(root, skip)
    if not writable.covers(parts):
        raise _not_writable(what, writable, parts)
    _refuse_nested_inherited(root)


def _refuse_nested_inherited(root: Path) -> None:
    """NO WRITE AT ALL in a project vault whose chain names a vault at or below its own root,
    whether that vault exists yet or not. Compose reads such a folder as the inherited vault, the
    tier every inheriting scope reads, and writes go only to the project vault.

    A LAYOUT RULE, NOT A TARGET MATCH. Matching each write's folder against the inherited vaults
    was got past three times: by path, by `inherits` spelling the folder in another case than the
    disk (a volume that ignores case serves both), and by naming a folder that does not exist yet,
    which `ingest` would create and compose would then read as the vault. So the question asked is
    only where each named vault is. Each is resolved as compose resolves it, then the nearest part
    of that path which exists is compared by IDENTITY (device and inode), with every folder above
    it, against the project vault: that is one answer for an existing vault and a missing one, in
    any spelling. The cost: a vault that inherits a nested vault it also skips composes, and takes
    no write here until the nested vault is moved out."""
    try:
        named = inherited_entries(root)
        home = os.stat(root)
    except (VaultError, OSError) as e:
        # The fault is the owner's to read, in `substrate compose`, as `_skip_list` says.
        raise NoteError(f"cannot tell which vaults this scope inherits: a {MANIFEST} in its chain "
                        f"does not read (`substrate compose` says why), so nothing is written.") from e
    home_id = (home.st_dev, home.st_ino)
    for manifest, entry, path in named:
        try:
            here = Path(os.path.realpath(path))
        except (ValueError, OSError) as e:
            raise NoteError(f"{entry!r}, in `inherits` in {manifest}, is not a path the engine "
                            f"can resolve ({e}), so whether this vault holds it is unknown and "
                            f"nothing is written.") from e
        while not os.path.lexists(here) and here != here.parent:
            here = here.parent
        for folder in (here, *here.parents):
            try:
                st = os.stat(folder)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == home_id:
                raise NoteError(
                    f"this scope's project vault holds a vault it inherits: {entry!r}, in "
                    f"`inherits` in {manifest}, is at or inside {root}. Compose reads that folder "
                    f"as the inherited vault, so `ingest` and `amend` write nothing in this vault "
                    f"until the layout is fixed: move the inherited vault out of the project "
                    f"vault's folder and update `inherits`."
                )


def _refuse_hidden_characters(text: str, what: str) -> None:
    """A name holding a character a viewer would hide (`_hidden`: controls, format characters such
    as a bidi override, separators), or a tab, is refused. The plan's target is the text a person
    approves, and such a name can make it read as another place. (`_hidden` passes a tab because a
    note's text may hold one; a name has no use for it.)"""
    if "\t" in text or any(_hidden(text, i) for i in range(len(text))):
        shown, _, _ = _visible(text.replace("\t", "\u2409"))
        raise NoteError(f"{what} {shown!r} holds a tab or a character a viewer would hide (marked "
                        f"⟨U+XXXX⟩). Name it with visible characters only.")


def _refuse_too_long(dest: _Dest) -> None:
    """A path compose's walk of the vault cannot open is a note compose never finds, without an
    error. The walk below writes relative to a folder descriptor, so nothing else stops one."""
    try:
        limit = os.pathconf(dest.root, "PC_PATH_MAX")
    except (OSError, ValueError):
        limit = 1024
    size = len(os.fsencode(str(dest.target)))
    if size >= limit:
        raise NoteError(f"the note's path is {size} bytes long, and compose cannot read a path of "
                        f"{limit} bytes or more on this volume. Choose a shorter folder or "
                        f"filename.")
    # Checked at the plan, so an approved plan is not refused half way through making its folders.
    try:
        name_max = os.pathconf(dest.root, "PC_NAME_MAX")
    except (OSError, ValueError):
        name_max = 255
    long = [n for n in (*dest.parts, dest.filename) if len(os.fsencode(n)) > name_max]
    if long:
        raise NoteError(f"a name in the note's path is {len(os.fsencode(long[0]))} bytes long, over "
                        f"the {name_max} a name may be on this volume. Choose a shorter one.")


def _walk_folder(root: Path, parts: tuple[str, ...], *, create: bool,
                 verb: str = "added") -> int | None:
    """A descriptor for the folder `parts` names under `root`, reached one component at a time,
    or None when one is missing and `create` is False. With `create`, a missing one is made.

    NOTHING BELOW THE ROOT IS FOLLOWED. Each folder is opened `O_NOFOLLOW` relative to the one
    before it, and every read and write is made relative to the descriptor this returns, so a
    folder swapped for a symlink after a check cannot redirect it. An earlier `amend` re-resolved
    the whole path string for every syscall, so only the last component was protected.

    Each existing name must be spelled exactly as it is on disk. The lexical refusals compare the
    CALLER'S string, and APFS is case-insensitive: `PASSAGES/` reaches `passages/`, so without this
    every one of them could be walked around by changing case. A name this walk creates is made
    only in NFC, so it reads back as written on a volume that keeps names as given, and is not
    held to the spelling check, which a volume that stores names decomposed would fail. The caller
    closes the descriptor."""
    import errno
    import stat as _stat

    link = f"A note is {verb} only at its real location inside the project vault, never through a link."
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise NoteError(f"cannot open the project vault {root}: {e}") from e
    try:
        for i, part in enumerate(parts):
            here = root.joinpath(*parts[: i + 1])
            created = False
            try:
                st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                # This folder and every one below it would be new, so each is made only in NFC,
                # refused at the plan as at the commit.
                for new in parts[i:]:
                    if unicodedata.normalize("NFC", new) != new:
                        raise NoteError(f"{root.joinpath(*parts)} would make a new folder named "
                                        f"in decomposed Unicode (not NFC). Write its name "
                                        f"composed.") from None
                if not create:
                    os.close(fd)
                    return None
                try:
                    os.mkdir(part, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                except OSError as e:
                    raise NoteError(f"cannot create the folder {here}: {e}") from e
                try:
                    st = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except OSError as e:
                    raise NoteError(f"cannot inspect {here}: {e}") from e
            except OSError as e:
                raise NoteError(f"cannot inspect {here}: {e}") from e
            if not created:
                try:
                    spelled = part in os.listdir(fd)
                except OSError as e:
                    raise NoteError(f"cannot list {here.parent}: {e}") from e
                if not spelled:
                    raise NoteError(
                        f"{here} is not spelled the way it is on disk (case or Unicode form "
                        f"differs). Name it exactly as it is (`expand` reports a note's path)."
                    )
            if _stat.S_ISLNK(st.st_mode):
                raise NoteError(f"{here} is a symlink. {link}")
            if not _stat.S_ISDIR(st.st_mode):
                raise NoteError(f"{here} is not a folder.")
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise NoteError(f"{here} is a symlink. {link}") from e
                raise NoteError(f"cannot open {here}: {e}") from e
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _validate(
    *, project_vault: Path, content: bytes, filename: str, folder: str | None,
) -> tuple[_Dest, dict]:
    """Run every gate against this note and return (destination, the plan's fields). Issues NOTHING.

    Split from `plan` because `commit` needs the validation and must NOT mint a token: minting one
    per commit left an unredeemable nonce in the PlanBook on every call, successful or refused, so
    the book grew for the life of the server and its own docstring ("plans issued and not yet
    redeemed") stopped being true after the first write.
    """
    from substrate import classes
    from substrate.markdown.ingest import CoverageError, ingest_markdown
    from substrate.spine import SpineError

    dest = _resolve_target(project_vault, folder, filename)

    tmp = Path(tempfile.mkdtemp())
    try:
        staged = tmp / filename
        staged.write_bytes(content)
        try:
            # require_status=True: a note entering a vault through this path must declare its
            # status and doc_type. Compose would index it without them, as `active` and
            # `reference`, but a note being written now has an author present to say which.
            #
            # require_confidence=True is STRICTER than compose too, and deliberately so. A note
            # being written now has an author present to judge it, so absence here means "not
            # decided", not "no signal survived a migration". Grandfathering the corpus (compose
            # stays lenient) while gating new writes is what stops the 530 unjudged notes from
            # growing, without a flag day that would freeze six scopes unattended. `unstated`
            # satisfies this gate; `unjudged` cannot be declared, so the gate has no bypass.
            result = ingest_markdown(
                staged, tmp / "out", require_status=True, require_confidence=True
            )
        except (ValueError, classes.ClassPolicyError, SpineError, CoverageError) as e:
            raise NoteError(f"{type(e).__name__}: {e}") from e

        return dest, {
            "doc_id": result.doc_id,
            "status": result.status,
            "doc_type": result.doc_type,
            "confidence": result.confidence,
            "domains": list(result.domains),
            "passages": int((result.run.get("chunk") or {}).get("passages", 0)),
            # Named, not implied: the per-note A22 sweep runs at compose, not here, so this plan
            # does not promise the note will pass it.
            "warnings": ["the per-note A22 assertion sweep runs at compose, not in this plan"],
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def plan(
    *, project_vault: Path, content: bytes, filename: str, folder: str | None = None,
    book: PlanBook, author: str | None = None, bound_to: str = "",
) -> Plan:
    """Validate a note against the real ingestion gates without writing anything, and issue a
    single-use token for it.

    Raises NoteError with the gate's own message when the note would be refused — the same
    refusal compose would give, delivered before the vault is touched rather than after. A refused
    plan issues no token, so nothing that failed a gate is ever redeemable.

    `author` is a granted client's name: the note is stamped `written_by: <author>` BEFORE it is
    validated and bound (`stamp_new`), so what is planned is what is written. `bound_to` binds the
    plan to that client's grant.
    """
    if author is not None:
        content = stamp_new(content, author)
    dest, fields = _validate(project_vault=project_vault, content=content, filename=filename,
                             folder=folder)
    return Plan(target=dest.target,
                confirm_token=book.issue(_binding(dest.target, content, bound_to)), **fields)


def commit(
    *, project_vault: Path, content: bytes, filename: str, folder: str | None = None,
    confirm_token: str, book: PlanBook, author: str | None = None, bound_to: str = "",
    who: writelog.Identity | None = None,
) -> Path:
    """Write the note, but only for a token this process issued for exactly these bytes and path.

    Two independent checks, guarding two different failures. Re-planning re-runs every gate
    against the content being written NOW, so a source edited between plan and commit is refused
    rather than written unvalidated. Redeeming the token proves a plan was issued and returned for
    someone to read — which re-planning alone cannot establish, since the caller supplies both of
    its inputs.
    """
    if author is not None:
        content = stamp_new(content, author)
    dest, fields = _validate(project_vault=project_vault, content=content, filename=filename,
                             folder=folder)
    target = dest.target
    if not book.redeem(confirm_token, _binding(target, content, bound_to)):
        raise NoteError(
            "confirm_token was not issued by this server for this note and destination, or has "
            "already been used. Call again without a token to get a current plan, confirm it with "
            "the human, and pass THAT token."
        )

    try:
        with writelog.recorded(who, op="ingest", target=target, doc_id=fields.get("doc_id"),
                               before=None, after=_sha(content)):
            _create(dest, content)
    except WriteLogError as e:
        # The log refused the intent, so nothing was written.
        raise NoteError(f"{e} No note was written.") from e
    return target


def _create(dest: _Dest, content: bytes) -> None:
    # Inside the logged section, so a write the log refuses does not leave a new folder behind.
    # The additive-only and stay-inside-the-vault guarantees are enforced by the SYSCALL, not by
    # the earlier existence check — that check is a check-then-act pair with a window, and a
    # staging file at a fixed sibling name (`<note>.md.tmp`) was itself an unguarded write: a
    # symlink planted there redirected the content outside the vault entirely, which is exactly
    # what the containment checks exist to refuse. O_EXCL makes "never overwrite" atomic;
    # O_NOFOLLOW refuses a symlinked target outright; and the open is relative to the folder
    # descriptor `_walk_folder` reached without following a link, so no folder can be swapped.
    target = dest.target
    dfd = _walk_folder(dest.root, dest.parts, create=True)
    try:
        try:
            fd = os.open(dest.filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=dfd)
        except FileExistsError as e:
            raise NoteError(
                f"{target} appeared between the plan and the write. This path is additive-only; "
                f"refusing to overwrite it."
            ) from e
        except OSError as e:
            raise NoteError(f"cannot create {target}: {e}") from e
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
        except OSError as e:
            # A partial note in the vault would compose as a truncated one. Remove it and refuse.
            try:
                os.unlink(dest.filename, dir_fd=dfd)
            except OSError:
                pass
            raise NoteError(f"failed writing {target}: {e}") from e
    finally:
        os.close(dfd)


# --------------------------------------------------------------------------- amending a note
#
# Changing an existing note is the write where decision history gets flattened and confidence
# markers get laundered, so a plan that shows only the new text cannot reveal what the edit
# destroys. An amend plan therefore carries the unified diff of the note AS IT IS against the note
# as proposed, and the spine on both sides, and its token is bound to the checksum of the bytes the
# diff was taken from. A note edited between plan and commit — in Obsidian, by a sync, by another
# session — is refused rather than overwritten, which is the manual `diff` check automated.
#
# What the compare-and-swap does NOT buy, said rather than implied: POSIX has no compare-and-rename,
# so a write landing between the last checksum and the `rename` is still lost. That window is the
# few microseconds between two syscalls. The engine keeps no copy of the replaced version; recovery
# inside that window, or from any approved edit, is the sync provider's version history.

# A diff that does not fit in one reply cannot be reviewed, and a truncated one shows the reviewer
# less than they are approving. Refused at plan time instead: split the change.
AMEND_DIFF_CHAR_CAP = 64_000

# The largest note amend reads. Matches the MCP server's source cap and agent mode's note cap.
MAX_NOTE_BYTES = 8 * 1024 * 1024

# What the diff may cost, every tool call waiting on it under the server's one lock. A line cap
# alone did not bound it: `difflib` matches repeated lines quadratically, and 10,000 lines held a
# plan for 32.6 s, 5,000 for 8.8 s (measured 2026-11-05). So the matching runs against a clock,
# and the line cap bounds the one step the clock cannot interrupt: a single longest-match pass,
# about N·max(N/100, 200) comparisons (autojunk drops lines over 1% popular, from 200 lines up) —
# 77-102 ms at the cap, measured. The gates after the diff are not on the clock.
AMEND_LINE_CAP = 10_000
AMEND_DIFF_SECONDS = 2.0

# The spine fields a plan compares. `doc_id` is here for the one case pinning cannot cover: a
# filename whose derived id is outside the declared shape, which still changes with every edit.
SPINE_FIELDS = ("doc_id", "status", "doc_type", "confidence", "document_class", "domains",
                "supersedes", "superseded_by")


@dataclass(frozen=True)
class AmendPlan:
    target: Path
    base_sha256: str
    new_sha256: str
    diff: str
    spine_before: dict
    spine_after: dict
    spine_changed: list[str]
    passages: int
    confirm_token: str
    warnings: list[str]
    pinned_doc_id: str | None


def _amend_binding(target: Path, base_sha256: str, content: bytes, bound_to: str = "") -> str:
    """What an amend plan is FOR: replacing exactly the bytes whose checksum is `base_sha256`, at
    exactly this path, with exactly this content. Prefixed so no add binding can equal it."""
    h = hashlib.sha256()
    h.update(b"amend\0")
    _bound(h, bound_to)
    h.update(str(target).encode("utf-8"))
    h.update(b"\0")
    h.update(base_sha256.encode("ascii"))
    h.update(b"\0")
    h.update(content)
    return h.hexdigest()


_sha = writelog.sha256


def _resolve_existing(project_vault: Path, path: str) -> tuple[Path, tuple[str, ...], Path, str]:
    """Check `path` LEXICALLY and return (vault root, components, target, vault-relative posix).

    Touches nothing on disk below the root but the vault's manifest, read for its `skip`:
    `_open_note_dir` walks the path, and every write is made relative to the folder descriptor that
    walk ends on. The root itself may sit behind a link (`~/OneDrive` is one) because the operator
    chose it.
    """
    from pathlib import PurePosixPath

    if "\0" in path:
        raise NoteError("`path` contains a NUL byte, which no note name can.")
    root = project_vault.expanduser().resolve()
    given = os.path.expanduser(path) if path.startswith("~") else path
    raw = PurePosixPath(given)
    if raw.is_absolute():
        rel = None
        # Accept the vault as the registry spells it or as it resolves: `expand` reports the
        # resolved path, a person types `~/OneDrive/...`. Neither form is resolved itself.
        for base in (root, project_vault.expanduser()):
            try:
                rel = PurePosixPath(Path(given).relative_to(base).as_posix())
                break
            except ValueError:
                continue
        if rel is None:
            raise NoteError(
                f"{path} is outside the project vault {root}. Only this scope's own project vault "
                f"is amended — never the shared core tier, which is promoted by hand."
            )
    else:
        rel = raw

    parts = rel.parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise NoteError(f"{path!r} must name a note inside the project vault, without `..`.")
    if any(p.startswith(".") for p in parts):
        raise NoteError(f"{path!r} names a hidden file or folder, which is never a note.")
    skip = _skip_list(root)
    if skip.skips_file(parts[-1]):
        raise NoteError(f"{path!r} is in this vault's skip list, so it is never indexed as a note.")
    if is_source_meta(parts[-1]):
        raise _source_meta_refusal(repr(path))
    # The same folders `ingest` writes to, refused the same way: a note the engine may add, it may
    # change.
    _folder_gates(root, parts[:-1], repr(path), skip, adding=False, name=parts[-1])
    if not parts[-1].endswith(".md"):
        raise NoteError(f"{path!r} is not markdown. Only .md notes are amended.")
    return root, parts, root.joinpath(*parts), rel.as_posix()


def _open_note_dir(root: Path, parts: tuple[str, ...]) -> int:
    """A descriptor for the folder holding the note, reached by `_walk_folder` (no link followed,
    each name spelled as it is on disk), with the note itself checked there: a regular file, spelled
    as it is, not a link. The read, the staging file and the `rename` all happen relative to the
    descriptor this returns. The caller closes it."""
    import stat as _stat

    missing = NoteError(f"{root.joinpath(*parts)} does not exist. `amend` changes an existing "
                        f"note; use `ingest` to add a new one.")
    fd = _walk_folder(root, parts[:-1], create=False, verb="amended")
    if fd is None:
        raise missing
    here, name = root.joinpath(*parts), parts[-1]
    try:
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            raise missing from None
        except OSError as e:
            raise NoteError(f"cannot inspect {here}: {e}") from e
        try:
            spelled = name in os.listdir(fd)
        except OSError as e:
            raise NoteError(f"cannot list {here.parent}: {e}") from e
        if not spelled:
            raise NoteError(f"{here} is not spelled the way it is on disk (case or Unicode form "
                            f"differs). Name the note exactly as `expand` reports it.")
        if _stat.S_ISLNK(st.st_mode):
            raise NoteError(f"{here} is a symlink. A note is amended only at its real location "
                            f"inside the project vault, never through a link.")
        if not _stat.S_ISREG(st.st_mode):
            raise NoteError(f"{here} is not a regular file.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_at(dfd: int, name: str, shown: Path) -> bytes:
    """The note's bytes, opened `O_NOFOLLOW` relative to its folder's descriptor."""
    from substrate.freshness import FileTooLarge, NotRegularFile, read_bounded

    try:
        return read_bounded(Path(name), MAX_NOTE_BYTES, nofollow=True, dir_fd=dfd)
    except (NotRegularFile, FileTooLarge) as e:
        raise NoteError(f"{shown}: {e}") from e
    except OSError as e:
        raise NoteError(f"cannot read {shown}: {e}") from e


def _lines(data: bytes) -> list[str]:
    """Lines split on `\\n` ONLY. `str.splitlines` also breaks on `\\f`, `\\x85`, U+2028 and a
    lone `\\r`, which gave the reviewer a diff whose lines were not the file's — and a false
    "No newline at end of file" marker mid-diff wherever one of them appeared."""
    parts = data.decode("utf-8", errors="replace").split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _line_count(data: bytes) -> int:
    """`len(_lines(data))` without decoding: what the diff will have to compare."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


# What a viewer would hide, by Unicode CATEGORY rather than a list: controls (Cc), format characters
# (Cf — zero-width spaces, bidi overrides, and the tag block U+E0000–E007F that smuggles ASCII text
# no viewer draws), line and paragraph separators (Zl, Zp), private-use (Co) and unassigned (Cn).
# A hand-picked list missed most of Cf, so a model could add a whole invisible instruction to a
# note and the approved diff showed nothing (crosscheck, 2026-11-04). Plus the default-ignorable
# code points that are NOT Cf: CGJ, the Hangul fillers, and the variation selectors.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})
_HIDDEN_EXTRA = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0})
_ZWJ, _VS16 = "\u200d", "\ufe0f"


def _hidden(text: str, i: int) -> bool:
    import unicodedata

    ch, o = text[i], ord(text[i])
    # Emoji are built from a joiner and a presentation selector between pictographs (👩\u200d💻, ❤\ufe0f).
    # Marking those would put a mark inside every emoji, which teaches a reviewer to ignore marks;
    # anywhere else the same two characters are hidden like any other.
    #
    # "Between pictographs" covers the shapes in ordinary use: a pictograph (So), or one followed by
    # a skin-tone modifier (U+1F3FB-1F3FF) or by VS16, then the joiner, then a pictograph; and VS16
    # after a pictograph or a keycap base (`1` + VS16 + U+20E3). The skin tones are named by RANGE,
    # not by their category: Sk also holds ASCII `` ` `` and `^`, and exempting it let a VS16 after a
    # backtick make `` `\ufe0f`` `` read as a code fence in the approved diff while the reader parsed
    # text (crosscheck, 2026-11-04). A text-default symbol given emoji presentation (U+203C + VS16) is
    # still marked: rare, and marked is not hidden.
    before = text[i - 1] if i > 0 else ""
    prev = unicodedata.category(before) if before else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    skin = bool(before) and 0x1F3FB <= ord(before) <= 0x1F3FF
    if ch == _VS16 and (prev == "So" or (before in "0123456789#*" and before and nxt == "\u20e3")):
        return False
    if ch == _ZWJ and (prev == "So" or skin or before == _VS16) \
            and nxt and unicodedata.category(nxt) == "So":
        return False
    if ch == "\t":
        return False
    return (unicodedata.category(ch) in _HIDDEN_CATEGORIES or o in _HIDDEN_EXTRA
            or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF or 0x180B <= o <= 0x180F)


def _visible(text: str) -> tuple[str, int, int]:
    """`text` with every character a viewer would hide shown as `⟨U+XXXX⟩`; returns (text, how many
    hidden characters were marked, how many literal `⟨` were escaped).

    A literal `⟨` is escaped the same way, so in the diff every `⟨` opens a mark and a note cannot
    forge one. It is counted apart: escaping it hides nothing, and `⟨x, y⟩` is ordinary notation in
    a maths note. Lookalike brackets (U+2329, U+3008) are NOT escaped.
    """
    out, hidden, literal = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "\u27e8":
            out.append("\u27e8U+27E8\u27e9")
            literal += 1
        elif _hidden(text, i):
            out.append(f"\u27e8U+{ord(ch):04X}\u27e9")
            hidden += 1
        else:
            out.append(ch)
    return "".join(out), hidden, literal


# The fields amend's own gates read from the current note: identity, supersession, the frozen class.
_GUARDED = frozenset({"doc_id", "status", "superseded_by", "document_class", "class"})


def _spine_of(staged: Path) -> tuple[dict, str]:
    """The DECLARED spine of a staged note, and its declared `doc_id` line ("" if none).

    Parsed, not gated, so the current note's spine can be read even if it would not pass today's
    write gate (most migrated notes declare no confidence). Both sides of a plan go through this one
    function, so a field reported as changed changed in the file, not in how it was read.
    """
    from substrate.classes import UNCLASSIFIED_CLASS
    from substrate.markdown.reader import (
        _parse_frontmatter, doc_id_list, normalized_text, read_markdown,
    )

    # A field the reader cannot read is left out, so amend can repair such a note, except where a
    # gate below rests on it: those refuse, rather than judge the note as if it declared nothing.
    text = normalized_text(staged.read_bytes())
    _parse_frontmatter(text, strict=True, keys=_GUARDED)
    doc, _body, _stats = read_markdown(staged, strict=False)
    front, _ = _parse_frontmatter(text)
    return {
        "doc_id": doc.doc_id,
        "status": doc.status,
        "doc_type": doc.doc_type,
        "confidence": doc.confidence,
        "document_class": doc.document_class or UNCLASSIFIED_CLASS,
        "domains": list(doc.domains),
        "supersedes": doc_id_list(doc.supersedes),
        "superseded_by": doc.superseded_by,
    }, front.get("doc_id", "")


def _require_within_caps(data: bytes, what: str) -> None:
    """The byte cap (a note over it no later amend could read back) and the line cap (one over it
    no plan could diff in bounded time), with `what` naming the bytes in the refusal."""
    if len(data) > MAX_NOTE_BYTES:
        raise NoteError(f"{what} is {len(data)} bytes, over the {MAX_NOTE_BYTES}-byte limit for a "
                        f"note.")
    n = _line_count(data)
    if n > AMEND_LINE_CAP:
        raise NoteError(f"{what} has {n} lines, over the {AMEND_LINE_CAP} that one plan can diff.")


def _diff_too_costly() -> NoteError:
    return NoteError(
        f"the diff took more than {AMEND_DIFF_SECONDS:g} s to work out, the most one diff may "
        f"take. Changes that reorder many repeated lines cost the most; split the change into "
        f"smaller amends."
    )


def _diff_too_large() -> NoteError:
    return NoteError(
        f"the diff is over the {AMEND_DIFF_CHAR_CAP} characters that one plan can show whole. A "
        f"truncated diff cannot be reviewed; split the change into smaller amends."
    )


def _unified(a: list[str], b: list[str], fromfile: str, tofile: str):
    """`difflib.unified_diff`'s lines, from a matcher that stops at the clock. Its formatting is
    copied rather than called because `unified_diff` builds its own `SequenceMatcher` with no way
    in; `test_the_bounded_diff_is_difflibs_diff` holds the two to the same output."""
    import difflib
    import time

    deadline = time.monotonic() + AMEND_DIFF_SECONDS

    class Bounded(difflib.SequenceMatcher):
        def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
            if time.monotonic() > deadline:
                raise _diff_too_costly()
            return super().find_longest_match(alo, ahi, blo, bhi)

    def span(start: int, stop: int) -> str:
        length = stop - start
        if length == 1:
            return f"{start + 1}"
        return f"{start + 1 if length else start},{length}"

    for k, group in enumerate(Bounded(None, a, b).get_grouped_opcodes(3)):
        if not k:
            yield f"--- {fromfile}\n"
            yield f"+++ {tofile}\n"
        first, last = group[0], group[-1]
        yield f"@@ -{span(first[1], last[2])} +{span(first[3], last[4])} @@\n"
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                yield from (" " + line for line in a[i1:i2])
                continue
            if tag in ("replace", "delete"):
                yield from ("-" + line for line in a[i1:i2])
            if tag in ("replace", "insert"):
                yield from ("+" + line for line in b[j1:j2])


def _render_diff(base: bytes, content: bytes, rel: str) -> tuple[str, dict, int]:
    """The diff a human approves, with what a viewer would hide marked, and how many marks fell on
    each side. Refused once it is longer than one plan can show whole — counted on the RAW lines
    as they arrive, so a note of a few huge lines is refused before any of it is rendered."""
    # A final line without a newline is marked the way GNU diff marks it. Left bare, it ran into
    # the next diff line, so `-old last line+new last line` read as one removed line and hid the
    # added one — in the only text the human approves. Obsidian often saves without a final newline.
    rendered, literal, raw = [], 0, 0
    hidden = {"+": 0, "-": 0, " ": 0}
    for k, line in enumerate(_unified(_lines(base), _lines(content), f"a/{rel}", f"b/{rel}")):
        raw += len(line)
        if raw > AMEND_DIFF_CHAR_CAP:
            raise _diff_too_large()
        ended = line.endswith("\n")
        shown, n, lit = _visible(line[:-1] if ended else line)
        # Counted by the side of the diff the line is on: a mark in a removed line is something
        # the amend takes OUT, and saying the note will hold it would misdescribe the change.
        # The two file headers are told apart by POSITION, which `unified_diff` fixes, not by their
        # `---`/`+++` prefix: a removed YAML fence renders as `----` and an added `++x` as `+++x`.
        side = line[:1] if k >= 2 and line[:1] in hidden else " "
        hidden[side] += n
        literal += lit
        rendered.append(shown + ("\n" if ended else "\n\\ No newline at end of file\n"))
    diff = "".join(rendered)
    # Again on the RENDERED text: a hidden character grows into a `⟨U+XXXX⟩` mark.
    if len(diff) > AMEND_DIFF_CHAR_CAP:
        raise _diff_too_large()
    return diff, hidden, literal


def _check_amend(*, target: Path, rel: str, base: bytes, content: bytes,
                 issued: Callable[[bytes], bool] | None = None,
                 author: str | None = None) -> tuple[dict, bytes]:
    """Every gate an amend must pass, against the current bytes `base`. Issues NOTHING.

    Returns (the plan's fields, the bytes to WRITE): `content` with a `doc_id:` line added when the
    note is pinnable, `content` has no `doc_id` key, and `_pin` can write one; `content` itself
    otherwise.
    The caller binds and writes these bytes, never the content it was handed.

    `issued` is None for a plan. A commit passes whether its token was issued for the bytes to
    write, asked as soon as they are known, so a token that cannot redeem buys no diff or gate;
    and a commit does not diff at all — the plan showed the diff, the binding holds these bytes to
    it, and a diff against the clock could refuse an approved edit on a slower second run."""
    from substrate import classes
    from substrate.markdown.ingest import CoverageError, ingest_markdown
    from substrate.spine import SpineError

    # The caps, before anything is parsed: a current note over the line cap costs seconds to read
    # as markdown, and is refused regardless. The pinned bytes are capped again below.
    _require_within_caps(base, str(target))
    _require_within_caps(content, "the amended note")
    tmp = Path(tempfile.mkdtemp())
    try:
        # The SAME filename on both sides, so an undeclared doc_id derives exactly as it will in
        # the vault: from the note's stem and its bytes.
        (tmp / "before").mkdir()
        before_path = tmp / "before" / target.name
        before_path.write_bytes(base)
        try:
            before, before_fid = _spine_of(before_path)
        except ValueError as e:
            raise NoteError(f"the current note cannot be read as markdown: {e}") from e

        if before["status"] == "superseded":
            raise NoteError(
                f"{target} is superseded (by {before['superseded_by']}). A superseded note is the "
                f"record of what was replaced; change the live note instead."
            )
        before_policy = classes.POLICIES.get(before["document_class"])
        if before_policy is not None and before_policy.frozen:
            raise NoteError(
                f"{target} is `{before['document_class']}`, a frozen class "
                f"({before_policy.description}) — it is never edited in place."
            )

        # PIN AN UNDECLARED ID. Without a `doc_id:` line a note's id is its stem plus a hash of its
        # bytes (`extract/base.py:57`), so every edit gave it a new identity and broke whatever named
        # it — a `supersedes` list, a `superseded_by`. The id it has NOW is written into the
        # frontmatter as part of this same edit, where the diff shows it. Once declared, the reader
        # takes it as given (`reader.py`) and never derives one again, so the new line changing the
        # hashed bytes does not matter, and neither does a later rename in Obsidian.
        # DECLARED means declared in a shape the reader honours. An empty `doc_id:` (a blank
        # Obsidian property) or an ill-shaped one is ignored by the reader, so such a note derives
        # its id like an undeclared one — and must be pinned like one (crosscheck, 2026-11-04).
        from substrate.markdown.reader import _DOC_ID

        declared = bool(_DOC_ID.fullmatch(before_fid))
        pinnable = not declared and bool(_DOC_ID.fullmatch(before["doc_id"]))
        if pinnable:
            with_pin = _pin(content, before["doc_id"])
            if with_pin != content:
                _require_within_caps(with_pin, "the amended note with its pinned doc_id line")
            content = with_pin
        # A GRANTED client's amend is stamped `amended_by` (`stamp_amend`), before the token is
        # checked and the diff taken, so both cover the stamp. Not a note sent back unchanged: the
        # stamp alone is no edit, and "nothing to amend" below says so.
        if author is not None and content != base:
            stamped = stamp_amend(content, base, author)
            if stamped != content:
                _require_within_caps(stamped, "the amended note with its amended_by line")
            content = stamped
        if issued is not None and not issued(content):
            raise _not_issued()
        # ONE `doc_id:` line at most. The reader keeps the last of several, so a decoy line above
        # the real one passed the identity gate while the approved diff showed an identity being
        # added — and YAML tooling that rejects duplicate keys drops the whole block (crosscheck).
        id_lines = _doc_id_lines(content)
        if id_lines > 1:
            keep = before["doc_id"] if pinnable else before_fid
            raise NoteError(
                f"the amended note has {id_lines} `doc_id:` lines in its frontmatter. A note has "
                f"one identity: " + (f"keep exactly one, `doc_id: {keep}`." if keep
                                     else "remove them all; this note's id cannot be declared.")
            )
        if base == content:
            # A pinnable note sent back unchanged still needs its pin, and "nothing to amend" would
            # hide the one line that would fix it: either its `doc_id:` line is empty or ill-shaped,
            # or `_pin` declined to write one. Only a note WITH frontmatter keys can be the second
            # case; one with none (no block, an empty one, a thematic break) truly has nothing to
            # amend, and blaming a `---` first line it does not have would be false.
            if pinnable and _declined_by_block(content):
                raise _pin_declined(before["doc_id"], content)
            if pinnable and id_lines:
                raise NoteError(
                    f"the note's id is {before['doc_id']}, and its `doc_id:` line is "
                    f"{'empty' if not before_fid else repr(before_fid)}. Write the line as "
                    f"`doc_id: {before['doc_id']}`."
                )
            raise NoteError(f"{target} already holds exactly this content; there is nothing to "
                            f"amend.")
        # Before the gates: a diff no plan could show is refused without spending them on it.
        diff, hidden, literal = (_render_diff(base, content, rel) if issued is None
                                 else ("", {"+": 0, "-": 0, " ": 0}, 0))

        (tmp / "after").mkdir()
        after_path = tmp / "after" / target.name
        after_path.write_bytes(content)
        try:
            # The same gates as adding a note, including the stricter confidence gate: an amend has
            # an author present, so a note it touches leaves with a judged confidence.
            result = ingest_markdown(
                after_path, tmp / "out", require_status=True, require_confidence=True
            )
        except (ValueError, classes.ClassPolicyError, SpineError, CoverageError) as e:
            raise NoteError(f"{type(e).__name__}: {e}") from e
        after, after_fid = _spine_of(after_path)

        after_policy = classes.POLICIES.get(after["document_class"])
        if after_policy is not None and after_policy.frozen:
            raise NoteError(
                f"the amended note would be `{after['document_class']}`, a frozen class "
                f"({after_policy.description}). A frozen note is never produced by an edit."
            )
        # A declared id may not change. An undeclared one may only BECOME declared, as the id the
        # note has now — by the pin above, or by the caller writing that same line. A note that can
        # be pinned MUST leave pinned: an empty or ill-shaped `doc_id:` sent back (copied from the
        # note) would otherwise switch the pin off, and the id would move on every edit.
        if declared:
            if after_fid != before_fid:
                raise NoteError(
                    f"the declared doc_id changes ({before_fid} → {after_fid or '<none>'}). A "
                    f"declared id is the note's identity: an amend may not change or remove it."
                )
        elif pinnable:
            if not id_lines:
                # ABSENT, so `_pin` declined: the spine gate passed, so frontmatter keys exist, and
                # a line above the first one would change how the block reads (above a `---`
                # document marker, the block would end or YAML would read two documents). "Leave
                # it out" would loop; say what works.
                raise _pin_declined(before["doc_id"], content)
            if after_fid != before["doc_id"]:
                state = "empty" if not after_fid else repr(after_fid)
                raise NoteError(
                    f"the note's id is {before['doc_id']}, and its `doc_id:` line is {state}. "
                    f"Write the line as `doc_id: {before['doc_id']}`."
                )
        elif after_fid not in ("", before_fid):
            # Unpinnable: the id this filename derives cannot be declared, and amend never ASSIGNS
            # an id — a new one could collide with another note's and refuse the scope. A
            # hand-written line here was once reported pinned while the id quietly changed.
            was = (f"`doc_id: {before_fid}`" if before_fid
                   else "empty" if _doc_id_lines(base) else "absent")
            raise NoteError(
                f"amend does not assign a new id, and the id this filename derives "
                f"({before['doc_id']}) is outside the shape a declared id must have, so this note "
                f"keeps deriving its id. Leave the `doc_id:` line as it was ({was}), or leave it "
                f"out."
            )
        pinned = before["doc_id"] if pinnable else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    changed = [f for f in SPINE_FIELDS if before[f] != after[f]]
    warnings = [
        "the per-note A22 assertion sweep runs at compose, not in this plan",
        "the engine keeps no copy of the current version; recovering it after the write means the "
        "sync provider's version history — restore it IN PLACE: a copy restored beside the note "
        "either claims the same doc_id (compose refuses the scope) or composes as a second, "
        "duplicate note",
    ]
    if any(hidden.values()):
        where = ", ".join(f"{n} in {label}" for n, label in (
            (hidden["+"], "added lines (the amended note will hold them)"),
            (hidden["-"], "removed lines"), (hidden[" "], "unchanged lines")) if n)
        warnings.append(
            f"the diff shows characters a viewer would hide (controls, zero-width and format "
            f"characters, separators) as `⟨U+XXXX⟩`: {where}"
        )
    if literal:
        warnings.append(f"a literal `⟨` in the note is shown as `⟨U+27E8⟩` ({literal}), so that "
                        f"every `⟨` in the diff opens a mark")
    if pinned is not None:
        warnings.append(
            f"doc_id pinned: the note declared no usable id, so this amend sets `doc_id: {pinned}` "
            f"— the id it has now — and it keeps that id through this and later edits and renames. "
            f"From now on any copy of the note (a sync conflict, an Obsidian duplicate) claims the "
            f"same id, and compose refuses the scope until the copy is removed or given its own"
        )
    if "doc_id" in changed:
        warnings.append(
            f"doc_id changes {before['doc_id']} → {after['doc_id']}: the id derived from this "
            f"filename is not a valid declared id, so it cannot be pinned. Nothing that names the "
            f"old id — a `superseded_by`, a `supersedes` entry — is updated."
        )
    if "confidence" in changed:
        warnings.append(
            f"confidence changes {before['confidence'] or '<undeclared>'} → {after['confidence']}: "
            f"check the diff carries the evidence for the new value (WRITING.md)."
        )
    if "status" in changed and after["status"] in ("archived", "superseded"):
        warnings.append(f"status → {after['status']}: the note leaves default retrieval.")
    if after["status"] == "superseded" and "superseded_by" in changed:
        warnings.append(
            f"superseded_by {after['superseded_by']} is not checked to exist, here or at compose."
        )

    return {
        "diff": diff,
        "spine_before": before,
        "spine_after": after,
        "spine_changed": changed,
        "passages": int((result.run.get("chunk") or {}).get("passages", 0)),
        "warnings": warnings,
        "pinned_doc_id": pinned,
    }, content


def _pin_declined(doc_id: str, content: bytes) -> NoteError:
    """The refusal for a pin `_pin` would not write, naming the line to write it under by its
    CONTENT: "the first line" also reads as the opening `---` fence, and a line written directly
    under the fence is exactly the placement that closes the block (crosscheck, 2026-11-04)."""
    # The line is NOTE CONTENT echoed into an error: marked like the diff marks it (a bidi override
    # or an escape sequence would otherwise reach the reader raw), quoted with repr so a backtick
    # in it cannot end the quote, and cut short — it only has to identify a line that is unique,
    # and an 8 MiB first line would otherwise come back twice (crosscheck, 2026-11-05).
    shown, _, _ = _visible(_first_key_line(content))
    first = repr(shown if len(shown) <= 80 else shown[:80] + "…")
    return NoteError(
        f"amend could not write `doc_id: {doc_id}` into this frontmatter: a line placed above "
        f"its first line, {first}, would change how the block reads (above a `---` document "
        f"marker, the block would end or YAML would read two documents). Write `doc_id: {doc_id}` "
        f"yourself, on its own line directly below the {first} line (the first line inside the "
        f"block, not the opening `---` fence), where a new line changes nothing else."
    )


def _first_key_line(content: bytes) -> str:
    """The first non-blank line inside the frontmatter block, as the reader sees it."""
    from substrate.markdown.reader import _FRONTMATTER, normalized_text

    m = _FRONTMATTER.match(normalized_text(content))
    return next((line for line in m.group(1).splitlines() if line.strip()), "") if m else ""


def _declined_by_block(content: bytes) -> bool:
    """GIVEN that `_pin` returned a pinnable note's content unchanged, whether that was because of
    the block's layout: it has frontmatter keys and none is `doc_id`, so only the check that the
    reader still sees the same keys can have refused the pin. False for content with no keys to pin
    into at all. Not a predicate on its own: ordinary keyed content `_pin` pins fine answers True."""
    from substrate.markdown.reader import frontmatter_pairs, normalized_text

    try:
        pairs = frontmatter_pairs(normalized_text(content))
    except UnicodeDecodeError:
        return False
    return bool(pairs) and not any(k == "doc_id" for k, _ in pairs)


def _pin(content: bytes, doc_id: str) -> bytes:
    """`content` with `doc_id: <doc_id>` as its first frontmatter line, or `content` unchanged when
    there is nothing to pin into. Called only for an id the reader would honour as declared
    (`_check_amend` decides that, and reports the pin).

    Left unchanged: content the caller already gave a `doc_id:` line (the identity gate judges it);
    content with no frontmatter, an empty block, or a block the reader takes for a thematic break
    (the spine gate refuses it); and content whose block reads differently with a line above its
    first line, a `---` document marker (`_pin_declined` says where to write the line instead). A value the reader cannot read is left out of the pairs compared below, and
    the strict gate refuses it when the engine reads that key. The note's own line ending is used,
    so a CRLF or CR note does not gain one LF line.
    """
    # Only an ABSENT line is written in. A `doc_id:` line the caller sent — usable, empty or
    # ill-shaped — is theirs, and the identity gate judges it rather than this rewriting it.
    pairs = _pairs(content)
    if not pairs or any(k == "doc_id" for k, _ in pairs):
        return content
    # Written as it is, unquoted, as the pin always has been: a declarable id is plain text.
    return _prepend_key(content, "doc_id", doc_id, written=doc_id) or content


def _pairs(content: bytes) -> list[tuple[str, str]] | None:
    """The frontmatter's (key, value) pairs as the reader reads them, or None (no block, not
    UTF-8)."""
    from substrate.markdown.reader import frontmatter_pairs, normalized_text

    try:
        return frontmatter_pairs(normalized_text(content))
    except UnicodeDecodeError:
        return None


def _opener(content: bytes) -> bytes | None:
    for opener in (b"---\r\n", b"---\n", b"---\r"):
        if content.startswith(opener):
            return opener
    return None


def _prepend_key(content: bytes, key: str, value: str, *,
                 written: str | None = None) -> bytes | None:
    """`content` with `key: value` as its first frontmatter line, or None when it cannot be put
    there and leave every other key reading as it did. The value is written as a YAML scalar
    (`yaml_scalar.encode`) unless `written` gives its text. The note's own line ending is used, so
    a CRLF or CR note does not gain one LF line."""
    from substrate.markdown import yaml_scalar
    from substrate.markdown.reader import _FRONTMATTER, normalized_text

    opener, pairs = _opener(content), _pairs(content)
    if opener is None or not pairs:
        return None
    eol = opener[3:]
    # At the block's own key indent, a comment's aside: a line left of it would make every key
    # below it indented.
    block = _FRONTMATTER.match(normalized_text(content)).group(1).split("\n")
    first = next((ln for ln in block if ln.strip() and not ln.lstrip().startswith("#")), "")
    indent = first[:len(first) - len(first.lstrip(" \t"))]
    line = f"{indent}{key}: {yaml_scalar.encode(value) if written is None else written}".encode()
    out = opener + line + eol + content[len(opener):]
    # The reader must see EXACTLY the old keys plus this one. Above a first line that is a `---`
    # document marker, the new line ends the block (`---`) or YAML reads two documents (`--- # c`)
    # (crosscheck, 2026-11-04). Left unwritten, the gates
    # judge the caller's own content instead of a block this function broke.
    if _pairs(out) != [(key, value)] + pairs:
        return None
    return out


def _replace_key(content: bytes, key: str, value: str) -> bytes | None:
    """`content` with its one `key:` frontmatter line rewritten to `key: value`, keeping its indent
    and line ending, or None when no single line can be rewritten so that only that key's value
    changes."""
    from substrate.markdown import yaml_scalar

    pairs = _pairs(content)
    if not pairs or sum(1 for k, _ in pairs if k == key) != 1:
        return None
    expected = [(k, value if k == key else v) for k, v in pairs]
    lines = content.splitlines(keepends=True)
    for i, raw in enumerate(lines[1:], start=1):
        text = raw.decode("utf-8", "replace")
        body = text.rstrip("\r\n")
        if body.partition(":")[0].strip() != key or ":" not in body:
            continue
        indent = body[:len(body) - len(body.lstrip(" \t"))]
        line = f"{indent}{key}: {yaml_scalar.encode(value)}".encode() + raw[len(body.encode()):]
        written = b"".join([*lines[:i], line, *lines[i + 1:]])
        if _pairs(written) == expected:
            return written
    return None


# `writelog.STAMP_KEYS` are the frontmatter keys the ENGINE writes for a granted client's
# write. A note that declares one with any other value is refused, so a well-behaved client's note
# says who wrote it. They are labels: a key spelled another way, or the file edited after, gets past
# this. The record is the engine's write log, and search shows a stamp the log contradicts as that.


def _values(pairs: list[tuple[str, str]] | None, key: str) -> list[str]:
    return [v for k, v in pairs or () if k == key]


def _unstampable(author: str, key: str) -> NoteError:
    return NoteError(
        f"this note is written for the client `{author}`, so the engine records it in the "
        f"frontmatter as `{key}: {author}`, and it cannot put that line into this note's "
        f"frontmatter block without changing how the rest of it reads. Give the note an ordinary "
        f"`---` frontmatter block whose first line inside it is not a `---` document marker."
    )


def stamp_new(content: bytes, author: str) -> bytes:
    """`ingest` content for a granted client, with `written_by: <author>` in its frontmatter.

    A note may already say `written_by: <author>` (it is then left as it is). Any other
    `written_by`, two of them, or any `amended_by` (a new note has not been amended) is refused."""
    pairs = _pairs(content)
    if not pairs:
        raise NoteError(
            f"a note written for the client `{author}` carries `written_by: {author}` in its "
            f"frontmatter, and this note has no frontmatter block to carry it. Every note needs one "
            f"anyway: `status`, `doc_type` and `confidence` are required.")
    written = _values(pairs, "written_by")
    if written and written != [author]:
        raise NoteError(
            f"the note declares `written_by: {', '.join(written)}`, but it is written for the "
            f"client `{author}`, and the engine records the client that writes a note. Leave the "
            f"`written_by:` line out; the engine writes it.")
    if _values(pairs, "amended_by"):
        raise NoteError("the note declares `amended_by`, which the engine writes when a note is "
                        "amended. A new note has not been; leave the line out.")
    if written:
        return content
    stamped = _prepend_key(content, "written_by", author)
    if stamped is None:
        raise _unstampable(author, "written_by")
    return stamped


def stamp_amend(content: bytes, base: bytes, author: str) -> bytes:
    """`amend` content for a granted client, with `amended_by: <author>` in its frontmatter.

    `written_by` must read exactly as it does in the note now: it names who wrote the note, and an
    edit does not change that. `amended_by` may be left as the note has it, set to `<author>`, or
    left out; it is then written as `<author>`. Any other value is refused."""
    pairs, before = _pairs(content), _pairs(base)
    if not pairs:
        # No keys to stamp into, so the spine gate refuses the note in its own words.
        return content
    if _values(pairs, "written_by") != _values(before, "written_by"):
        was = _values(before, "written_by")
        keep = (f"keep `written_by: {was[0]}` as the note has it" if len(was) == 1
                else "leave `written_by` out, as the note does" if not was
                else "keep the `written_by` lines as the note has them")
        raise NoteError(
            f"`written_by` records who wrote the note, and an amend does not change it: {keep}. "
            f"The engine records this edit as `amended_by: {author}`.")
    amended = _values(pairs, "amended_by")
    if len(amended) > 1 or (amended and amended[0] not in (author, *_values(before, "amended_by"))):
        raise NoteError(
            f"the note declares `amended_by: {', '.join(amended)}`, but this edit is made for the "
            f"client `{author}`, and the engine records `amended_by: {author}` itself. Leave the "
            f"line as the note has it, or leave it out.")
    if amended == [author]:
        return content
    stamped = (_replace_key(content, "amended_by", author) if amended
               else _prepend_key(content, "amended_by", author))
    if stamped is None:
        raise _unstampable(author, "amended_by")
    return stamped


def _doc_id_lines(content: bytes) -> int:
    """How many `doc_id` keys the frontmatter holds, as the reader reads it — its dict keeps only
    the last of several, so it cannot see a duplicate; `frontmatter_pairs` can. 0 when there is no
    frontmatter, or the bytes are not UTF-8."""
    from substrate.markdown.reader import frontmatter_pairs, normalized_text

    try:
        text = normalized_text(content)
    except UnicodeDecodeError:
        return 0
    return sum(1 for k, _ in frontmatter_pairs(text) or () if k == "doc_id")


def plan_amend(*, project_vault: Path, path: str, content: bytes, book: PlanBook,
               author: str | None = None, bound_to: str = "") -> AmendPlan:
    """Diff an existing note against `content`, run every gate, and issue a single-use token bound
    to the current bytes. Writes nothing. A refused plan issues no token. `author` and `bound_to`
    as for `plan`; the stamp is `amended_by`."""
    root, parts, target, rel = _resolve_existing(project_vault, path)
    dfd = _open_note_dir(root, parts)
    try:
        base = _read_at(dfd, parts[-1], target)
        fields, final = _check_amend(target=target, rel=rel, base=base, content=content,
                                     author=author)
        # After the gates, so a note amend would refuse anyway is not answered "make it writable".
        _require_writable_note(dfd, parts[-1], target)
        _require_writable_folder(dfd, target)
    finally:
        os.close(dfd)
    # The token is bound to what will be WRITTEN — the pinned note when a pin was added — and the
    # commit re-derives the same pin from the same base, so the two agree byte for byte.
    base_sha = _sha(base)
    token = book.issue(_amend_binding(target, base_sha, final, bound_to),
                       note=(str(target), base_sha))
    return AmendPlan(target=target, base_sha256=base_sha, new_sha256=_sha(final),
                     confirm_token=token, **fields)


def _require_writable_note(dfd: int, name: str, target: Path) -> None:
    """`rename` replaces a folder ENTRY, so it never asks the note's own permissions: a note made
    read-only (`chmod`, an ACL) was replaced as if it were writable, and a Finder-Locked one planned
    fine and then failed every commit. Asked the way a write in place would ask, at plan and again
    just before the rename."""
    if not os.access(name, os.W_OK, dir_fd=dfd, effective_ids=True, follow_symlinks=False):
        raise NoteError(
            f"{target} is read-only (its permissions, an ACL, or Finder's Locked). amend replaces "
            f"a note only where it could write it in place; make it writable first if the edit "
            f"is meant."
        )


def _require_writable_folder(dfd: int, target: Path) -> None:
    """The commit stages a file beside the note. A folder that cannot take it planned fine and then
    refused every commit with "cannot stage"; asked at plan, and again before staging."""
    if not os.access(".", os.W_OK | os.X_OK, dir_fd=dfd, effective_ids=True):
        raise NoteError(
            f"{target.parent} is read-only (its permissions, an ACL, or Finder's Locked), and amend "
            f"replaces a note by writing beside it; make the folder writable first if the edit is "
            f"meant."
        )


def _not_issued() -> NoteError:
    return NoteError(
        "confirm_token was not issued by this server for this amend, or has already been used. "
        "Call again without a token to get a current plan, confirm it with the human, and pass "
        "THAT token."
    )


def _changed_since_plan(target: Path) -> NoteError:
    return NoteError(
        f"{target} changed after the plan was made, so the diff you approved no longer describes "
        f"the edit. Nothing was written. Call amend again without a token to get a diff against "
        f"the note as it is now."
    )


def commit_amend(
    *, project_vault: Path, path: str, content: bytes, confirm_token: str, book: PlanBook,
    author: str | None = None, bound_to: str = "", who: writelog.Identity | None = None,
) -> tuple[Path, str, str]:
    """Replace the note, only for a token issued for exactly the bytes these would be written as
    (a pinned doc_id included) over exactly the bytes on disk now. Returns (target,
    previous_sha256, written_sha256).

    The token is looked up first, then the compare-and-swap, before any gate, so a note that moved
    under the plan is reported as that, rather than as whichever gate the moved note fails. ANY refusal burns the token: a
    plan only issues once every gate has passed, so a commit that is refused means something moved,
    and the plan the human approved no longer describes what would be written.
    """
    try:
        root, parts, target, rel = _resolve_existing(project_vault, path)
        dfd = _open_note_dir(root, parts)
    except NoteError:
        book.discard(confirm_token)
        raise
    try:
        previous, written = _commit_at(dfd, parts[-1], target=target, rel=rel, content=content,
                                       confirm_token=confirm_token, book=book, author=author,
                                       bound_to=bound_to, who=who)
    except NoteError:
        book.discard(confirm_token)
        raise
    finally:
        os.close(dfd)
    return target, previous, written


def _replace_at(dfd: int, name: str, target: Path, *, content: bytes, stage: str,
                unchanged: Callable[[bytes], bool], changed: Callable[[], Exception]) -> None:
    """Replace the note `name` in the folder `dfd` with `content`: staged beside it, its mode kept,
    renamed over it only if `unchanged` still holds for what it now reads and it is writable, and
    the folder fsynced. `amend`'s commit and `substrate frontmatter fix` both write through this."""
    import stat as _stat

    try:
        mode = _stat.S_IMODE(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode)
    except OSError as e:
        raise NoteError(f"cannot inspect {target}: {e}") from e
    _require_writable_folder(dfd, target)
    tmp = f".substrate-{stage}.{secrets.token_hex(8)}.tmp"
    # Staged beside the note so `rename` stays on one filesystem and is atomic. Random, O_EXCL and
    # O_NOFOLLOW: a staging name that could be predicted could be pre-planted as a symlink, which is
    # how the add path once wrote outside the vault. The `.tmp` suffix keeps a crash's leftover out
    # of compose, which only discovers `*.md`. The name is fixed-length, NOT the note's name plus a
    # suffix: that made any note name over 233 characters plan but never commit.
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    except OSError as e:
        raise NoteError(f"cannot stage the rewritten note beside {target}: {e}") from e
    try:
        try:
            # The descriptor is owned by the file object BEFORE anything else can fail, so no
            # error path leaks it.
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), mode)
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise NoteError(f"failed staging the rewritten note for {target}: {e}") from e

        # The last look before the swap. Anything that changed the note since the read above is
        # refused here; only a write inside the gap between this read and `rename` is not seen.
        if not unchanged(_read_at(dfd, name, target)):
            raise changed()
        # Here, not before the gates: a `chmod` changes no byte the checksum above could see, and
        # the gates are seconds in which one could land.
        _require_writable_note(dfd, name, target)
        try:
            # Relative to the folder descriptor, so neither name is re-resolved through a path.
            # `rename` replaces the final directory ENTRY rather than writing through it.
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as e:
            raise NoteError(f"failed replacing {target}: {e}") from e
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            # Gone already, or its folder turned read-only: the refusal on its way out is what the
            # caller needs, and a cleanup error raised over it would hide it.
            pass
        raise

    try:
        os.fsync(dfd)
    except OSError:
        # The rename has happened; a filesystem that cannot fsync a directory (some sync providers)
        # leaves its durability to that provider. Not a failure of the write.
        pass


def _commit_at(dfd: int, name: str, *, target: Path, rel: str, content: bytes,
               confirm_token: str, book: PlanBook, author: str | None = None,
               bound_to: str = "", who: writelog.Identity | None = None) -> tuple[str, str]:
    """The write itself, relative to the note's folder descriptor. Returns (replaced sha256,
    written sha256) — the written bytes may carry a pinned doc_id the caller did not send."""
    planned = book.note_of(confirm_token)
    # A token no amend plan for THIS note issued is refused before anything is read or checked:
    # the parse and the gates cost seconds under the server's lock, and an invented token must not
    # buy them.
    if planned is None or planned[0] != str(target):
        raise _not_issued()
    base = _read_at(dfd, name, target)
    base_sha = _sha(base)
    if not secrets.compare_digest(planned[1], base_sha):
        raise _changed_since_plan(target)

    fields, content = _check_amend(
        target=target, rel=rel, base=base, content=content, author=author,
        issued=lambda final: book.matches(confirm_token,
                                          _amend_binding(target, base_sha, final, bound_to)))
    if not book.redeem(confirm_token, _amend_binding(target, base_sha, content, bound_to)):
        raise _not_issued()

    doc_id = fields["spine_after"].get("doc_id")
    try:
        with writelog.recorded(who, op="amend", target=target, doc_id=doc_id, before=base_sha,
                               after=_sha(content)):
            _replace_at(dfd, name, target, content=content, stage="amend",
                        unchanged=lambda now: _sha(now) == base_sha,
                        changed=lambda: _changed_since_plan(target))
    except WriteLogError as e:
        # The log refused the intent, so the note was not touched.
        raise NoteError(f"{e} The note was not changed.") from e
    return base_sha, _sha(content)
