"""Which notes a compose indexes, which it quarantines, and when it refuses the whole scope.

A note that fails on its own (frontmatter the engine cannot read, a spine value outside the
vocabulary, a class policy refusal) is QUARANTINED: left out of the index entirely, and named with
its reason in `status` and in the refresh record. The rest of the scope is indexed. Before this,
one such note refused the whole scope, so one bad note in a vault someone else maintains stopped
every refresh until it was fixed.

Three things still refuse the whole scope, so a broken index never looks complete:

  * a SCOPE FAULT, which is not one note's own: a duplicate `doc_id`, a missing inherited vault, a
    broken manifest, an ingest folder the engine cannot write safely. Most of these refuse before
    any note is read (`vault.resolve_scope`); the ones found while ingesting reach `decide`.
  * MORE QUARANTINED NOTES THAN THE CEILING. The project vault's `quarantine_ceiling` sets it;
    without one it is the larger of 10 notes and 1% of the scope's notes, rounded down.
  * NOTHING LEFT TO INDEX: every note the scope discovered failed. Whatever the ceiling says,
    `"none"` included, the scope refuses and the earlier index stays.

Over MCP a quarantined note is named by its path and a KIND of fault from `KINDS`, with the engine
key it concerns from `KEYS`, and never by text from the note (`tag`, `kind_of`). The reason with
the note's own value in it stays in the operator's terminal: compose's output, `substrate status`.

`decide` is the rule, as a pure function, so it can be modelled: `experiments/lean-quarantine/`
proves its properties and checks the real compose against the model.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

# The manifest key, and the one value that is not a count.
CEILING_KEY = "quarantine_ceiling"
NO_CEILING = "none"

# The default ceiling: the larger of DEFAULT_FLOOR notes and one in DEFAULT_SHARE of the scope.
DEFAULT_FLOOR = 10
DEFAULT_SHARE = 100

# The marker each quarantined note's line starts with in compose's output, which a refresh pass
# carries into its report.
MARK = "quarantined:"

K = TypeVar("K", bound=Hashable)

# Why a scope is refused: a fault that is not one note's own, more failures than the ceiling, or
# no note left to index.
FAULT = "fault"
CEILING = "ceiling"
NOTHING_LEFT = "nothing_left"

# What kind of fault quarantined a note: the closed vocabulary a client sees (the contract's
# `quarantine.kinds`). Each says what is wrong without quoting the note.
KINDS: frozenset[str] = frozenset({
    "not_utf8",                        # the note's bytes are not UTF-8
    "over_size_cap",                   # the note is larger than the vault's size cap
    "frontmatter_unreadable",          # a value under a key the engine reads cannot be read
    "outside_vocabulary",              # a value outside the spine's or the classes' vocabulary
    "absence_marker_declared",         # `unclassified` or `unjudged` written as a value
    "superseded_without_link",         # `status: superseded` with no `superseded_by`
    "engine_key_spelled_another_way",  # `Status:`, `doc-type:`, `"class":` (`spine.EXACT_SPELLING`)
    "opening_block_unread",            # a default over a `---` block the reader did not take whole
    "missing_required_field",          # a field the note's class requires is absent
    "source_meta_unreadable",          # a passage's `_meta.md` could not be read at ingest
    "coverage_loss",                   # A18: ingest would drop the note's content
    "per_note_assertion",              # A22: a per-note assertion failed
    "other",                           # a refusal no kind names yet
})

# The keys a kind may name: the engine's own, never one a note wrote. Any other key is shown as
# none (`kind_of`), so a key spelled in a note never reaches a client.
KEYS: frozenset[str] = frozenset({
    "doc_id", "title", "class", "document_class", "status", "doc_type", "confidence", "domains",
    "supersedes", "superseded_by", "source_sha256", "source_pages", "version", "version_date",
    "page_label_offset", "raw", "raw_sha256", "raw_location",
})


def tag(exc: BaseException, kind: str, key: str | None = None) -> BaseException:
    """`exc`, marked with the kind of fault it is and the engine key it concerns, for `kind_of`.
    Returned, so a raise site reads `raise tag(SpineError(…), "outside_vocabulary", "status")`."""
    exc.quarantine_kind = (kind, key)  # type: ignore[attr-defined]
    return exc


def kind_of(exc: BaseException) -> tuple[str, str | None]:
    """(kind, key) for a refusal: what `tag` marked, held to `KINDS` and `KEYS`, else `other`."""
    return checked(*getattr(exc, "quarantine_kind", ("other", None)))


def checked(kind: object, key: object) -> tuple[str, str | None]:
    """`kind` and `key` held to `KINDS` and `KEYS`: an unknown kind is `other`, an unknown key
    none. Applied wherever one leaves the engine, whoever wrote it, so a row edited by hand cannot
    put a note's text where a client reads a kind."""
    return (kind if kind in KINDS else "other"), (key if key in KEYS else None)


def ceiling_for(declared: int | str | None, notes: int) -> int | None:
    """The most notes a scope of `notes` notes may quarantine and still be indexed, or None for no
    limit. `declared` is the project manifest's `quarantine_ceiling`: absent (None), a count, or
    `NO_CEILING`. `notes` counts every note the scope composes, across all its vaults, failed or
    not; skipped files are not notes. 1% rounds down, so the default never allows more than 1%
    once the scope is larger than 1,000 notes. `vault._declared_ceiling` has checked `declared`."""
    if declared == NO_CEILING:
        return None
    if declared is None:
        return max(DEFAULT_FLOOR, notes // DEFAULT_SHARE)
    return declared


@dataclass(frozen=True)
class Decision(Generic[K]):
    """What a compose does with its notes. `refusal` is why the whole scope is refused (`FAULT`,
    `CEILING` or `NOTHING_LEFT`), or None. When it refuses, `indexed` is empty; `quarantined` lists every note that
    failed either way, so a refusal can name them."""

    refusal: str | None
    indexed: tuple[K, ...]
    quarantined: tuple[tuple[K, str], ...]


def decide(outcomes: Mapping[K, str | None], scope_faults: Sequence[str],
           ceiling: int | None) -> Decision[K]:
    """Index every note that passed, quarantine every note that failed, or refuse the scope.

    `outcomes` maps each note to None when it passed, or to why it failed. `scope_faults` are the
    faults that are not one note's own. `ceiling` is `ceiling_for`'s answer.

    The scope is refused exactly when there is a scope fault, or there is a ceiling and more notes
    failed than it allows, or every note failed. Otherwise every note that passed is indexed and no
    note that failed is.
    """
    failed = tuple((k, why) for k, why in outcomes.items() if why is not None)
    if scope_faults:
        return Decision(FAULT, (), failed)
    if ceiling is not None and len(failed) > ceiling:
        return Decision(CEILING, (), failed)
    if failed and len(failed) == len(outcomes):
        return Decision(NOTHING_LEFT, (), failed)
    return Decision(None, tuple(k for k, why in outcomes.items() if why is None), failed)


# The longest a reason is kept, stored or shown. A reason may quote the note (the value it
# declared, the words a coverage check missed), and it reaches every client of `status`, so a
# note's own text never fills a reply or reads as the engine's account at any length.
MAX_REASON_CHARS = 300


def shown(text: str, limit: int | None = None) -> str:
    """`text` as one printable line: every character that does not print (a line break, a control
    character, one that renders as nothing) written as its escape, so a name or a reason cannot
    break a line it is printed on or forge the next one. Cut to `limit` characters, with `…`."""
    out = "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)
    return out if limit is None or len(out) <= limit else out[:limit - 1] + "…"


def reason(text: str) -> str:
    """A failure's reason as compose keeps it: `shown`, and cut to MAX_REASON_CHARS."""
    return shown(text, MAX_REASON_CHARS)
