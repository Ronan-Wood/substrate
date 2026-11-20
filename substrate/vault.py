"""Manifest reading + scope composition — the inheritance mechanism (Doc 2 §1–2).

Option A inheritance is, in the engine, nothing more than *which source paths to index*. This
module is that and only that: read a project vault's `.substrate.toml`, resolve what it inherits
to concrete vault directories, discover the notes under the composed set, and hand back a Scope
the compose command ingests + indexes as one. No copies, no submodules, no sync.

The engine has an opinion on SHAPE, none on LOCATION (Doc 2 §0): an `inherits` entry is resolved
as an explicit path when it is one (absolute, or contains a separator) and otherwise as a sibling
directory of the project vault — so a user may keep core-vault in a cloud folder and point at it
by path, or keep it beside the project and name it. The manifest FORMAT is the system's; WHERE the
paths live is the user's.

Every refusal here is a hard failure with the condition attached, never a silent fallback to a
narrower scope — a composed query that quietly dropped the core tier and returned plausible
project-only results is the chapter-title bug (a well-formed answer from the wrong source set)
wearing a new hat. `assert_composed` is the post-index proof that it did not happen.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import unicodedata

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from substrate import quarantine
from substrate.extract.base import doc_id_from, sha256_file  # stdlib-only; no Docling/torch
from substrate.markdown.reader import (
    _DOC_ID, _DOMAIN, FrontmatterError, _blocks, _parse_frontmatter, _parse_list, normalized_text,
    unread_keys,
)
from substrate.models import Kind

MANIFEST = ".substrate.toml"

# Mirror reader._MAX_MD_BYTES: every file the vault path reads — the manifest, each `_meta.md`, and
# each note during the doc_id pre-scan — is size-checked before it is slurped, so a pathological
# file (a multi-GB cloud-synced `_meta.md`) is refused rather than driven into an OOM. The note
# reader is already hardened this way; this extends the same guard to the reads it did not cover.
_MAX_BYTES = 64 * 1024 * 1024

@dataclass(frozen=True)
class SkipList:
    """What compose never indexes in ONE vault: `files` by file name, in any folder, and `folders`
    by folder name, at any depth and with everything under them.

    An entry ending in `.md` is a file name and any other entry is a folder name. Compose only ever
    reads `.md` files, so that is the whole difference between the two, and it is why one list can
    carry both and still match each the way the two fixed sets it replaces did: a folder that
    happens to be named `log.md` skips nothing.

    Both sides are compared in one Unicode form (NFC). An accented name typed into the manifest is
    NFC, and a name on disk may be stored decomposed, so the two spellings of one name look the
    same everywhere and compared as bytes would never match. Case is compared exactly.
    """

    files: frozenset[str]
    folders: frozenset[str]

    @classmethod
    def of(cls, names: Iterable[str]) -> SkipList:
        names = frozenset(_nfc(n) for n in names)
        files = frozenset(n for n in names if n.endswith(".md"))
        return cls(files=files, folders=names - files)

    def skips_file(self, name: str) -> bool:
        """Whether a file of this name is skipped wherever it is."""
        return _nfc(name) in self.files

    def skips_folder(self, rel_parts: tuple[str, ...]) -> bool:
        """Whether a skipped folder is among these vault-RELATIVE path parts. Relative, never
        absolute: a vault that happens to live under an ancestor named e.g. `99-templates` would
        otherwise skip every note."""
        return any(_nfc(part) in self.folders for part in rel_parts)

    def skips(self, rel_parts: tuple[str, ...]) -> bool:
        """Whether the note at this vault-relative path is skipped, by a folder or by its name."""
        return self.skips_folder(rel_parts) or (bool(rel_parts) and self.skips_file(rel_parts[-1]))


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


# The skip list of a vault whose manifest declares none (`skip_list`): one layout's scaffolding,
# not retrievable notes. `_meta.md` is a source's metadata (read for domains/class by
# `_source_meta`, never indexed); `structure.md` is an orientation outline Doc 2 explicitly calls
# "not itself a retrieval target"; MEMORY.md and log.md are navigation/history; WRITING.md is the
# authoring standard, a multi-job document by nature, so it is excluded rather than chunked into
# single-job passages; templates are templates. Nothing in this package reads `structure.md` or
# `WRITING.md` — they are excluded, full stop, and a reader who needs the standard opens it
# directly. In another layout `log.md` is an ordinary note, which is why a manifest may declare its
# own list.
#
# Excluding them is not a silent drop for two reasons: the count check in assert_composed is over
# the notes actually selected here, and `_refuse_skipped_note_with_a_spine` refuses any skipped
# file that DECLARES itself a note. That second guard exists because the first argument — "they
# are not notes" — stopped being self-evident once §6a named `digest`, a doc_type that describes
# exactly what a MEMORY.md contains.
DEFAULT_SKIP = SkipList.of(
    ("_meta.md", "structure.md", "MEMORY.md", "log.md", "WRITING.md", "99-templates"))


class VaultError(RuntimeError):
    """A manifest is malformed, points at a missing vault, or composes an empty/ambiguous scope."""


def _read_capped(path: Path) -> str:
    """Read a vault file as UTF-8 (line-endings normalized), refusing a pathologically large one
    before it is slurped into memory. VaultError on any read failure or over-cap size."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise VaultError(f"cannot stat {path}: {e}") from e
    if size > _MAX_BYTES:
        raise VaultError(f"{path} is {size} bytes (> {_MAX_BYTES} cap) — refusing to read.")
    try:
        return normalized_text(path.read_bytes())
    except (OSError, UnicodeDecodeError) as e:
        raise VaultError(f"cannot read {path}: {e}") from e


@dataclass(frozen=True)
class VaultRef:
    name: str          # the vault DIRECTORY name — the composition-provenance key
    path: Path
    # What this vault's OWN manifest skips (`skip_list`). `resolve_vaults` reads it once, so the
    # discovery and the prune of one compose cannot work from two readings of it. No default: a
    # VaultRef built without one would carry a list its vault never declared.
    skip: SkipList


@dataclass(frozen=True)
class NoteRef:
    path: Path
    vault: str
    tier: int                       # 1 operator · 2 reference · 3 project — derived from location
    doc_class: str | None = None    # from a reference source's _meta.md `class`, else None
    override_status: str | None = None   # from _meta.md, applied only when the note declares none
    override_doc_type: str | None = None  # from _meta.md, applied only when the note declares none
    override_confidence: str | None = None  # ditto — a source states its settledness once
    raw: str | None = None            # §3b provenance, from the source's _meta.md
    raw_sha256: str | None = None
    raw_location: str | None = None
    override_version: str | None = None  # from a versioned source's _meta.md
    extra_domains: list[str] = field(default_factory=list)  # merged from _meta.md


class IdenticalGroup(NamedTuple):
    """Notes with the same text (the same bytes once line endings are normalized), first by path
    (`resolve_scope`). `body` is false when the text after the frontmatter is nothing but headings
    and blank lines, so the notes index no passage (`_has_body`)."""
    notes: tuple[NoteRef, ...]
    body: bool


@dataclass(frozen=True)
class Scope:
    """The composed retrieval scope: the ordered vaults (roots first, project last) and the notes
    to index across them. `vaults` is the set assert_composed proves the index actually covers.

    `name` is the manifest-DECLARED name (Doc 2 §2's `name = "atlas"`) and is deliberately not
    `project.name`, which is the vault DIRECTORY (`atlas-vault`) the composition provenance is
    keyed on. Both are real identifiers for different jobs: the directory names which vault a
    passage came from, the manifest names the scope a caller asks for.
    """

    project: VaultRef
    vaults: tuple[VaultRef, ...]
    notes: tuple[NoteRef, ...]
    name: str = ""
    # The notes discovered whose bytes cannot be read as a note (over the size cap, not UTF-8),
    # each with its `quarantine.KINDS` kind and why. They are in `notes` too, and compose
    # quarantines them.
    unreadable: Mapping[Path, tuple[str, str]] = field(default_factory=dict)
    # The project manifest's `quarantine_ceiling`, as declared (`_declared_ceiling`).
    quarantine_ceiling: int | str | None = None
    # Each note discovered and LEFT OUT because another note derives the same doc_id from the same
    # bytes, paired with the note indexed in its place (`resolve_scope`). Not in `notes`.
    duplicates: tuple[tuple[NoteRef, NoteRef], ...] = ()
    # Every set of two or more notes in `notes` with the same text, each indexed as its own note.
    identical: tuple[IdenticalGroup, ...] = ()

    @property
    def vault_names(self) -> frozenset[str]:
        return frozenset(v.name for v in self.vaults)

    def root(self, n: NoteRef) -> Path:
        """The root of the vault `n` was discovered in. By name AND containment: two vaults in a
        chain may share a directory name, and a map keyed by name keeps only one of them."""
        return next(v.path for v in self.vaults
                    if v.name == n.vault and n.path.is_relative_to(v.path))

    def relative(self, n: NoteRef) -> str:
        """`n`'s path inside its vault, as `status`, compose's output and the refresh record name
        it."""
        return n.path.relative_to(self.root(n)).as_posix()


# A TOML key that needs no quotes.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

# Every key the manifest format defines at top level, and the one set both checks below read: a key
# outside it, `UNREAD_KEYS` and tables aside, is refused at top level (`unknown_keys`), a key in it
# is refused inside a table
# (`misplaced_keys`). `identity.IDENTITY_KEY` is one of them, and a test keeps the two in step.
# `reference_pins` is the only table, and its keys are the operator's reference names, so no table
# has a fixed schema to hold unknown keys against.
TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"name", "inherits", "reference_domains", "reference_pins", "identity", "skip",
     "writable_folders", quarantine.CEILING_KEY})

# Top-level keys the engine accepts and never reads, so they are not unknown. `guard_state` was
# retired in 0.2.1 with the promise that a manifest still declaring it composes. Scripta writes the
# other two into every vault it makes, and reads them back as top-level lines, so they cannot move
# into a table. Not in TOP_LEVEL_KEYS: nothing here reads them, so no table reserves them.
UNREAD_KEYS: frozenset[str] = frozenset(
    {"guard_state", "scripta_workspace", "scripta_workspace_vault"})


def unknown_keys(manifest: dict) -> list[str]:
    """Each top-level key of a parsed manifest that the format does not define, in file order,
    unless its value is a table.

    Read as given, a misspelt key is a key nobody reads, and the default stands in for what its
    owner wrote: `skips` leaves the default skip list in effect. Compared exactly, as every key is
    read: `Skip`, and a spelling in another Unicode form, are unknown. A table is left open, at any
    name, because the format defines none with a fixed schema; that is a TOML table as `tomllib`
    returns one, so `[x]`, `x = {}` and a dotted `x.y = 1` are tables, and an array of tables
    (`[[x]]`) is an array, refused like any other value."""
    return [k for k, v in manifest.items()
            if k not in TOP_LEVEL_KEYS and k not in UNREAD_KEYS and not isinstance(v, dict)]


def _nearest_key(key: str, known: Iterable[str] | None = None) -> str | None:
    """The key in `known` (default TOP_LEVEL_KEYS) that `key` is likely a slip for, or None.
    Compared after NFKC and case folding, so `SKIP`, and `skip` in full-width letters, point at
    `skip`. Below difflib's 0.8 ratio (`skips` to `skip` is 0.89, `identities` to `identity` 0.78)
    there is no suggestion rather than a guess. A key too long to reach that ratio is not compared:
    normalising and matching a manifest-sized key would cost gigabytes on every `status`. The CLI
    matches an unknown command the same way."""
    known = sorted(TOP_LEVEL_KEYS if known is None else known)
    if len(key) > 2 * max(map(len, known)):
        return None
    folded = unicodedata.normalize("NFKC", key).casefold()
    near = difflib.get_close_matches(folded, known, n=1, cutoff=0.8)
    return near[0] if near else None


# How much of the unknown keys a refusal shows. It reaches every client of `status` and every
# refresh log line, and a manifest under the size cap can hold a key of millions of characters.
_SHOWN_KEYS = 10
_SHOWN_CHARS = 40


def _shown(key: str) -> str:
    """`key` as `repr` prints it, at most _SHOWN_CHARS printed characters between its quotes. Cut
    before `repr` too, which writes a character that does not print as up to ten."""
    shown = repr(key[:_SHOWN_CHARS])
    if len(key) <= _SHOWN_CHARS and len(shown) <= _SHOWN_CHARS + 2:
        return shown
    return f"{shown[:_SHOWN_CHARS + 1]}{shown[0]}... ({len(key)} characters)"


def unknown_refusal(path: Path, unknown: list[str]) -> str:
    shown = unknown[:_SHOWN_KEYS]
    more = f" and {len(unknown) - len(shown)} more" if len(unknown) > len(shown) else ""
    near = [(k, n) for k in shown if (n := _nearest_key(k))]
    if len(unknown) == 1:
        guess = f" (did you mean {near[0][1]!r}?)" if near else ""
    else:
        guess = (f" (did you mean {', '.join(f'{n!r} for {_shown(k)}' for k, n in near)}?)"
                 if near else "")
    return (f"{path}: {', '.join(map(_shown, shown))}{more} "
            f"{'is not a key' if len(unknown) == 1 else 'are not keys'} the manifest "
            f"defines{guess}, and the engine refuses a key rather than ignore it. The top-level "
            f"keys are {', '.join(sorted(TOP_LEVEL_KEYS))}, spelled exactly. A [table] of any "
            f"other name is not checked.")


def misplaced_keys(manifest: dict) -> list[tuple[str, str]]:
    """(key, table) for each top-level manifest key found inside a table, the table written as its
    TOML header would name it.

    TOML puts every key written after a `[table]` header inside that table, so a top-level key
    written below `[reference_pins]` is absent where it is read and present where nothing reads it.
    A swallowed `identity` is a string, so it would pass as a pin, and the roster would be unread.
    The path is a tuple, not a joined string: a table may be named `[""]`, and its path must not
    read as the top level. Iterative, because a manifest tomllib can parse may nest deeper than a
    recursive walk can go.
    """
    found: list[tuple[str, str]] = []
    pending: list[tuple[object, tuple[str, ...]]] = [(manifest, ())]
    while pending:
        value, where = pending.pop()
        if isinstance(value, dict):
            for key, child in reversed(list(value.items())):
                pending.append((child, (*where, key)))
            if where:
                found.extend((key, _header(where)) for key in value if key in TOP_LEVEL_KEYS)
        elif isinstance(value, list):
            pending.extend((child, where) for child in reversed(value))
    return found


def _header(path: tuple[str, ...]) -> str:
    return ".".join(p if _BARE_KEY.fullmatch(p) else json.dumps(p) for p in path)


def misplaced_refusal(path: Path, misplaced: list[tuple[str, str]]) -> str:
    keys = ", ".join(f"{key!r} is inside the [{table}] table" for key, table in misplaced)
    return (f"{path}: {keys}, where the engine never reads it. Move it above the first [table] "
            f"header: in TOML every key written after a header belongs to that table. (A pin that "
            f"really has this name needs another: the manifest reserves it.)")


def _read_manifest(vault_dir: Path) -> dict:
    path = vault_dir / MANIFEST
    text = _read_capped(path)  # size-checked; raises VaultError on read failure / over-cap
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise VaultError(f"malformed manifest {path}: {e}") from e
    misplaced = misplaced_keys(manifest)
    if misplaced:
        raise VaultError(misplaced_refusal(path, misplaced))
    unknown = unknown_keys(manifest)
    if unknown:
        raise VaultError(unknown_refusal(path, unknown))
    name = manifest.get("name")
    if not isinstance(name, str) or not name:
        raise VaultError(f"{path}: manifest has no string 'name'.")
    inherits = manifest.get("inherits", [])
    if not isinstance(inherits, list) or not all(isinstance(x, str) for x in inherits):
        raise VaultError(f"{path}: 'inherits' must be a list of strings.")

    # All four Doc 2 §2 keys are shape-checked, not just the two the engine acts on today. The
    # manifest FORMAT is the system's contract; a malformed one that parses cleanly and yields a
    # wrong scope silently is the chapter-title shape — well-formed, wrong, green.
    #
    # This exists because of a real defect: `reference_domains` was written BELOW `[reference_pins]`
    # in both shipped manifests, and in TOML every key after a table header belongs to that table.
    # It parsed as `reference_pins.reference_domains`, so the vault's declared domains never existed
    # at top level and the pins table gained a bogus entry. Nothing noticed for a whole phase,
    # because both features that would read those keys are deferred — the value was declared,
    # valid-looking, and read by nobody. Checking shape at PARSE time converts that into an
    # immediate loud failure instead of a surprise whenever the deferred feature lands.
    pins = manifest.get("reference_pins", {})
    if not isinstance(pins, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in pins.items()
    ):
        raise VaultError(
            f"{path}: 'reference_pins' must be a table of string→string (name = \"version\"); "
            f"got {pins!r}."
        )
    domains = manifest.get("reference_domains", [])
    if not isinstance(domains, list) or not all(isinstance(x, str) for x in domains):
        raise VaultError(
            f"{path}: 'reference_domains' must be a top-level list of strings; got {domains!r}. "
            "If it looks absent, check it is not written BELOW a [table] header — in TOML every "
            "key after one belongs to that table."
        )
    # A pin whose value is not a plausible version, or a domain that is not a well-shaped tag, is
    # a typo that would otherwise surface as "why did this resolve to nothing" much later.
    bad_domains = [d for d in domains if not _DOMAIN.fullmatch(d)]
    if bad_domains:
        raise VaultError(f"{path}: malformed domain tag(s) {bad_domains}.")
    _declared_skip(manifest, path)
    _declared_ceiling(manifest, path)
    return manifest


def _declared_ceiling(manifest: dict, path: Path) -> int | str | None:
    """The manifest's `quarantine_ceiling`: None when it declares none, else a count of notes, 0 or
    more, or `quarantine.NO_CEILING`. Anything else refuses, because a ceiling misread as the
    default lets a mostly broken scope index, or freezes one its owner meant to let through. Every
    vault's is checked; only the project vault's applies (`Scope.quarantine_ceiling`)."""
    if quarantine.CEILING_KEY not in manifest:
        return None
    value = manifest[quarantine.CEILING_KEY]
    if value == quarantine.NO_CEILING or (
            isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        return value
    raise VaultError(
        f"{path}: '{quarantine.CEILING_KEY}' must be a whole number of notes, 0 or more, or "
        f"\"{quarantine.NO_CEILING}\" for no ceiling; it is {type(value).__name__} "
        f"{value!r}. 0 refuses the scope when any note fails.")


def _declared_skip(manifest: dict, path: Path) -> SkipList:
    """The skip list the parsed manifest at `path` puts in effect: its `skip`, else DEFAULT_SKIP.
    An empty list is a declaration, and skips nothing.

    A `skip` the engine cannot apply refuses, because ignoring it indexes files the vault's owner
    wrote down as skipped. That covers a value that is not a list of strings, and an entry that is
    not one exact name (`_not_a_name`).

    The refusal names the faulty entries by position rather than quoting them, because this text
    reaches every client of `status`. That does not make the list private: a write refused for a
    skipped name, and a refresh report's `pruned:` lines, still say what it skips.
    """
    if "skip" not in manifest:
        return DEFAULT_SKIP
    skip = manifest["skip"]
    if not isinstance(skip, list):
        raise VaultError(f"{path}: 'skip' must be a top-level list of strings, each one file or "
                         f"folder name; it is a {type(skip).__name__}.")
    not_strings = [i for i, x in enumerate(skip, 1) if not isinstance(x, str)]
    if not_strings:
        raise VaultError(f"{path}: 'skip' must be a list of strings, each one file or folder name; "
                         f"entries {not_strings} (counting from 1) are not strings.")
    unmatchable = [i for i, x in enumerate(skip, 1) if _not_a_name(x)]
    if unmatchable:
        raise VaultError(
            f"{path}: 'skip' entries {unmatchable} (counting from 1) would match nothing. Each "
            f"entry is one file name or one folder name, compared exactly with the names in every "
            f"folder of the vault. It is not a path and not a pattern, so it is never empty, `.` "
            f"or `..`, has no white space at either end, and holds no `/`, `\\`, `*`, `?` or NUL: "
            f"write \"99-templates\", not \"99-templates/\" or \"*-templates\"."
        )
    return SkipList.of(skip)


def _not_a_name(entry: str) -> bool:
    """Whether a `skip` entry is something other than one exact file or folder name.

    Entries are compared with single path components, so an empty one, `.`, `..` or one holding a
    path separator or a NUL can equal none. Space at either end, `*` and `?` can be in a name on
    this machine, and are refused all the same: an entry written that way is far likelier a typing
    slip or a `.gitignore` pattern than a real name, and read as a name it skips nothing while its
    owner believes it does.
    """
    return (not entry or entry in (".", "..") or entry != entry.strip()
            or any(c in entry for c in "/\\*?\0"))


def skip_list(vault_dir: Path) -> SkipList:
    """The skip list in effect for ONE vault: the `skip` its own manifest declares, else
    DEFAULT_SKIP. A vault with no manifest (a root vault need not carry one) has the default.

    Each vault's list governs its own files and nobody else's. A project vault declaring
    `skip = []` therefore indexes its own `log.md` and leaves the housekeeping files of the vaults
    it inherits skipped.

    ONLY `skip` IS READ, as `chain_best_effort` reads only `inherits`. The write path asks this
    about a vault it is not composing, and a manifest that fails validation for an unrelated key
    still says what it skips. A manifest that cannot be read or parsed, a `skip` that is misplaced
    or malformed, or an unknown key in a manifest declaring no `skip` (`_unknown_may_be`) makes the
    list unknowable, and that raises VaultError.
    """
    read = _own_key(vault_dir, "skip", "what the vault skips")
    if read is None:
        return DEFAULT_SKIP
    path, manifest = read
    return _declared_skip(manifest, path)


def folder_fault(entry: str) -> str | None:
    """Why `entry` is not a folder path relative to a vault, as the write path takes one, or None.

    `/` separates folders, and the vault root is `.`, written alone. Refused: empty; absolute; a
    NUL or a `\\` (a separator elsewhere, so far likelier a slip than a name); a separator at the
    end or doubled; `.` or `..` as a component; a hidden component; a component with white space at
    either end, refused as `skip` refuses one (`_not_a_name`); a component ending in `.md`, which
    compose's walk would take for a note and fail to read, refusing the scope. Nothing is
    normalised, so the folder checked is the folder written.
    """
    if entry == ".":
        return None
    if not entry:
        return "is empty"
    if "\0" in entry or "\\" in entry:
        return "holds a NUL or a `\\`; folders are separated by `/`"
    if entry.startswith("/"):
        return "is absolute; it is relative to the vault"
    if entry.endswith("/") or "//" in entry:
        return "ends with `/` or doubles one"
    parts = entry.split("/")
    if any(p in (".", "..") for p in parts):
        return "names `.` or `..` as a folder (the vault root is `.` alone)"
    if any(p.startswith(".") for p in parts):
        return "names a hidden folder, which is never a note's"
    if any(p != p.strip() for p in parts):
        return "has white space at either end of a folder name"
    if any(folded(p).endswith(".md") for p in parts):
        return "names a folder ending in `.md`, which compose would read as a note"
    return None


def folder_parts(entry: str) -> tuple[str, ...]:
    """The components of a folder path `folder_fault` accepts; `()` for the root, `.`."""
    return () if entry == "." else tuple(entry.split("/"))


def _own_key(vault_dir: Path, key: str, what: str) -> tuple[Path, dict] | None:
    """(manifest path, parsed manifest) for a reader of ONE key, or None when the vault has no
    manifest. Refuses, as VaultError, a manifest that cannot be read or parsed (`what` says what
    that leaves unknown), a `key` swallowed into a table, and an unknown key while `key` is absent
    (`_unknown_may_be`). The other keys are not validated: a fault elsewhere in the manifest does
    not change what this key says."""
    path = vault_dir / MANIFEST
    if not path.is_file():
        return None
    try:
        manifest = tomllib.loads(_read_capped(path))
    except (tomllib.TOMLDecodeError, RecursionError, ValueError) as e:
        raise VaultError(f"cannot parse {path}, so {what} is unknown: {e}") from e
    swallowed = [(k, t) for k, t in misplaced_keys(manifest) if k == key]
    if swallowed:
        raise VaultError(misplaced_refusal(path, swallowed))
    _unknown_may_be(manifest, path, key, f"{what} is unknown")
    return path, manifest


def _unknown_may_be(manifest: dict, path: Path, key: str, unknowable: str) -> None:
    """Refuse, for a reader of ONE key, an unknown key in a manifest that does not declare `key`:
    the unknown one may be `key` misspelt, and reading `key` as absent puts the default in place of
    what was written. With `key` declared, no unknown key changes what it says, and compose is
    what refuses the manifest."""
    if key not in manifest and (unknown := unknown_keys(manifest)):
        raise VaultError(f"{unknown_refusal(path, unknown)} An unknown key may be {key!r} "
                         f"spelled another way, so while it is there {unknowable}.")


def writable_folders(vault_dir: Path, skip: SkipList) -> tuple[tuple[str, ...], ...] | None:
    """The folders a vault's OWN manifest declares writable (`writable_folders`), each as its
    components (`()` is the root, `.`) as written, or None when it declares none. Read for the write
    path only:
    compose indexes the same notes whatever it says. `skip` is the vault's own list in effect.

    ONLY `writable_folders` IS READ (`_own_key`). A declaration the engine cannot apply raises
    VaultError, naming entries by position: no manifest at all; not a list of strings; an entry
    `folder_fault` refuses;
    or one `skip` skips, where a note would never be indexed. Whether each entry exists on disk is
    the write path's to check, the way it reaches folders (`notes._writable`).
    """
    read = _own_key(vault_dir, "writable_folders", "where a note may be written")
    if read is None:
        # A registered scope's project vault has a manifest. Without one (moved, or mid-save in an
        # editor that unlinks first) nothing says where a write may go, and None would widen a
        # declared list to the default. Decided on this one read, not a check before it.
        raise VaultError(f"{vault_dir} has no {MANIFEST}, so where a note may be written is "
                         f"unknown.")
    if "writable_folders" not in read[1]:
        return None
    path, declared = read[0], read[1]["writable_folders"]
    what = "'writable_folders' must be a top-level list of folder paths relative to the vault"
    if not isinstance(declared, list):
        raise VaultError(f"{path}: {what}; it is a {type(declared).__name__}.")
    not_strings = [i for i, x in enumerate(declared, 1) if not isinstance(x, str)]
    if not_strings:
        raise VaultError(f"{path}: {what}; entries {not_strings} (counting from 1) are not "
                         f"strings.")
    malformed = [i for i, x in enumerate(declared, 1) if folder_fault(x)]
    if malformed:
        raise VaultError(
            f"{path}: 'writable_folders' entries {malformed} (counting from 1) are not folder "
            f"paths. Each is relative to the vault, with `/` between folders and `.` alone for the "
            f"root: never empty or absolute, no `..`, `.` or hidden folder, no `\\` or NUL, no "
            f"`/` at the end or doubled, no white space at either end of a name, and no folder "
            f"ending in `.md`.")
    folders = tuple(folder_parts(x) for x in declared)
    skipped = [i for i, parts in enumerate(folders, 1) if skip.skips_folder(parts)]
    if skipped:
        raise VaultError(
            f"{path}: 'writable_folders' entries {skipped} (counting from 1) are under a folder "
            f"the vault's skip list skips, so a note written there would never be indexed.")
    return folders


def _resolve_inherit(entry: str, project_dir: Path) -> Path:
    """An inherits entry → a vault directory. Absolute path used as-is, else project-relative.

    Location is the user's (Doc 2 §0): an ABSOLUTE path is honoured as given (a cloud-synced
    core-vault, a NAS mount). Everything else — a bare name OR a relative path with separators
    (`../core`, `shared/core`) — resolves against the PROJECT VAULT's parent, never the process
    CWD, so composition is deterministic regardless of where `compose` is invoked. A missing
    directory is a hard failure: resolving to nothing and proceeding project-only is exactly the
    silent core-tier drop this module refuses.
    """
    p = Path(entry).expanduser()
    resolved = p if p.is_absolute() else (project_dir.parent / entry)
    if not resolved.is_dir():
        raise VaultError(
            f"inherited vault {entry!r} resolves to {resolved}, which does not exist. Refusing to "
            "compose a scope missing a vault it declares — a query would silently drop that tier."
        )
    return resolved


def _tier_for(rel_parts: tuple[str, ...]) -> int:
    """A note's tier from where it lives: operator (1), reference (2), or project (3). Derived from
    location, not a vault-level flag, because core-vault holds both tier-1 and tier-2 content."""
    if any(part.startswith("00-operator") for part in rel_parts):
        return 1
    if any(part.startswith("10-reference") for part in rel_parts):
        return 2
    return 3


# A reference source's metadata file, which `_source_meta` reads whether or not a skip list names
# it. So it is the one skipped file name NOT guarded by `_refuse_skipped_note_with_a_spine`: spine
# keys there are its job rather than a misplaced note. Every other file a skip list names is one
# nothing reads, which a writer could plausibly believe is an indexable note. For the same reason
# the write path never writes one, whatever the list says (`notes._source_meta_refusal`).
SOURCE_META = "_meta.md"


def is_source_meta(name: str) -> bool:
    """Whether a file of this name may be read as a source's `_meta.md`. Compared folded, because
    a volume that ignores case serves `_META.md` to `_source_meta`'s open of `_meta.md`, and
    without the code points HFS+ ignores in a name, which it serves the same way. Folded on every
    platform, so the write path's refusal is one rule wherever the vault lives."""
    return folded(name) == folded(SOURCE_META)


def folded(name: str) -> str:
    """A name as a volume that ignores case compares it: NFC, without the code points HFS+ leaves
    out of a name, and case folded. For a refusal that must hold wherever the vault lives."""
    return _nfc(name).translate(_HFS_IGNORED).casefold()


# The code points HFS+ leaves out when it compares two names, as git lists them for `.git`.
_HFS_IGNORED = dict.fromkeys(
    [*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF])

# The `_meta.md` fields `_source_meta` reads, which its passages inherit.
_META_KEYS: frozenset[str] = frozenset({"class", "document_class", "status", "doc_type",
                                        "confidence", "raw", "raw_sha256", "raw_location",
                                        "domains", "version"})


def source_meta_path(note_path: Path, vault_root: Path) -> Path | None:
    """The `_meta.md` whose spine a passage inherits, or None: the source folder is the nearest
    enclosing `passages/` folder's parent, found from the note's path INSIDE the vault, and the
    file counts only when it is a file inside the vault. The one resolution, which `_source_meta`
    reads and compose's ingest judges (`markdown.ingest._judge_opening`)."""
    try:
        rel_parts = note_path.relative_to(vault_root).parts
    except ValueError:
        return None
    if "passages" not in rel_parts:
        return None
    src_dir = vault_root.joinpath(*rel_parts[: len(rel_parts) - 1 - rel_parts[::-1].index("passages")])
    meta_file = src_dir / SOURCE_META
    # Belt-and-braces: the slice above cannot escape, but an `_meta.md` outside the vault must never
    # govern a note inside it, so assert the resolved path rather than trusting the arithmetic.
    if not meta_file.is_file() or vault_root not in meta_file.parents:
        return None
    return meta_file


def _source_meta(note_path: Path, vault_root: Path) -> dict:
    """`_meta.md` context for a reference passage: the `class`/`status`/`doc_type`/`confidence`/
    `domains` a passage under `<source>/passages/` inherits from its source. Empty for a note that
    is not under a passages/ dir, or whose source has no _meta.md. The _meta is read, never indexed.

    Resolved against the VAULT-RELATIVE path, and only ever from inside the vault. Matching the
    absolute path instead meant the first ancestor named `passages` won, so a vault that merely
    happened to live under such a directory inherited an `_meta.md` from OUTSIDE the composed scope
    — a file the manifest never authorised, silently supplying a note's class and (since these
    fields joined it) its stated settledness. `_discover_notes` already guards skipped folders this
    exact way, for this exact reason. `rindex` rather than `index` so a nested `passages/…/passages/`
    resolves to the NEAREST enclosing source, not the outermost.
    """
    meta_file = source_meta_path(note_path, vault_root)
    if meta_file is None:
        return {}
    # Strict over the fields read below: nothing ingests a `_meta.md` itself, so one of them the
    # reader cannot read must refuse here, or every passage under it loses it without a word.
    try:
        front, _ = _parse_frontmatter(_read_capped(meta_file), strict=True, keys=_META_KEYS)
    except FrontmatterError as e:
        raise VaultError(f"{meta_file}: {e}. Its passages inherit this field, so refusing rather "
                         f"than composing them without it.") from e
    return {
        # Doc 2 writes reference metadata as `class:`; the engine's field is `document_class`.
        "doc_class": front.get("class") or front.get("document_class"),
        "status": front.get("status"),
        "doc_type": front.get("doc_type"),
        "confidence": front.get("confidence"),
        # §3b: the raw pointer lives on the SOURCE, so its passages inherit it —
        # that is the whole mechanism by which a chunk can name the PDF it came from.
        "raw": front.get("raw"),
        "raw_sha256": front.get("raw_sha256"),
        "raw_location": front.get("raw_location"),
        "domains": _parse_list(front.get("domains")),
        "version": front.get("version"),
    }


# The keys whose presence means "this file declared itself a retrievable note". `title` and `class`
# are deliberately absent — a navigation file may legitimately carry either without claiming to be
# indexed, so refusing on them would be a false positive.
_SPINE_KEYS: frozenset[str] = frozenset({"doc_type", "status", "confidence", "domains"})


def _refuse_skipped_note_with_a_spine(path: Path) -> None:
    """Refuse a skipped file that declares a spine, rather than dropping it silently.

    `path` is a file its vault's skip list names, the default list or a declared one. Doc 2 §4
    names `00-index/MEMORY.md` as the project content map, and §6a's `digest` is exactly that
    shape — so the obvious move is to put `doc_type: digest` on MEMORY.md, and before this check
    that produced a fully green compose with the note absent from the index. DEFAULT_SKIP's own
    comment argues the exclusion is not a silent drop because these files "are not notes"; that
    reasoning held only while no doc_type could describe them, and `digest` ended it.

    Uses `_parse_frontmatter` rather than a local parser. A second implementation of "is this
    frontmatter" disagreed with the reader in BOTH directions: it refused a block the reader
    classifies as a thematic break (so the file has no spine at all) and it missed a real
    declaration past a fixed read window. Sharing the reader's rule is what makes "declared" here
    mean the same thing it means at ingest. A spine key whose value the reader cannot read is
    declared all the same: it is written there, and YAML reads it.

    An unreadable file is skipped, not refused: it cannot have declared anything, and these paths
    were never opened before this check existed — so a broken symlink or a non-UTF-8 `log.md` must
    not become the reason a whole scope fails to compose.
    """
    if path.name == SOURCE_META:
        return
    try:
        text = _read_capped(path)
    except VaultError:
        return
    front, _ = _parse_frontmatter(text)
    declared = sorted({k for k in front if k in _SPINE_KEYS}
                      | {k for k, _ in unread_keys(text) if k in _SPINE_KEYS})
    if not declared:
        return
    raise VaultError(
        f"{path} declares spine fields ({', '.join(declared)}) but its filename is in its vault's "
        f"skip list, so it is never indexed. Refusing rather than dropping a file that declared "
        f"itself retrievable. If {path.name} is a note, take the name out of the list: `skip` in "
        f"the vault's {MANIFEST}, which skips {sorted(DEFAULT_SKIP.files | DEFAULT_SKIP.folders)} "
        f"when it declares none. If it is not a note, remove those fields from it, or move it "
        f"under a skipped folder. (Doc 2 §6a: a `digest` is an ordinary indexed note. Give it a "
        f"normal filename under 02-areas/ or 04-synthesis/, not MEMORY.md.)"
    )


def _discover_notes(vault: VaultRef) -> list[NoteRef]:
    notes: list[NoteRef] = []
    skip = vault.skip
    for path in sorted(vault.path.rglob("*.md")):
        rel_parts = path.relative_to(vault.path).parts
        if skip.skips_folder(rel_parts):
            continue
        if skip.skips_file(path.name):
            _refuse_skipped_note_with_a_spine(path)
            continue
        meta = _source_meta(path, vault.path)
        notes.append(NoteRef(
            path=path, vault=vault.name, tier=_tier_for(rel_parts),
            doc_class=meta.get("doc_class"), override_status=meta.get("status"),
            override_doc_type=meta.get("doc_type"),
            override_confidence=meta.get("confidence"),
            raw=meta.get("raw"), raw_sha256=meta.get("raw_sha256"),
            raw_location=meta.get("raw_location"),
            override_version=meta.get("version"), extra_domains=list(meta.get("domains", [])),
        ))
    return notes


def resolve_vaults(project_vault: Path) -> tuple[VaultRef, ...]:
    """The composed vault chain, roots first — inheritance only, no note discovery.

    Split out so a caller that only needs "what does this scope compose" (scope discovery, a
    freshness check) does not pay for walking every note, and — more importantly — so it reads the
    SAME resolution the compose path does. A second, cheaper implementation of "which vaults" is
    how a discovery surface comes to disagree with what is actually indexed.
    """
    project_vault = project_vault.expanduser().resolve()
    if not (project_vault / MANIFEST).is_file():
        raise VaultError(
            f"{project_vault} has no {MANIFEST}. A project vault must carry a manifest — refusing "
            "to guess a scope."
        )

    # Resolve the inheritance chain, roots first, with cycle detection. core-vault has no manifest
    # (it is the root) so recursion bottoms out there; a vault that DOES declare inherits is
    # followed, so the chain is general, not hard-coded to one level.
    ordered: list[VaultRef] = []
    seen_paths: set[Path] = set()

    def visit(vault_dir: Path, chain: tuple[Path, ...]) -> None:
        vault_dir = vault_dir.resolve()
        if vault_dir in chain:
            raise VaultError(f"inheritance cycle: {' -> '.join(str(p) for p in (*chain, vault_dir))}")
        if vault_dir in seen_paths:
            return
        manifest_path = vault_dir / MANIFEST
        skip = DEFAULT_SKIP
        if manifest_path.is_file():
            manifest = _read_manifest(vault_dir)
            skip = _declared_skip(manifest, manifest_path)
            for entry in manifest.get("inherits", []):
                visit(_resolve_inherit(entry, vault_dir), (*chain, vault_dir))
        seen_paths.add(vault_dir)
        ordered.append(VaultRef(name=vault_dir.name, path=vault_dir, skip=skip))

    visit(project_vault, ())
    return tuple(ordered)


def inherited_entries(project_vault: Path) -> list[tuple[Path, str, Path]]:
    """Every vault the chain names, whether or not it exists: (the manifest naming it, the entry as
    written, the path it resolves to as `_resolve_inherit` resolves it). For PROTECTION only, as
    `chain_best_effort` is, and built on it: the manifests read are those of the vaults it reaches,
    and its refusals (a manifest that cannot be read, an `inherits` that is not a list) are its."""
    found: list[tuple[Path, str, Path]] = []
    for vault_dir in chain_best_effort(project_vault):
        manifest = vault_dir / MANIFEST
        if not manifest.is_file():
            continue
        try:
            data = tomllib.loads(_read_capped(manifest))
        except (tomllib.TOMLDecodeError, RecursionError, ValueError, VaultError):
            continue  # changed since `chain_best_effort` read it; that read is what refuses
        inherits = data.get("inherits", [])
        for entry in inherits if isinstance(inherits, list) else []:
            if isinstance(entry, str) and entry:
                try:
                    p = Path(entry).expanduser()
                except (RuntimeError, ValueError) as e:
                    # Skipped, an entry could name a vault inside the project vault unseen.
                    raise VaultError(f"{manifest}: `inherits` entry {entry!r} does not resolve "
                                     f"({e}).") from e
                found.append((manifest, entry, p if p.is_absolute() else vault_dir.parent / entry))
    return found


def chain_best_effort(project_vault: Path) -> tuple[Path, ...]:
    """The vaults a scope's chain still names when `resolve_vaults` refuses it. For PROTECTION
    only — composing such a chain is still refused.

    ONLY `inherits` IS READ, AND LENIENTLY. A manifest fails validation for keys that have nothing
    to do with inheritance (a bad domain tag, a missing `name`), and reading it through
    `_read_manifest` dropped every vault it inherits from protection. An entry that is not a string
    or does not resolve is skipped, and a cycle ends the walk. A project vault with no manifest, a
    manifest that cannot be read or parsed, an `inherits` that is not a list, or an unknown key in
    a manifest declaring no `inherits` (`_unknown_may_be`) makes the chain unknowable, and that
    raises VaultError: a guard must not pass on what it cannot see.
    """
    chain: list[Path] = []
    pending = [project_vault]
    while pending:
        vault_dir = Path(os.path.realpath(os.path.expanduser(pending.pop())))
        if vault_dir in chain:
            continue
        chain.append(vault_dir)
        manifest = vault_dir / MANIFEST
        if not manifest.is_file():
            if len(chain) == 1:
                # The PROJECT vault. It was composed, so it had a manifest; without one now (moved,
                # or its volume offline) nothing says what it inherits.
                raise VaultError(f"{vault_dir} has no {MANIFEST} any more, so the vaults it "
                                 f"inherits are unknown.")
            continue
        try:
            data = tomllib.loads(_read_capped(manifest))
        except (tomllib.TOMLDecodeError, RecursionError, ValueError) as e:
            raise VaultError(f"cannot parse {manifest}, so the vaults it inherits are unknown: "
                             f"{e}") from e
        swallowed = [(k, t) for k, t in misplaced_keys(data) if k == "inherits"]
        if swallowed:
            raise VaultError(f"{misplaced_refusal(manifest, swallowed)} So the vaults it inherits "
                             f"are unknown.")
        _unknown_may_be(data, manifest, "inherits", "the vaults it inherits are unknown")
        inherits = data.get("inherits", [])
        if not isinstance(inherits, list):
            raise VaultError(f"{manifest}: 'inherits' is not a list, so the vaults it inherits "
                             f"are unknown.")
        for entry in inherits:
            if not isinstance(entry, str):
                continue
            try:
                pending.append(_resolve_inherit(entry, vault_dir))
            except (VaultError, OSError, RuntimeError, ValueError):
                continue
    return tuple(chain)


def resolve_scope(project_vault: Path) -> Scope:
    """Read a project vault's manifest, resolve its inheritance, and compose the note set to index.

    Refuses (VaultError) rather than mislead: a missing manifest, a malformed one, an inherited
    vault that does not exist, a scope that composes zero notes, one file two vaults discover, or a
    doc_id that two notes claim when neither can stand for the other (reconcile keys the index on
    doc_id — a collision would silently make one note overwrite the other). Notes with the same
    bytes are not refused: each is composed, except that of several deriving one doc_id only the
    first by path is (`Scope.duplicates`). A note over the size cap or not in UTF-8 is not refused
    here: it is listed in `Scope.unreadable`, and compose quarantines it. One the filesystem cannot
    read still refuses.
    """
    project_vault = project_vault.expanduser().resolve()
    ordered = list(resolve_vaults(project_vault))
    project_ref = next(v for v in ordered if v.path == project_vault)

    # Each note with its vault's place in first-by-path order (`_first_by_path`) and its root.
    notes: list[NoteRef] = []
    where: dict[Path, tuple[int, Path]] = {}
    for i, v in enumerate(ordered):
        for n in _discover_notes(v):
            if n.path in where:
                _refuse_one_file(n.path, n.path, where[n.path][1], v.path)
            where[n.path] = (len(ordered) - 1 - i, v.path)
            notes.append(n)

    if not notes:
        raise VaultError(
            f"composed scope over {[v.name for v in ordered]} discovered zero notes. An empty "
            "scope is refused rather than indexed into a silently-empty retrieval set."
        )

    # IDENTICAL NOTES. doc_id is computed at ingest from frontmatter, else from the file name and
    # the bytes (`doc_id_for`), and reconcile keys the index on it, so two notes with one doc_id
    # would make one silently replace the other while every A-series gate stayed green. What two
    # such notes are decides what happens:
    #
    #   * SAME TEXT, DIFFERENT NAMES: each is indexed as its own note. That is ordinary in a vault:
    #     a month of daily notes made from one template and never filled in, `Untitled.md` and
    #     `Untitled 1.md` both empty, a note and the copy a file sync left beside it. Each derives
    #     its own doc_id from its own name, and since a note with no title takes its file name as
    #     one, each is found under its own title. Nothing is withheld. A conflict copy then answers
    #     twice, so `status` lists each such group by path (`introspect.identical_report`), and
    #     the conflict stays visible without stopping the vault. Up to 0.2.x this refused the
    #     whole scope, on the reasoning that two files with the same bytes are one note twice; a
    #     vault of plain notes trips that without any sync, so it was a refusal of ordinary use.
    #   * THE SAME DERIVED doc_id FROM THE SAME BYTES (one name in two folders, or two names that
    #     slug alike, such as `Todo.md` and `todo.md`, or `a b.md` and `a-b.md`): ONE is indexed,
    #     first by path, and the others are listed in `Scope.duplicates` and named in `status`.
    #     No text is lost, because they are identical. When the indexed one is deleted, the next
    #     note in that order takes its place under the same doc_id. A slug keeps 48 characters, so
    #     two long names that differ only after them (a template's date at the end) are this case
    #     too, and the copy's title, its file name, is not indexed. Their sources' `_meta.md` must
    #     be the same bytes, or both absent: two versions of a standard can hold the same passage
    #     under one name, and leaving one out would drop it from that version, and ingest judges a
    #     passage on its `_meta.md`, so a copy must not wait on a note that fails for a file the
    #     copy does not share. A copy in an inherited vault is left out for the project's, so a
    #     search filtered to the inherited vault no longer finds that text.
    #
    # STILL A WHOLE-SCOPE FAULT, so the scope is refused: a doc_id that a note DECLARES and another
    # note also has, whether declared or derived (the owner wrote it, and the engine does not pick
    # between an owner's two claims), and one derived doc_id from two notes whose bytes differ
    # (a 32-bit collision of the fingerprint `doc_id_from` keeps). A conflict copy of a note that
    # declares its doc_id carries the declaration too, so it still refuses, naming both files.
    #
    # A NOTE WHOSE BYTES ARE NOT A NOTE is left out of these checks and listed in `unreadable`,
    # where compose quarantines it. It cannot overwrite another note at index time, because it is
    # never indexed.
    by_id: dict[str, list[tuple[NoteRef, bool, str, str | None]]] = {}
    by_text: dict[str, list[NoteRef]] = {}
    bodiless: set[str] = set()
    unreadable: dict[Path, tuple[str, str]] = {}
    for n in notes:
        try:
            raw = _read_capped(n.path)
        except VaultError as e:
            # Only a note whose BYTES are at fault is the note's own failure: over the size cap, or
            # not UTF-8. A read the filesystem refused (EIO, EACCES, a file a sync client has not
            # fetched) may be gone on the next pass, and quarantining it would remove the note's
            # last good version from the index, so it still refuses the scope, as `prune` refuses
            # to count an unreadable note as deleted. Said by the engine, not by the OS.
            cause = e.__cause__
            if isinstance(cause, OSError):
                raise
            unreadable[n.path] = (
                ("not_utf8", "not UTF-8") if isinstance(cause, UnicodeDecodeError)
                else ("over_size_cap", f"larger than the {_MAX_BYTES // (1024 * 1024)} MiB cap"))
            continue
        front, body = _parse_frontmatter(raw)  # size-checked before the bytes are hashed
        fid = front.get("doc_id", "")
        declared = bool(_DOC_ID.fullmatch(fid))
        try:
            sha = sha256_file(n.path)
        except OSError as e:
            raise VaultError(f"cannot read {n.path}: {e}") from e
        eff = fid if declared else doc_id_from(n.path.stem, sha)
        by_id.setdefault(eff, []).append((n, declared, sha, _meta_sha(n.path, where[n.path][1])))
        text = hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()
        by_text.setdefault(text, []).append(n)
        if not _has_body(body):
            bodiless.add(text)

    first = _first_by_path(where)
    roots = {path: root for path, (_, root) in where.items()}
    duplicates: list[tuple[NoteRef, NoteRef]] = []
    for eff, claims in by_id.items():
        if len(claims) < 2:
            continue
        claims.sort(key=lambda c: first(c[0]))
        _refuse_unshared_id(eff, claims, roots)
        kept = claims[0][0]
        duplicates.extend((c[0], kept) for c in claims[1:])
    left_out = {n.path for n, _ in duplicates}
    composed = tuple(n for n in notes if n.path not in left_out)

    groups: list[IdenticalGroup] = []
    for text, same in by_text.items():
        same = sorted((n for n in same if n.path not in left_out), key=first)
        if len(same) > 1:
            groups.append(IdenticalGroup(tuple(same), text not in bodiless))
    groups.sort(key=lambda g: first(g.notes[0]))
    duplicates.sort(key=lambda d: first(d[0]))

    # The project's own manifest is re-read for its declared name. `visit` parsed it already but
    # kept only `inherits`; the file is size-capped and tiny, and reading it here keeps the name
    # sourced from the manifest rather than inferred from the directory (they legitimately differ).
    manifest = _read_manifest(project_vault)
    return Scope(
        project=project_ref, vaults=tuple(ordered), notes=composed,
        name=manifest["name"], unreadable=unreadable,
        quarantine_ceiling=_declared_ceiling(manifest, project_vault / MANIFEST),
        duplicates=tuple(duplicates), identical=tuple(groups),
    )


def _has_body(body: str) -> bool:
    """Whether a note's text after its frontmatter holds anything but headings, by the reader's
    own blocks: an unfilled template of headings indexes no passage, as an empty note does not."""
    return any(b.kind is not Kind.HEADING for b in _blocks(body))


def _meta_sha(note: Path, root: Path) -> str | None:
    """The SHA-256 of the `_meta.md` a note inherits from (`source_meta_path`), or None."""
    meta = source_meta_path(note, root)
    if meta is None:
        return None
    try:
        return sha256_file(meta)
    except OSError as e:
        raise VaultError(f"cannot read {meta}: {e}") from e


def _first_by_path(where: Mapping[Path, tuple[int, Path]]):
    """The order "first by path" means, as a sort key over notes: which of two notes with one
    doc_id is indexed, and how a group of identical notes is listed.

    The vault first: the project vault's copy before any it inherits, then the inherited vaults in
    the reverse of the order compose lists them (`Scope.vaults`, roots first), so a vault comes
    before the vaults it inherits. The project is the vault the scope is for, and the one `ingest`
    and `amend` write. Then the path inside the vault, folder by folder, as strings compared by
    code point: each name in NFC first, so a name a sync tool stored decomposed sorts where it does
    composed, then as stored, which orders two names one volume holds and only NFC tells apart.
    Never the filesystem's listing order and never the locale's, so it is the same on APFS and
    ext4, and from one compose to the next while the notes are the same."""

    def key(n: NoteRef) -> tuple:
        rank, root = where[n.path]
        parts = n.path.relative_to(root).parts
        return rank, tuple(_nfc(p) for p in parts), parts

    return key


def _composed_alike(n: NoteRef) -> NoteRef:
    """What a note's source adds when compose reads it: its NoteRef, by construction, less where
    it is."""
    return replace(n, path=Path(), vault="", tier=0)


def _refuse_one_file(a: Path, b: Path, a_root: Path, b_root: Path) -> None:
    """Refuse one note found twice: by one path in two vaults (an inherited vault inside the
    project vault), or as one file under two paths (that layout under another spelling, or a hard
    link). Not a copy of a note but one note with two owners, so neither can be left out for the
    other."""
    if a == b:
        raise VaultError(
            f"{a} is discovered by two vaults, {a_root} and {b_root}: one lies inside the other. "
            f"Skip the inner folder in the outer vault's {MANIFEST} (`skip`), or move the inner "
            f"vault out.")
    if a_root == b_root:
        raise VaultError(f"{a} and {b} are one file, discovered twice: it has two names in one "
                         f"vault (a hard link). Remove one.")
    raise VaultError(
        f"{a} and {b} are one file, discovered twice, through two vaults, {a_root} and {b_root}: "
        f"one lies inside the other under another spelling, or the file is hard-linked into "
        f"both. Skip the inner folder in the outer vault's {MANIFEST} (`skip`), move the inner "
        f"vault out, or remove one link.")


def _refuse_shared_file(notes: list[NoteRef], roots: Mapping[Path, Path]) -> None:
    """Refuse when two notes claiming one doc_id are one file: on a volume that folds case, or
    through a macOS firmlink, a vault inside another is reached by a spelling `resolve` keeps
    apart, and a hard link is one file under two names. Asked by the file's identity, and only of
    notes that would otherwise be copies, so a volume whose inode numbers mean nothing (0, as some
    network and FAT mounts report) can at worst miss one, never refuse a vault for it."""
    seen: dict[tuple[int, int], NoteRef] = {}
    for n in notes:
        try:
            st = os.lstat(n.path)
        except OSError as e:
            raise VaultError(f"cannot read {n.path}: {e}") from e
        if not st.st_ino:
            continue
        other = seen.setdefault((st.st_dev, st.st_ino), n)
        if other is not n:
            _refuse_one_file(other.path, n.path, roots[other.path], roots[n.path])


def _refuse_unshared_id(eff: str, claims: list[tuple[NoteRef, bool, str, str | None]],
                        roots: Mapping[Path, Path]) -> None:
    """Refuse, naming two of the notes, when the notes claiming doc_id `eff` (first by path) are
    not copies one may stand for: any of them declares it, their bytes differ, or their sources
    compose them differently. Otherwise return, and the first is indexed for all of them."""
    (a, a_declared, a_sha, a_meta), *rest = claims
    _refuse_shared_file([c[0] for c in claims], roots)
    if a_declared or any(c[1] for c in rest):
        b, b_declared, *_ = rest[0] if a_declared else next(c for c in rest if c[1])
        how = ("both declare it" if a_declared and b_declared else
               f"{(a if a_declared else b).path} declares it, and the other derives it from its "
               f"file name and bytes")
        raise VaultError(
            f"doc_id {eff!r} is claimed by two notes: {a.path} and {b.path}; {how}. Composed "
            "vaults share one id space, and one would silently replace the other at index time. "
            "A declared doc_id is the owner's, so the engine does not choose between them. If "
            "one is a copy of the other (a file sync's conflict copy carries the declaration "
            "too), delete or merge it; otherwise give one a doc_id of its own.")
    for b, _, b_sha, b_meta in rest:
        if b_sha != a_sha:
            raise VaultError(
                f"doc_id {eff!r} is derived by two notes whose bytes differ: {a.path} and "
                f"{b.path}. Their names slug alike and the fingerprint the id keeps matches by "
                "chance, so one would silently replace the other at index time. Rename one, or "
                "declare a doc_id in one.")
        if b_meta != a_meta or _composed_alike(b) != _composed_alike(a):
            raise VaultError(
                f"doc_id {eff!r} is derived by two notes with the same bytes, {a.path} and "
                f"{b.path}, whose sources compose them differently: their `_meta.md` files are "
                "not the same bytes. Neither can stand for the other, and one would silently "
                "replace the other at index time. Rename one.")


# The marker each copy's line starts with in compose's output (`Scope.duplicates`).
DUPLICATE_MARK = "duplicate:"

def assert_composed(store, *, sources: Mapping[str, Path]) -> dict:
    """A-series composition proof: inheritance actually composed, nothing silently dropped or added.

    Runs after indexing. The core guarantee is that the indexed doc set is EXACTLY what this compose
    ingested — no more, no less — which closes the "green gates, silent loss" shape from both sides.
    `sources` is the ground truth: each doc_id this run indexes, with the note it was ingested
    from (one id each: the discovered set less the notes compose quarantined and the copies it
    left out), so the scope is not needed a second time:

      1. every ingested note is indexed. A note discovered, ingested, then absent from the index
         means reconcile dropped it. When a whole vault's notes go missing this way, the message
         names the vault — the CORE tier vanishing is the chapter-title bug reincarnate (plausible
         project-only results, core silently gone).
      2. no indexed doc that this compose did NOT ingest. A stale ingest dir left by a previous run
         (one compose could not attribute to a deleted or skipped note, and so did not prune) would keep
         answering queries; out-of-scope content in the index root would answer from sources the
         manifest never authorised. Both are `indexed − ingested`.
      3. each indexed doc was read from the note compose ingested it from. Several notes can
         derive one doc_id (`Scope.duplicates`), and reconcile keeps whichever folder holding it
         comes last, so an earlier folder of a copy compose left out holds an ingested id and
         passes 1 and 2.

    A vault that legitimately contributes zero notes (e.g. a freshly-scaffolded, empty core) is NOT
    an error — it simply adds nothing to either side of the equality, so it is neither required to
    appear nor flagged as missing. That distinguishes "empty vault" from "silently dropped tier",
    which a bare "every scope vault must have ≥1 doc" check conflated.

    Raises VaultError on any breach; returns per-vault / per-tier counts for the read-out.
    """
    # Which vault each indexed doc belongs to — so a missing/stale doc can be named with its vault.
    rows = store.db.execute(
        "SELECT doc_id, COALESCE(vault,'') AS vault, source_path FROM documents").fetchall()
    doc_vault = {r["doc_id"]: r["vault"] for r in rows}
    indexed_ids = set(doc_vault)
    ingested_doc_ids = set(sources)

    # (1) ingested ⊆ indexed. A note discovered and ingested but absent from the index means
    # reconcile dropped it; a wholly-missing vault this way is the silent-tier-drop this catches.
    lost = ingested_doc_ids - indexed_ids
    if lost:
        raise VaultError(
            f"{len(lost)} note(s) ingested but not indexed: {sorted(lost)[:5]}. reconcile dropped "
            "content between ingest and index — inheritance did not compose."
        )

    # (2) indexed ⊆ ingested. An indexed doc this compose did not produce = a stale/polluted index
    # root still answering queries. The folder of a deleted note, or of one its vault now skips, is
    # pruned before this (`prune`), so what is left is content compose could not attribute to one.
    stale = indexed_ids - ingested_doc_ids
    if stale:
        raise VaultError(
            f"{len(stale)} indexed doc(s) this compose did not ingest: "
            f"{sorted((d, doc_vault[d]) for d in stale)[:5]}. The index root holds stale or "
            "out-of-scope content that compose could not attribute to a deleted or skipped note "
            "(it prunes only those) — re-run with --clean."
        )

    # (3) each from its own note. A copy left out for the note indexed in its place derives the
    # same doc_id, so its folder, if one survived, answers under an id this compose did ingest.
    wrong = sorted(
        (r["doc_id"], r["source_path"]) for r in rows
        if Path(r["source_path"] or "").resolve() != Path(sources[r["doc_id"]]).resolve())
    if wrong:
        raise VaultError(
            f"{len(wrong)} indexed doc(s) were read from another note than the one this compose "
            f"ingested: {[(d, sources[d].as_posix(), p) for d, p in wrong][:5]}. An ingest folder "
            "of a note compose left out as a copy is still in the index root — re-run with "
            "--clean.")

    # Counted by the store, not here: this hand-rolled its own GROUP BY with a third missing-value
    # convention (`COALESCE(vault,'')`, so a NULL vault and an empty one shared a bucket). The
    # query it replaced fed only this read-out — both membership proofs above run off `doc_vault`.
    return {"by_vault": store.counts_by("vault"), "by_tier": store.counts_by("tier")}
