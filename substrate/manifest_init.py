"""`substrate init`: the one file a folder of notes needs before `compose` will index it.

`init` writes a `.substrate.toml` holding `name` and nothing else, and writes nothing more: no
index, no registry row, nothing under the engine's folder. Registering the scope is compose's job,
done when its first compose succeeds. `plan` decides everything and `create` writes; every refusal
is an InitError raised before anything is written.

It writes into the operator's vault, so every way it could clobber, misplace or break a file is
refused:

- **Never overwrite, never follow.** The manifest is created with `O_CREAT | O_EXCL | O_NOFOLLOW`
  relative to a descriptor of the folder, so an existing entry of that name refuses whatever it is
  (a file, a symlink, a dangling symlink, a folder), including one that appears after the checks.
  The descriptor must be the folder `plan` checked, or nothing is written.
- **No folder is created.** The vault must be an existing folder. A symlink to one is followed, as
  compose follows it, and the manifest is written in the folder it names.
- **Not inside another vault, not around one.** A folder with a manifest above it or below it would
  give two scopes the same notes, and one inheriting the other cannot compose at all (each nested
  note is one file both vaults discover, which `vault.resolve_scope` refuses). Written by hand
  with `skip`, such a layout works; `init` does not guess that it is meant. A folder below that cannot be read refuses too: a nested
  manifest there would go unseen.
- **Not around an index.** Compose refuses an index root with a manifest in it or above it
  (`cli._refuse_index_root`), so a manifest at or above `~/.substrate/index`, or a registered ingest
  tree, would make every compose into it refuse.

The default name is the folder name made into the one shape every consumer of a scope name takes
as it is (`default_name`), and a folder name that cannot be made into one is refused rather than
replaced by an invented name.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from substrate import scopes
from substrate.blobs import _write_all
from substrate.vault import DEFAULT_SKIP, MANIFEST

# The shape a derived name has: lowercase ASCII letters, digits, `-` and `_`, starting and ending
# with a letter or digit, at most MAX_NAME characters. It is the narrowest shape a scope name meets
# anywhere: the agent snapshot's (`agent._SCOPE_NAME`) without the dot, so `scopes.index_folder_name`
# keeps it as it is and an index is named after the scope rather than a hash of it. Nothing to quote
# in a shell, in TOML or in a `--scopes` list.
MAX_NAME = 128
_DERIVED = re.compile(rf"[a-z0-9](?:[a-z0-9_-]{{0,{MAX_NAME - 2}}}[a-z0-9])?")


class InitError(Exception):
    """A refusal. Nothing was written."""


@dataclass(frozen=True)
class Plan:
    vault: Path        # resolved
    name: str
    source: str        # where the name came from, as the read-out says it
    notes: int         # the `.md` files compose would read under the default skip list
    checked: os.stat_result


def default_name(folder: str) -> str | None:
    """The scope name a folder called `folder` gets, or None when it cannot be made into one.

    Case-folded, then decomposed (NFKD) with its combining marks dropped, so `Café`, stored composed
    or decomposed, is `cafe`, `Straße` is `strasse` and full-width letters are ASCII. Each run of
    anything but an ASCII letter, a digit or `_` becomes one `-`, and `-` and `_` at either end go.
    None when nothing is left (a name in a script with no ASCII form) or more than MAX_NAME
    characters are: the caller asks for `--name`."""
    folded = unicodedata.normalize("NFKD", folder.casefold())
    kept = "".join(c for c in folded if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9_]+", "-", kept).strip("-_")
    return name if _DERIVED.fullmatch(name) else None


def name_fault(name: str) -> str | None:
    """Why `name` cannot be a scope's, or None. The rules compose applies (`vault._read_manifest`,
    `scopes.check_name`), and one more: every character prints, because the name is printed in
    every read-out and a control character there would act on the terminal."""
    if not name.isprintable():
        return "holds a character that does not print"
    try:
        scopes.check_name(name)
    except scopes.ScopeError:
        return "is empty, holds a '/', or has white space at either end, which no scope name may"
    return None


def manifest_text(name: str) -> str:
    """The manifest `init` writes: `name`, as a TOML basic string, and nothing else."""
    return f"name = {scopes._quote(name)}\n"


def what_is_there(path: Path) -> str | None:
    """What occupies `path`, not following a symlink, or None when nothing does. Raises InitError
    when that cannot be known."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise InitError(f"cannot read {path}: {e.strerror}. Nothing was written.") from e
    if stat.S_ISLNK(st.st_mode):
        return "a symlink" if os.path.exists(path) else "a symlink to nothing"
    if stat.S_ISDIR(st.st_mode):
        return "a folder"
    return "a file"


def _occupied(path: Path, there: str) -> str:
    return f"{path} already exists ({there}). init never replaces a manifest; nothing was written."


def _walk(vault: Path) -> int:
    """The notes compose would read under `vault`, refusing a manifest below it, or a folder below
    it that cannot be read. One walk; symlinked folders are not entered, as compose's `rglob`
    enters none."""
    notes = 0

    def unreadable(e: OSError) -> None:
        raise InitError(f"cannot read {e.filename}: {e.strerror}, so a vault inside {vault} could "
                        f"go unseen. Nothing was written.")

    for folder, dirs, files in os.walk(vault, onerror=unreadable):
        here = Path(folder)
        # Folded: a volume that ignores case reads `.SUBSTRATE.toml` as the manifest, as compose's
        # `is_file()` does. Elsewhere this refuses a little more than it must.
        found = next((n for n in (*files, *dirs) if n.casefold() == MANIFEST), None)
        if here != vault and found is not None:
            raise InitError(_nested(here / found, "inside", vault))
        dirs.sort()
        rel = here.relative_to(vault).parts
        notes += sum(1 for f in files if f.endswith(".md") and not DEFAULT_SKIP.skips((*rel, f)))
    return notes


def _nested(other: Path, where: str, vault: Path) -> str:
    return (f"{other} is the manifest of a vault {where} {vault}. A vault inside another gives two "
            f"scopes the same notes, and one that inherits the other cannot compose. Nothing was "
            f"written; if the layout is meant, write the manifest by hand and `skip` the nested "
            f"folder (docs/configuration.md).")


def plan(given: Path, name: str | None, entries: dict[str, scopes.ScopeEntry]) -> Plan:
    """Everything `init` decides, for the folder `given` and `--name` (None for the default),
    against the registry's `entries`. Raises InitError. Cheap checks first, the tree walk last."""
    vault = Path(os.path.realpath(given))
    try:
        checked = os.stat(vault)
    except FileNotFoundError as e:
        raise InitError(f"{given} does not exist. init writes into an existing folder of notes and "
                        f"creates no folder.") from e
    except OSError as e:
        raise InitError(f"cannot read {given}: {e.strerror}.") from e
    if not stat.S_ISDIR(checked.st_mode):
        raise InitError(f"{given} is not a folder.")
    there = what_is_there(vault / MANIFEST)
    if there is not None:
        raise InitError(_occupied(vault / MANIFEST, there))
    for parent in vault.parents:
        if what_is_there(parent / MANIFEST) is not None:
            raise InitError(_nested(parent / MANIFEST, "above", vault))
    for index in (scopes.index_home(), *(e.index_root for e in entries.values() if e.index_root)):
        if scopes.nested(vault, index):
            raise InitError(
                f"{vault} is, holds or is inside {index}, where the engine keeps an index, and "
                f"compose refuses an index with a manifest in it or above it. Nothing was written.")

    held = sorted(n for n, e in entries.items() if scopes._same_path(e.vault, vault))
    if name is not None:
        fault, source = name_fault(name), "from --name"
        if fault is not None:
            raise InitError(f"--name {name!r} {fault}. Nothing was written.")
    elif len(held) > 1:
        raise InitError(f"the registry holds this vault under {len(held)} names, "
                        f"{', '.join(map(repr, held))}, so none is its name. Pass --name NAME. "
                        f"Nothing was written.")
    elif held:
        name, source = held[0], "the name the registry has this vault under"
        if name_fault(name) is not None:
            raise InitError(f"this vault is registered as {name!r}, which init will not write. "
                            f"Pass --name NAME. Nothing was written.")
    else:
        name, source = default_name(vault.name), "from the folder name"
        if name is None:
            raise InitError(
                f"the folder name {vault.name!r} has no letter or digit in ASCII once its accents "
                f"are dropped, or is longer than {MAX_NAME} characters, so it makes no scope "
                f"name. Pass --name NAME. Nothing was written.")
    owner = entries.get(name)
    if owner is not None and not scopes._same_path(owner.vault, vault):
        raise InitError(f"scope {name!r} is already registered to {owner.vault}, so compose would "
                        f"refuse this vault under it. Pass another --name. Nothing was written.")
    if not held:
        # Compose's own check (`cli._index_location`), made only where compose would make it: a
        # registered vault keeps the index it has, and never asks for a new place.
        try:
            db, root = scopes.default_index(name, taken=[n for n in entries if n != name])
        except scopes.ScopeError as e:
            raise InitError(f"{e}. Nothing was written.") from e
        other = scopes._foreign_owner_in(entries, vault=vault, db=db, index_root=root)
        if other is not None:
            raise InitError(f"scope {other.name!r} keeps its index at {db} or {root}, where compose "
                            f"would put {name!r}'s. Pass another --name. Nothing was written.")
    return Plan(vault=vault, name=name, source=source, notes=_walk(vault), checked=checked)


def create(p: Plan) -> Path:
    """Create the planned manifest. Raises InitError, having written nothing.

    The folder is opened without following a link and must be the one `plan` checked, and the file
    is created relative to it. A write that fails part way removes what it created, and only that:
    the entry is removed only while it is still the file this call made. The mode is 0600, as the
    write path gives a note (`notes._create`), under the umask every command runs with."""
    path = p.vault / MANIFEST
    try:
        dfd = os.open(p.vault, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise InitError(f"cannot open {p.vault}: {e.strerror}. Nothing was written.") from e
    try:
        if not os.path.samestat(os.fstat(dfd), p.checked):
            raise InitError(f"{p.vault} changed while init was checking it. Nothing was written.")
        try:
            fd = os.open(MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        except FileExistsError as e:
            raise InitError(_occupied(path, what_is_there(path) or "gone again")) from e
        except OSError as e:
            raise InitError(f"cannot create {path}: {e.strerror}. Nothing was written.") from e
        try:
            _write_all(fd, manifest_text(p.name).encode("utf-8"))
        except OSError as e:
            left = "a partial manifest may be left there; delete it"
            try:
                there = os.stat(MANIFEST, dir_fd=dfd, follow_symlinks=False)
                if os.path.samestat(there, os.fstat(fd)):
                    os.unlink(MANIFEST, dir_fd=dfd)
                    left = "what was written is removed"
            except OSError:
                pass
            raise InitError(f"failed writing {path}: {e.strerror}; {left}.") from e
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return path
