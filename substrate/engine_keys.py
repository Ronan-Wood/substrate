"""Which frontmatter property each spine field is read from, vault by vault (`[engine_keys]`).

Many vaults use `status`, `doc_type`, `confidence` or `class` for values of their own (`status:
done`, `class: Math 101`), and the engine refuses a value outside its vocabulary under those keys,
so such a vault quarantines those notes and, past the ceiling, refuses. A vault's manifest can name
another property for the engine to read a field from:

    [engine_keys]
    status = "substrate_status"
    class = "substrate_class"

Each field is renamed on its own; nothing is renamed by default, and a vault that renames nothing
is read as it always was. A rename holds for the notes of the vault whose manifest declares it, and
no other: a vault it inherits is read by its own manifest, so a shared vault written for the engine
keeps its `status: archived` notes archived whatever a project vault renames. A source's `_meta.md`
is the engine's own format, and is always read under the engine's names.

Under its new name the field is judged exactly as before: a value outside the vocabulary is
quarantined, and no value is mapped or defaulted. The vault's own property of the old name is then
one the engine does not judge, with one exception, THE GUARD: when it holds a value that would
withhold the note, or freeze it, under the engine's own name (`GUARDED`), the note is quarantined.
A note written for the engine before the rename is never indexed as a live, editable note because
of it. The value is compared as the engine would have read it under that name, and the name as the
engine compares a key (`reader.canonical_forms`), so `Class: conversation` is held too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from substrate import classes, spine
from substrate.markdown import reader

# The manifest table.
TABLE = "engine_keys"

# The fields a vault may rename, and the names each is read from when it is not renamed. `class`
# has two, read `document_class` first.
FIELDS: tuple[str, ...] = ("status", "doc_type", "confidence", "class")
DEFAULT_NAMES: dict[str, tuple[str, ...]] = {
    "status": ("status",), "doc_type": ("doc_type",), "confidence": ("confidence",),
    "class": ("document_class", "class"),
}

# THE GUARD'S VOCABULARY, from the engine's own sets, so a value added to one is guarded the day it
# is added: a status left out of default retrieval, a class withheld from it, and a class whose
# notes `amend` never edits. No doc_type or confidence withholds or freezes a note, so neither has
# a guarded value, and renaming either needs no guard.
GUARDED: dict[str, frozenset[str]] = {
    "status": spine.EXCLUDED_STATUSES,
    "doc_type": frozenset(),
    "confidence": frozenset(),
    "class": classes.EXCLUDED_CLASSES | frozenset(
        name for name, policy in classes.POLICIES.items() if policy.frozen),
}

# Each name a field is read from by default, to the field (`document_class` and `class` to `class`).
FIELD_OF: dict[str, str] = {n: f for f, names in DEFAULT_NAMES.items() for n in names}

# Where a person meets the setting: the docs section, and the lines to add for `fields`.
DOCS = 'docs/configuration.md, "A vault that already uses status or class"'


def example_lines(fields) -> list[str]:
    """The `[engine_keys]` lines that rename `fields`, each to `substrate_<field>`."""
    return [f"[{TABLE}]", *(f'{f} = "substrate_{f}"' for f in fields)]


# The quarantine kind of a note the guard holds (`quarantine.KINDS`).
GUARD_KIND = "withheld_value_under_renamed_key"

# What a value outside the vocabulary is, per field: what `spine` and `classes` refuse as
# `outside_vocabulary`. `unjudged` and `unclassified` are the engine's own values, refused as
# declared absence markers, so not foreign.
_KNOWN: dict[str, frozenset[str]] = {
    "status": spine.STATUSES,
    "doc_type": spine.DOC_TYPES,
    "confidence": spine.STORED_CONFIDENCES,
    "class": frozenset(classes.POLICIES),
}


@dataclass(frozen=True)
class KeyMap:
    """One vault's renames: (field, property name) pairs in `FIELDS` order. Empty is the default
    reading. Built by `vault._declared_keys`, which has checked every name."""

    renamed: tuple[tuple[str, str], ...] = ()

    def name_of(self, field: str) -> str | None:
        return next((n for f, n in self.renamed if f == field), None)

    def reads(self, field: str) -> tuple[str, ...]:
        """The names `field` is read from, in order: its new name, else its default names."""
        name = self.name_of(field)
        return (name,) if name is not None else DEFAULT_NAMES[field]

    def guarded(self) -> tuple[tuple[str, tuple[str, ...], frozenset[str]], ...]:
        """(field, its default names, the values guarded under them), for each renamed field with
        any guarded value."""
        return tuple((f, DEFAULT_NAMES[f], GUARDED[f]) for f, _ in self.renamed if GUARDED[f])

    @property
    def _unjudged(self) -> frozenset[str]:
        """The default names of renamed fields the guard does not read: ordinary properties."""
        return frozenset(n for f, _ in self.renamed if not GUARDED[f] for n in DEFAULT_NAMES[f])

    @property
    def read_keys(self) -> frozenset[str]:
        """The keys a strict read refuses an unreadable value under: the reader's, without a
        renamed field's old names unless the guard reads them, with the new names."""
        return (reader.READ_KEYS - self._unjudged) | {n for _, n in self.renamed}

    @property
    def spine_keys(self) -> frozenset[str]:
        """The keys whose value decides a note's spine, class or guard (`reader.opening_block`)."""
        return (reader._SPINE_VALUES - self._unjudged) | {n for _, n in self.renamed}

    @property
    def spelling(self) -> dict[str, tuple[str, str]]:
        """Each canonical form of a key the engine reads under its exact spelling only, to (that
        key, the engine key a refusal names): `spine.EXACT_SPELLING`'s keys, a renamed field's old
        names replaced by its new one. A renamed field is in the table when its old names were."""
        names = {k: k for k in spine.EXACT_SPELLING}
        for field, name in self.renamed:
            old = DEFAULT_NAMES[field]
            if any(n in names for n in old):
                for n in old:
                    names.pop(n, None)
                names[name] = field
        return {f: (k, key) for k, key in names.items() for f in reader.canonical_forms(k)}

    @property
    def signature(self) -> str:
        """The renames as one string, empty for none: what the index records a vault was composed
        under, and what a quarantined note's fingerprint covers."""
        return json.dumps(dict(self.renamed), sort_keys=True) if self.renamed else ""


DEFAULT = KeyMap()


def fixable(front: dict[str, str], keys: KeyMap, doc_class: str | None) -> list[str]:
    """The fields a rename would let this frontmatter compose with: those `foreign_fields` names,
    less one the vault renamed already (a value outside the vocabulary under the new name is a
    typo) and less `class` where the note's source's `_meta.md` supplies it (`doc_class`), which
    compose reads instead under the engine's names. A `_meta.md` status, doc_type or confidence
    fills only a field the note leaves empty, so the note's own value is what is judged."""
    return [f for f in foreign_fields(front, keys)
            if keys.name_of(f) is None and not (f == "class" and doc_class)]


def foreign_fields(front: dict[str, str], keys: KeyMap = DEFAULT) -> list[str]:
    """The fields whose value in this frontmatter, read as `keys` reads it, is outside the engine's
    vocabulary (the values compose quarantines as `outside_vocabulary`), in `FIELDS` order."""
    out = []
    for field in FIELDS:
        value = next((front[n] for n in keys.reads(field) if front.get(n)), None)
        if value is not None and value not in _KNOWN[field]:
            out.append(field)
    return out
