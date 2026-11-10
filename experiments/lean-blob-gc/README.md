# A Lean model of `substrate blobs gc --unreferenced`, checked against the real planner

**The planning logic of `gc --unreferenced` is proved safe, and the real planner agrees with the
model.** When the experiment was written, against the engine at Scripta's `fd62ccb5`, the
differential found a hole in the reference scan under the planner: a note could name a blob in valid
YAML that the scan did not read, and `gc_plan` then listed the blob for removal once it was older
than seven days (the tables below, *at `fd62ccb5`*). Ronan-Wood/Scripta#3 fixed it. It reads the
frontmatter as YAML, and adds a backstop: any checksum anywhere in a note keeps its blob.

Against this repository at `b428b99` (2026-11-09):

| run | cases | result |
|---|---|---|
| `agree` | 2,000 | no disagreement |
| `survey` | 3,000 | 0 disagreements (407 at `fd62ccb5`) |
| `unsafe block_comment` | 2,000 | no safety violation |
| `agree --mutant …`, each of five planted regressions | 1,000 to 3,000 each | each caught |
| the repro | | exits 0: the key scan finds every reference |

**Two mutants changed with the fix.** The backstop alone keeps a forgotten key from costing a blob.
So `audio-not-a-key` now forgets `audio_sha256` *and* turns the backstop off, and a fifth mutant,
`no-text-backstop`, turns only the backstop off. For the same reason the repro counts a reference
as found only when the key scan itself finds it, not the backstop.

This is a side experiment in the style of AWS Cedar: a Lean model with proofs, plus randomized
differential testing of the production code against the model. The non-required `lean` CI job
(`.github/workflows/lean.yml`) runs on a pull request that touches the engine or the model:
- it builds the model and checks its axioms;
- it runs 2,000 `agree` cases;
- it requires one planted regression to be caught;
- it runs the repro.

Apart from that workflow, it changes nothing outside `experiments/`.

## What is proved

The model is [`lean/BlobGc/Model.lean`](lean/BlobGc/Model.lean): `gcPlan : World → Result`, a pure
function from registered vaults, notes, the store, the ledger and `--retire-vault` to a refusal or
a plan. The proofs are in [`lean/BlobGc/Proofs.lean`](lean/BlobGc/Proofs.lean). None uses `sorry`,
and `lake env lean Axioms.lean` shows only Lean's standard axioms (`propext`, `Quot.sound`,
`Classical.choice`).

| theorem | says |
|---|---|
| `never_deletes_a_referenced_blob` | If there is a plan, then for every vault in a registered scope's inheritance chain, and every vault the ledger remembers that was not retired: every note in it was read whole (readable, with no malformed frontmatter claim), and none names a blob the plan removes. |
| `plan_safe` | Every blob in a plan is held and older than seven days, and no note in any vault the plan read names it. |
| `plan_reads_everything` | A plan read every vault reachable through `inherits` from a registered scope, and every remembered vault that was not retired. |
| `plan_removes_what_it_should` | The converse of safety: a plan removes every held blob that is old and that no note it read names. |
| `a_plan_exists` | A concrete world on which the planner removes a blob, so the safety theorems do not hold merely because the planner always refuses. |
| `malformed_reference_refuses`, `unreadable_note_refuses`, `missing_vault_refuses` | Any of these in any registered chain yields `.refuse .incomplete`, never a plan: a malformed frontmatter reference, an unreadable note, or a missing inherited vault (the moved core-vault blocker from the review of pre-split Scripta PR 84). |
| `empty_registry_refuses` | With nothing registered, the result is `.refuse .noVault`. |
| `tombstones_irrelevant` | True by construction, since no definition reads tombstones. That the real code also ignores them is checked by the differential, which writes tombstones. |

The chain is computed by a bounded closure that is then *checked*: it must be closed under
`inherits`, every vault must exist, every registered vault must have a manifest, and it must be
acyclic. If any check fails, the planner refuses. So no proof depends on the closure being
complete. Were it incomplete, the differential would see the model refusing where the code plans.

## What was tested, at `fd62ccb5`

[`harness/run.py`](harness/run.py) generates small worlds with Hypothesis
([`harness/world.py`](harness/world.py)): up to four vaults plus two that are named but absent,
inheritance with cycles, notes, the store, tombstones, a ledger and retirements. Each world runs
two ways:

- The real `gc_plan` runs in a temporary folder that holds its own `~/.substrate`, config,
  registry and store.
- The compiled Lean model reads it as JSON.

Refusal kinds and plans (removed, recent, vaults read, the ledger after) must match. PyYAML must
read every note's frontmatter as the abstract world says, or the harness stops with a `HarnessBug`
rather than reporting a finding. Runs are derandomized, so they are reproducible. Every check is an
explicit `raise`, so they hold under `python -O`.

| run | cases | result |
|---|---|---|
| `agree`: every spelling the scan's docstring says it reads | 10,000 | **no disagreement**: 1,885 plans that remove something, 3,664 that remove nothing, 4,451 refusals (2,827 incomplete, 1,548 unregistered, 76 no vault) |
| `agree --mutant …`: four planted regressions (lenient chain, no absent-vault guard, 6-day guard, `audio_sha256` not a key) | 3,000 each | each caught, with the disagreement each guard predicts |
| `survey`: those spellings plus eight hostile but valid YAML ones | 3,000 | 407 disagreements, 33 of them a code plan removing a blob a note names. In each, rewriting every hostile spelling to a faithful one makes the disagreement go away (0 remain), so each is caused by its spelling |
| `unsafe <spelling>`: search for a code plan that removes a blob named by a note in a vault the headline theorem protects (taken from the world, not from the code's scan) | 1,500 each | found for the five spellings in rows 1–3 below; none for `nextline`, `trailing_comment` or `folded` |
| `agree nextline` | 1,500 | row 5: an uppercase checksum on the line after the key is not flagged |

The faithful spellings are:
- a scalar: bare, `'`-quoted or `"`-quoted;
- a flow list, unwrapped or wrapped across lines;
- a block list, indented or flush;
- a quoted key, a BOM, or CRLF line endings;
- Obsidian `tags`/`aliases` properties before or after the fields;
- in the body: a key line, a flow list, a block list or an inline `{key: …}`.

## Disagreements found, at `fd62ccb5`, before the fix

Every disagreement was a code bug. In each one PyYAML reads the frontmatter the way the model does,
so none is a model bug.

| # | spelling (valid YAML) | what the code does | verdict |
|---|---|---|---|
| 1 | a blank line after `source_sha256:`, or between two `- item`s | every item after the blank line is dropped, and the blob is **planned for removal** | **code bug, safety** |
| 2 | a `# comment` line after the key, or between two items | the same | **code bug, safety** |
| 3 | a `# comment` line between the key and a next-line value | the value is dropped, and the blob is **planned for removal** | **code bug, safety** |
| 4 | a whole frontmatter written as one flow mapping, `{title: t, audio_sha256: [a, b]}` | only `a` is kept alive and nothing is flagged. Checked by hand; the generator does not produce this | **code bug, safety** |
| 5 | a non-checksum value on the line after the key (`source_sha256:` then `  TODO`) | not flagged malformed, so the code plans where the spec says refuse. `_INLINE` still keeps a checksum there alive. Junk under a commented list is also missed, but that list is case 2 | code bug, refusal gap |
| 6 | a trailing comment, `source_sha256: <c>  # pdf` | flagged malformed, so the code refuses | code over-refuses (safe direction) |
| 7 | a folded scalar, `source_sha256: >-` then the checksum | flagged malformed, and the code refuses | code over-refuses (safe direction) |

**Cause of 1–3:** for a key whose value is empty on its own line, `_text_refs` reads the `- item`
lines after it and stops at the first line that is not one, so any non-item line ends the list.
`_INLINE`, the backstop, crosses the newline, but not a `#`, and not the `-` of an item.

**Cause of 4:** the same backstop reads only the first checksum after `[`.

Obsidian's Properties editor writes neither comments nor blank lines, but a hand edit or another
tool can. The scan exists so that a note's spelling can never make its original collectable.

The minimal shrunk world for case 2 has one vault, one note, and one blob older than seven days:

```yaml
---
title: t
source_sha256:
  # the originals
  - e769af2e03f5226ab4af05db1842e4ea0d40468fd5a5d6875aaf35529cc8809d
---
```

Three harness bugs were found and fixed along the way, and none is a finding:
- an oracle expectation for empty values;
- attribution that counted vaults the scan had not read;
- a `no-ledger` mutant that was caught by the harness's own ledger readback rather than by the
  guard it disabled.

## Where the model is simpler than the code

| the model | the code | why the gap is acceptable |
|---|---|---|
| A note is a list of fields `(key, front/body, tokens)`. A token is `ok`, `upper`, `padded` or `junk`, and every non-junk token is a reference. | A line-based scan over each decoding of the bytes. It is deliberately looser than the spec: body lines, uppercase hex and trailing junk all count. | The proofs are about planning, not parsing. Parsing is what the differential checks against PyYAML, and it is where the bug was. Proving it would mean modelling YAML. |
| `readable : Bool` | a dangling link, an iCloud stub, EPERM, a FIFO, an oversize note, an unreadable folder | Each makes the scan incomplete in the same way. The harness generates the first two. |
| vault ids and a checked closure | realpaths and the DFS in `vault.resolve_vaults`; relative `inherits`; manifest validation; registry, ledger and store-location errors | The first pair agreed over 10,000 cases, including missing vaults, cycles and diamonds. Everything else is a refusal or an exception raised before any plan exists, which is the safe direction. |
| the planner only | `gc_apply`: a re-plan, the token, and an age re-check under the exclusive lock | By inspection, apply removes a subset of a fresh plan. That is not proved, and concurrency is not modelled. |

## Running it

The same commands the `lean` job runs, from the repository root. First the model and its axioms:

```bash
(cd experiments/lean-blob-gc/lean && lake build) && python3 experiments/lean-blob-gc/lean/check_axioms.py
```

Then the environment CI uses: the locked `test` group, which holds Hypothesis, and PyYAML.

```bash
uv venv --python 3.14 && uv export --frozen --only-group test --no-emit-project -o /tmp/test-group.txt && uv pip install --require-hashes -r /tmp/test-group.txt && uv pip install pyyaml
```

Then the harness and the repro:

```bash
uv run --no-sync python experiments/lean-blob-gc/harness/run.py agree --cases 10000 && uv run --no-sync python experiments/lean-blob-gc/harness/run.py survey --cases 3000 && uv run --no-sync python experiments/lean-blob-gc/harness/run.py unsafe block_comment
```

```bash
uv run --no-sync python experiments/lean-blob-gc/harness/repro_hidden_reference.py
```

The repro's exit codes:
- **1:** a spelling's reference is missed by the key scan, so the plan removes the blob or keeps it
  only by the backstop;
- **0:** the key scan finds every reference;
- **2:** the repro itself is broken: an import or a case crashed, or one of its two controls
  misbehaved.

**Where things are written:**
- `run.py`'s per-case folders go under `BLOBGC_TMP`, default `$TMPDIR`, and each is removed when its
  case ends.
- The repro writes under a temporary folder of its own, which it removes at the end.
- Nothing is written into the engine's own tree.
