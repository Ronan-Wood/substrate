# A Lean model of the conversion quarantine rule, checked against the real rule

**The rule that decides whether a converted file is attempted, retried, quarantined or blocked is
proved, and the real `quarantine_decision` agrees with the model.** A file is withheld from an
ingest (exit 3) in exactly two ways:
- it is blocked: no attempt has been made, its record is this converter's, and no retry was asked
  for;
- it is quarantined: `RETRIES + 1` faults in a row.

Docling's own answer is final, a record is written only on quarantine and deleted only by a
conversion that went through, and the rule decides exactly the sequences a driver can hand it.

Against this repository's `feat/lean-convert-quarantine` on `77a9000` (2026-11-24, macOS; 18
theorems, standard axioms only):

| run | cases | result |
|---|---|---|
| `agree` | 2,000 | no disagreement: 433 decided (118 attempt, 124 quarantined, 6 blocked, 38 converted, 147 another answer), 1,141 rejected by the rule, 426 rejected for an outcome or record state the code does not know |
| `all --length 4` | 59,048 | no disagreement: every sequence of up to four outcomes (the code's eight and one it does not know), every record state and one it does not know, with and without the flag. 166 decided, 27,920 rejected by the rule, 30,962 for an unknown outcome or state |
| `agree --mutant …`, each of eleven planted regressions | 300 each | each caught, with the minimal disagreement below |

The non-required `lean` CI job (`.github/workflows/lean.yml`) builds the model, checks its axioms,
runs 2,000 `agree` cases and every case to four outcomes, and requires each mutant to be caught.

## A finding, fixed with the model

Before this change, at `77a9000`, the rule decided two kinds of sequence that no driver produces,
where its docstring said it raises `ValueError`. It checked the bound on faults only when the last
outcome was a fault, so `hang, hang, ok` and `hang, hang, hang, ok` were `converted`. It also never
rejected an attempt on a file a matching record blocks: `ok` with a matching record and no retry
was `converted`, and the record was deleted. `through_worker` stops at the first decision that is
not `attempt` and reads the record state once, before its loop, so it never built either sequence.
The rule exists to catch a driver that does, and it did not. It now rejects both;
`tests/test_convert_worker.py` holds the seven cases that returned a decision before, and two
mutants (`bound-after-answer`, `attempt-when-blocked`) put each back.

## What is proved

The model is [`lean/ConvertQuarantine/Model.lean`](lean/ConvertQuarantine/Model.lean):
`decideWith retries os r retry : Option Decision`, which is `quarantine_decision` with `RETRIES` as
a parameter. `none` is where the code raises. The proofs are in
[`lean/ConvertQuarantine/Proofs.lean`](lean/ConvertQuarantine/Proofs.lean), for every `retries`
unless the row says otherwise. None uses `sorry`, and `check_axioms.py` shows only Lean's standard
axioms.

| theorem | says |
|---|---|
| `withheld_only_two_ways` | A decision of `blocked` or `quarantined` comes from no attempt with a matching record and no retry, or from `retries + 1` outcomes that are all faults. |
| `blocked_iff` | Blocked if and only if no attempt has been made, the record is this converter's, and no retry was asked for. |
| `quarantined_iff` | Quarantined if and only if there are `retries + 1` outcomes, every one a fault, on a file the record does not block. |
| `an_answer_is_final` | When the last outcome is an answer, the verdict is that answer's own, and never `attempt`, `quarantined` or `blocked`. |
| `an_answer_never_quarantines` | No sequence holding an answer anywhere quarantines. |
| `converted_iff` | Converted if and only if the last outcome is `ok`. So a fault never yields a conversion. |
| `write_iff` | The record is written if and only if the file is quarantined. |
| `delete_iff` | The record is deleted if and only if a conversion went through and there was a record (matching or another converter's). |
| `retry_matters_only_for_a_matching_record` | With no record, or another converter's, the flag changes nothing at all. |
| `retry_treats_a_matching_record_as_another_s` | With the flag, a matching record is decided on every sequence as another converter's record is. |
| `without_retry_a_matching_record_is_never_attempted` | Without the flag, a matching record admits only the empty sequence, which is blocked. With the two rows above, that is everything the flag does. |
| `only_a_matching_record_blocks` | Another converter's record never blocks, and neither does no record. |
| `attempt_iff` | `attempt`, which always keeps the record, exactly when every outcome so far is a fault, there are at most `retries` of them, and the record does not block. |
| `accepts_iff` | The rule decides exactly the empty sequence, and faults followed by one more outcome, with at most `retries` faults before it, on a file the record does not block. |
| `accepts_iff_reached` | The rule decides exactly the sequences a driver can reach (`Reached`: start empty, and extend by any outcome after an `attempt`). So it is total on what a driver produces and rejects the rest. |
| `a_run_is_bounded` | No reached sequence is longer than `retries + 1`: a driver starts at most that many workers. |
| `a_first_fault_is_retried`, `a_second_fault_quarantines` | At the code's `RETRIES = 1`: one fault is retried, and a second in a row quarantines and writes the record, whichever faults they are. |

## What is tested

[`harness/run.py`](harness/run.py) imports the real `conversion_quarantine.quarantine_decision`
unchanged and compares it with the compiled model on each case. A case is a sequence of outcomes,
a record state and the flag. The two must give the same verdict and record action, or both
reject: the code raises `ValueError`, the model answers `reject`. Any other exception, or a return
that is not a `Decision`, is a disagreement.

- **`agree`** draws cases with Hypothesis, derandomized. Half the sequences have a driver's shape
  (up to three faults, then perhaps an answer), so every verdict and record action is common. The
  other half are any outcomes in any order. A sequence holds an outcome the code does not know now
  and then, and a record state does too.
- **`all --length L`** walks every case up to `L` outcomes, shortest first, so the first
  disagreement it meets is a shortest one.

The harness stops with a `HarnessBug` if the code's outcomes or `RETRIES` stop being the model's,
rather than generating a new outcome only as an unknown one. Every check is an explicit `raise`, so
the checks hold under `python -O`.

Each mutant rewrites the real rule in the harness's process: a constant, or the text of a line or
a block of `quarantine_decision`'s source, loaded in its place. Each text must occur exactly once,
so a reformatted rule stops the run with a `HarnessBug` rather than running a mutant that no
longer applies. A wrapper around the function could not reach inside its branches.

| mutant | the regression | minimal disagreement |
|---|---|---|
| `one-fault` | `RETRIES = 0`: quarantine after one fault | `hang`: attempt, code quarantined |
| `three-faults` | `RETRIES = 2`: off by one the other way | `hang, hang`: quarantined, code attempt |
| `never-quarantine` | the last fault is attempted again | `hang, hang`: quarantined, code attempt |
| `retry-an-answer` | a refusal falls through to the fault branch | `refused`: refused, code attempt |
| `refused-clears` | a refusal deletes the record | `refused` with another's record: keep, code delete |
| `converted-keeps` | a conversion keeps the record, so the file stays blocked | `ok` with another's record: delete, code keep |
| `write-on-answer` | an answer writes a record | `refused`: keep, code write |
| `other-blocks` | another converter's record blocks | `[]` with another's record: attempt, code blocked |
| `retry-ignored` | `--retry-quarantined` is ignored | `[]`, matching, retry: attempt, code blocked |
| `bound-after-answer` | the bound checked only on a fault, as at `77a9000` | `hang, hang, ok`: reject, code converted |
| `attempt-when-blocked` | an attempt on a blocked file decided, as at `77a9000` | a single outcome, matching, no retry: reject, code decides |

Each exits 1 at 300 cases, printing its disagreement.

## What is not covered

The model is the pure rule. The rule is only as good as the loop that calls it and the record it
reads, and neither is modelled:

| not modelled | what checks it instead |
|---|---|
| The driver, `through_worker` in `substrate/extract/convert.py`: that it reads the record once, asks the rule before the first attempt and after each one with every outcome so far, starts a worker only on `attempt`, stops at the first other verdict, and writes and deletes the record on the record action rather than the verdict | `test_the_driver_asks_the_rule_after_every_attempt_and_obeys_it`, `test_the_record_follows_the_decision_s_record_action` and `test_a_conversion_removes_a_record_only_when_the_decision_says_delete` in [`tests/test_convert_worker.py`](../../tests/test_convert_worker.py), against scripted workers. `Reached` is the driver as the proofs see it, and the first test pins the code to it |
| Which outcome an attempt has: what counts as a fault (`worker_client`), and when a fault or refusal becomes `changed` because the file's bytes moved | the worker and file-change tests in the same file |
| The record: `read`, `state`, `write`, `remove`, and a record the engine did not write reading as absent | the record tests in the same file |
| Two ingests of one file at once | `write` is atomic (`test_two_ingests_writing_one_record_leave_a_whole_one`); the record is read once per run (the driver test counts the reads), so a racing write is seen by the next run, not this one. Not proved |

## Where the model is simpler than the code

- **Outcomes and record states are types.** A string the code does not know cannot be written in
  the model, so its JSON reader rejects one, as the code raises for it. The harness generates them.
- **A rejection carries no message**, and the harness compares only that both reject.

## Running it

The same commands the `lean` job runs, from the repository root, in an environment with the
locked `test` group:

```sh
cd experiments/lean-convert-quarantine/lean && lake build && python3 check_axioms.py && cd ../../..
uv run --no-sync python experiments/lean-convert-quarantine/harness/run.py agree --cases 2000
uv run --no-sync python experiments/lean-convert-quarantine/harness/run.py all --length 4
uv run --no-sync python experiments/lean-convert-quarantine/harness/run.py agree --mutant converted-keeps --cases 300
```

Nothing is written outside this folder. The model builds under `lean/.lake/`, and Hypothesis keeps
its scratch under `harness/.hypothesis/`, both ignored by git; it keeps no example database
(`database=None`).
