# A Lean model of compose's quarantine, checked against the real compose

**The rule that decides which notes a compose indexes is proved, and the real compose agrees with
the model.** A note that fails on its own is quarantined: left out of the index and named with its
reason. The scope is refused for a fault that is not one note's own, when more notes fail than the
ceiling allows, or when every note failed and none is left to index. Before any of that, of
several notes that derive one `doc_id` from the same bytes, only the first by path is composed;
the others are copies, left out and named in `status` (#71). The model proves that a quarantined
note is never indexed, that a copy is neither indexed nor quarantined, that exactly one note
answers for each `doc_id`, and that nothing else is withheld. The differential runs the real
`substrate compose` on generated vaults and finds no disagreement.

Against this repository's `feat/rename-spine-keys` on `8e95ed8` (2026-11-20), on macOS, with
copies and renamed spine keys generated together (22 theorems, standard axioms only):

| run | cases | result |
|---|---|---|
| `agree` | 2,000 | no disagreement: 408 index every note, 382 quarantine some, 875 refuse for a scope fault, 189 refuse with nothing left to index, 146 refuse over the ceiling. Of all of them, 238 leave out copies and 245 index a copy under a name of its own; in 932 the project vault renames spine keys (`[engine_keys]`), 396 of them only after an earlier compose, 155 hold a note the guard quarantines, and 145 hold copies two vaults read apart |
| `agree --mutant keep-stale` | 500 | caught: a quarantined note's earlier version stays indexed |
| `agree --mutant ceiling-ge` | 500 | caught: the scope is refused at the ceiling rather than above it |
| `agree --mutant no-default` | 500 | caught: a manifest with no ceiling gets none |
| `agree --mutant faults-ignored` | 500 | caught: `decide` drops a scope fault found while ingesting |
| `agree --mutant allow-empty` | 500 | caught: a scope whose every note failed is indexed empty |
| `agree --mutant last-copy` | 500 | caught: of two notes with one `doc_id`, the last by path is indexed rather than the first |
| `agree --mutant no-guard` | 500 | caught: a note whose renamed field's old name still says `archived` or `conversation` is indexed live |
| `agree --mutant read-alike` | 500 | caught: a copy is left out for a note its own vault would read differently |

The non-required `lean` CI job (`.github/workflows/lean.yml`) builds the model, checks its axioms,
runs 2,000 `agree` cases and requires each of the eight planted regressions to be caught.

## What is proved

The model is [`lean/Quarantine/Model.lean`](lean/Quarantine/Model.lean): `verdict : World →
Result`, from each note's outcome, whether the scope has a fault that is not one note's own, and
the declared ceiling, to a refusal or the notes indexed and quarantined. It is
`substrate.quarantine.decide` with `ceiling_for` folded in. The proofs are in
[`lean/Quarantine/Proofs.lean`](lean/Quarantine/Proofs.lean). None uses `sorry`, and
`check_axioms.py` shows only Lean's standard axioms.

| theorem | says |
|---|---|
| `a_failed_note_is_never_indexed` | When the scope is indexed, no note that failed is among the notes indexed. |
| `every_passing_note_is_indexed` | When the scope is indexed, every note that passed is. |
| `quarantined_is_what_failed` | What is quarantined is exactly what failed. |
| `the_notes_are_partitioned` | Every note is indexed or quarantined, and none is both. |
| `refuses_exactly_when` | The scope is refused if and only if it has a scope fault, or there is a ceiling and more notes failed than it allows, or it has notes and none passed. |
| `nothing_left_refuses`, `an_index_is_never_empty` | A scope with notes of which none passed refuses whatever the ceiling, `"none"` included, so an index of a scope with notes is never empty. |
| `a_scope_fault_refuses` | A scope fault refuses whatever the notes. |
| `a_ceiling_of_zero_refuses_any_failure` | `quarantine_ceiling = 0` is the all-or-nothing compose of before. |
| `no_ceiling_never_refuses_for_the_count` | `quarantine_ceiling = "none"` indexes every note that passed, however many failed, so long as one passed. |
| `the_default_ceiling` | With no declaration the ceiling is at least 10, and never more than 1% of the scope once it is above 10. |
| `a_quarantine_exists`, `the_default_refuses_eleven`, `all_failing_refuses_with_no_ceiling` | Concrete worlds on which the model quarantines a note, and refuses, so neither kind of theorem holds vacuously. |

Three theorems assume the notes are numbered without repeats. Compose keys each note by its path,
which names one note in a scope.

**Copies** are in [`lean/Quarantine/Copies.lean`](lean/Quarantine/Copies.lean). `outcome` runs
`verdict` over `composed`: the notes discovery finds, less the copies. So every theorem above is
about the notes composed, and `every_passing_note_is_indexed` says "every note composed that
passed". A copy passes and is not indexed, so for every note discovery finds the statement is the
narrower one below, and it is said, not left to the old name:

| theorem | says |
|---|---|
| `every_passing_note_is_indexed_or_a_copy` | When the scope is indexed, every note discovery found that passed is indexed, or is a copy and a note composed in its place, indexed or quarantined, derives the same `doc_id`. |
| `every_passing_note_is_indexed_or_its_twin_is` | The same, with that note indexed, under an assumption: a copy fails exactly when the note composed in its place does (`twins`). The code makes it likely, not certain (below). |
| `a_copy_is_never_indexed_or_quarantined` | A copy is in neither list. |
| `a_copy_has_its_twin_composed` | A copy has a note with its `doc_id`, before it by path, that is composed. |
| `one_note_per_doc_id` | No two composed notes derive one `doc_id`. |
| `without_shared_ids_no_copy` | When every note has a `doc_id` of its own (identical notes under their own names among them), nothing is left out and every note is decided as before. |
| `a_copy_is_left_out`, `a_copy_of_a_failure_is_not_quarantined` | Concrete scopes on which a copy is left out beside an indexed note and beside a quarantined one. |

Two of them assume the found notes are numbered, and ranked by path, without repeats: a path names
one note, and first-by-path order is total on paths.

## What is tested

[`harness/run.py`](harness/run.py) generates small cases with Hypothesis: up to 24 notes across a
project vault and an inherited one, each passing or failing in one of nine ways compose
quarantines (a status, doc_type or class outside the vocabulary, frontmatter that is not YAML, an
engine key spelled another way, `superseded` with no link, a class policy refusal, a note that is
not UTF-8), or, in half the cases, a project vault that renames `status`, `doc_type` and `class`
in `[engine_keys]`, with two more kinds whose outcome is the vault's (values of the vault's own,
and an old key that still says `archived` and `conversation`); up to six byte copies of those
notes, each under a name of its own or under its note's name in another folder (`a/`, before
`notes/` by path, or `zz/`, after it) or in the other vault; a declared ceiling (none, a count
from 0 to 12, or `"none"`); one of seven scope faults (a `doc_id` two notes declare, a conflict
copy of a note that declares its `doc_id`, one name in two folders whose bytes differ and whose
`doc_id` fingerprint matches anyway, a missing inherited vault, a manifest that does not parse, a
ceiling that is not one, a link where a composed passing note's ingest folder goes); and, for some
notes, an earlier compose that indexed them in a good version. All but the link are refused
before any note is read; the link reaches `decide` while compose ingests.

The real compose runs in the harness's process, against a scratch registry and engine home in a
temporary folder per case. The compiled model reads the same case as JSON. They must agree:

- the model refuses exactly when compose exits non-zero. Then the index is what the earlier
  compose left (or there is none), every ingest folder that held a version still holds one, and
  compose's report to a refresh pass holds only why it refused;
- otherwise the index holds exactly the notes the model indexes, and the quarantine table and
  compose's report both name exactly the notes it quarantines. Compose's report and `status` both
  name exactly the copies it leaves out, each with a composed note of its `doc_id` as the one
  indexed in its place. A failed note whose earlier version is still indexed is a disagreement.

Runs are derandomized, so they are reproducible. Every check is an explicit `raise`, so they hold
under `python -O`.

## Where the model is simpler than the code

- **A `doc_id` is a number, and so is first-by-path order.** The harness gives each note one per
  file name and bytes, and a note that is not UTF-8 one of its own (compose never reads it for
  one; it is quarantined). It ranks the notes itself, from their vault and path, rather than
  asking the code. That ranking is a second statement of the rule, so a code that picks another
  copy disagrees: `last-copy` is how it shows.
- **A copy is generated to fail exactly when its note does**, as `twins` assumes: the harness
  writes it with its note's kind. The code makes notes copies only when their bytes and their
  sources' `_meta.md` bytes are the same, and refuses two that differ only in `_meta.md`
  (`tests/test_identical_notes.py`). A failure that depends on the path alone can still tell two
  copies apart: a name with nothing visible titles no note, where a name that slugs alike does.
  That is not generated; the theorem without `twins` covers it, and compose and `status` then say
  that neither copy is indexed.

- **Why a note failed is not modelled.** Every per-note failure is one bit. The harness writes
  each kind the code distinguishes, so a kind the code refused the scope for would disagree.
- **A scope fault is one bit.** The code refuses most of them before any note is read
  (`vault.resolve_scope`, exit 2), and an ingest folder it cannot write safely while ingesting
  (exit 3). The link is planted only at a note that passes: ingest opens a note's folder after its
  gates, so a link at a failing note's folder is never written through, and is left for
  `assert_composed` if anything at its target reads as a document.
- **A note the filesystem cannot read is not generated.** It refuses the scope before any note is
  read, as it did before quarantine; `tests/test_compose_quarantine.py` pins it.
- **The 1% side of the default is proved, not run.** The harness's scopes are under 1,000 notes,
  where the default is 10. `tests/test_compose_quarantine.py` composes 1,111 and 1,112 notes.
- **Failures after reconcile are not modelled.** The composition assertions run once the index is
  written, and a refusal there leaves it partly rewritten. That is outside this rule, and the
  `keep-stale` mutant is how it shows: the stale version is indexed, and `assert_composed`
  refuses after the write.
- **A renamed vault is a case, not a model input.** Whether a note fails depends on its vault's
  `[engine_keys]`: in a vault that renames, a note of the vault's own values passes and one whose
  old key still marks it archived or a call fails; elsewhere, the inherited vault included, the
  reverse. The harness works that out per note and hands the model the bit, so the theorems about
  failed notes cover it unchanged. Some cases add the rename only after the earlier compose, so a
  note indexed in a good version, with the same bytes, must leave the index.
- **A22 and A18 failures are not generated.** Both quarantine their note through the same path;
  `tests/test_compose_quarantine.py` plants each.

## Running it

```sh
cd experiments/lean-quarantine/lean && lake build && python3 check_axioms.py && cd ../../..
uv run python experiments/lean-quarantine/harness/run.py agree --cases 2000
uv run python experiments/lean-quarantine/harness/run.py agree --mutant keep-stale --cases 300
uv run python experiments/lean-quarantine/harness/run.py agree --mutant last-copy --cases 300
```
