"""Differential testing: the real `quarantine_decision` against the Lean model of it.

    python run.py agree --cases 2000            # generated cases: the two must agree on every one
    python run.py all --length 4                # every case up to that many outcomes
    python run.py agree --mutant one-fault      # the rule broken on purpose: the harness must notice

A case is a sequence of outcomes, a record state and `--retry-quarantined`. The outcomes are the
code's own (`FAULTS`, `ANSWERS`) and, now and then, one it does not know; so is the record state.
The real `conversion_quarantine.quarantine_decision`, imported unchanged, decides each case in this
process, and the compiled model decides it too. They must agree: the same verdict and record
action, or both reject it (the code raises `ValueError`, the model answers `reject`). Any other
exception from the code is a disagreement.

`agree` runs Hypothesis, derandomized, so the same command gives the same counts, and shrinks a
disagreement to a minimal one. `all` walks every case in order of length, so the first disagreement
it meets is a shortest one. A script, not a module: it refuses to be imported. Every check is an
explicit `raise`, never an `assert`, so `python -O` cannot turn it into one that passes without
testing anything.
"""

from __future__ import annotations

import argparse
import collections
import inspect
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

if __name__ != "__main__":
    raise ImportError("harness/run.py is a script: run it, do not import it")

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LEAN_BIN = HERE.parent / "lean" / ".lake" / "build" / "bin" / "convertquarantine"
sys.path.insert(0, str(REPO))
os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] = str(HERE / ".hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from substrate.extract import conversion_quarantine as q  # noqa: E402


class HarnessBug(Exception):
    """The harness, not the code or the model, is wrong. Never caught: a finding cannot hide it."""


class Finding(Exception):
    """The code and the model disagree. Hypothesis shrinks on it."""


UNKNOWN = "bogus"
OUTCOMES = (*q.FAULTS, *q.ANSWERS)
RECORDS = (q.ABSENT, q.MATCHING, q.OTHER)
# The vocabulary the model's reader knows, kept by hand. It guards the code's side only: if the
# code adds an outcome or changes RETRIES, the run stops rather than generating the new outcome as
# UNKNOWN. Drift on the model's side shows as a disagreement.
MODEL_OUTCOMES = ("hang", "crash", "protocol", "ok", "refused", "needs_extra", "unsupported",
                  "changed")
if tuple(sorted(OUTCOMES)) != tuple(sorted(MODEL_OUTCOMES)) or q.RETRIES != 1:
    raise HarnessBug(f"the code's vocabulary {OUTCOMES} or RETRIES {q.RETRIES} is not the model's")


class Lean:
    def __init__(self) -> None:
        self.p = subprocess.Popen([str(LEAN_BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True)

    def decide(self, outcomes: tuple[str, ...], record: str, retry: bool) -> dict:
        self.p.stdin.write(json.dumps({"outcomes": list(outcomes), "record": record,
                                       "retry": retry}) + "\n")
        self.p.stdin.flush()
        out = json.loads(self.p.stdout.readline())
        if "error" in out:
            raise HarnessBug(f"the model could not read the case: {out['error']}")
        return out


def code_decide(outcomes: tuple[str, ...], record: str, retry: bool) -> dict:
    try:
        d = q.quarantine_decision(outcomes, record, retry)
    except ValueError:
        return {"reject": True}
    except Exception as e:  # noqa: BLE001 — a crash is a disagreement, reported as one
        return {"crash": f"{type(e).__name__}: {e}"}
    if type(d) is not q.Decision:
        return {"not a Decision": repr(d)}
    return {"verdict": d.verdict, "record": d.record}


def check(outcomes: tuple[str, ...], record: str, retry: bool, n: collections.Counter) -> None:
    model = LEAN.decide(outcomes, record, retry)
    code = code_decide(outcomes, record, retry)
    if model != code:
        raise Finding(f"outcomes {list(outcomes)}  record {record}  retry {retry}\n"
                      f"  model {model}\n  code  {code}")
    if "reject" in model:
        unknown = UNKNOWN in outcomes or record == UNKNOWN
        n["both reject: " + ("an unknown outcome or record" if unknown else "the rule")] += 1
    else:
        n[f"both decide: {model['verdict']}, record {model['record']}"] += 1


# ---------------------------------------------------------------- mutants

def _edit(*pairs: tuple[str, str]):
    """Rewrite `quarantine_decision`'s source and load it in place of the real one. Each old text
    must occur exactly once, so a mutant that no longer applies stops the run."""
    def apply() -> None:
        src = inspect.getsource(q.quarantine_decision)
        for old, new in pairs:
            if src.count(old) != 1:
                raise HarnessBug(f"mutant text found {src.count(old)} times: {old!r}")
            src = src.replace(old, new)
        exec(compile(src, q.__file__, "exec"), vars(q))  # noqa: S102 — the harness's own text
    return apply


def _retries(k: int):
    def apply() -> None:
        q.RETRIES = k
    return apply


BLOCKED = """    if record == MATCHING and not retry_quarantined:
        if outcomes:
            raise ValueError("an attempt on a file its record blocks")
        return Decision("blocked")
"""
BOUND = """    if len(outcomes) > RETRIES + 1:
        raise ValueError(f"{len(outcomes)} attempts, past the {RETRIES + 1} allowed")
"""
ANSWER = """        return Decision(_VERDICT[last])
"""

MUTANTS = {
    # Quarantine after one fault, and after three: RETRIES moved down and up by one.
    "one-fault": _retries(0),
    "three-faults": _retries(2),
    "never-quarantine": _edit(('return Decision("quarantined", "write")',
                               'return Decision("attempt")')),
    # A refusal falls through to the fault branch, so it is attempted again.
    "retry-an-answer": _edit(("if last in ANSWERS:", 'if last in ANSWERS and last != "refused":')),
    "refused-clears": _edit((ANSWER, '        return Decision(_VERDICT[last], "delete" if '
                                     'last == "refused" and record != ABSENT else "keep")\n')),
    "converted-keeps": _edit(('"delete" if record != ABSENT else "keep"', '"keep"')),
    "write-on-answer": _edit((ANSWER, '        return Decision(_VERDICT[last], "write")\n')),
    "other-blocks": _edit(("if record == MATCHING and not retry_quarantined:",
                           "if record != ABSENT and not retry_quarantined:")),
    "retry-ignored": _edit(("if record == MATCHING and not retry_quarantined:",
                            "if record == MATCHING:")),
    # The two rejections the fix added, each dropped back to the rule before it.
    "bound-after-answer": _edit((BOUND, ""), ("    if len(outcomes) <= RETRIES:\n",
                                              BOUND + "    if len(outcomes) <= RETRIES:\n")),
    "attempt-when-blocked": _edit((BLOCKED, '    if record == MATCHING and not retry_quarantined '
                                            'and not outcomes:\n        return Decision("blocked")\n')),
}

SETTINGS = dict(deadline=None, database=None, derandomize=True,
                suppress_health_check=list(HealthCheck), print_blob=True)

# Half the sequences have a driver's shape, up to three faults and then perhaps an answer, so
# every verdict and record action is common; three faults, or two and an answer, are past the
# bound. The other half are any outcomes in any order, now and then one the code does not know.
# Record states are the code's own, and now and then one it does not know.
shaped = st.tuples(st.lists(st.sampled_from(q.FAULTS), max_size=3),
                   st.one_of(st.none(), st.sampled_from(q.ANSWERS))).map(
    lambda t: (*t[0], *(() if t[1] is None else (t[1],))))
anything = st.lists(st.sampled_from((*OUTCOMES, *q.FAULTS, UNKNOWN)), max_size=5).map(tuple)
sequence = st.one_of(shaped, anything)
record = st.sampled_from((*RECORDS, *RECORDS, *RECORDS, UNKNOWN))


def report(label: str, n: collections.Counter) -> None:
    print(f"{label}: {sum(n.values())} cases, no disagreement")
    for k, v in sorted(n.items()):
        print(f"  {v:6d}  {k}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["agree", "all"])
    ap.add_argument("--cases", type=int, default=2000, help="agree: how many cases")
    ap.add_argument("--length", type=int, default=4, help="all: the longest sequence")
    ap.add_argument("--mutant", choices=sorted(MUTANTS), default=None,
                    help="break the real rule on purpose, to show the harness notices")
    a = ap.parse_args()
    if a.mutant:
        MUTANTS[a.mutant]()
    label = f"{a.mode}{' --mutant ' + a.mutant if a.mutant else ''}"
    n: collections.Counter = collections.Counter()

    if a.mode == "all":
        for k in range(a.length + 1):
            for seq in itertools.product((*OUTCOMES, UNKNOWN), repeat=k):
                for r in (*RECORDS, UNKNOWN):
                    for retry in (False, True):
                        try:
                            check(seq, r, retry, n)
                        except Finding as e:
                            print(f"{label}: minimal disagreement\n{e}")
                            return 1
        report(label, n)
        return 0

    @settings(max_examples=a.cases, **SETTINGS)
    @given(sequence, record, st.booleans())
    def agree(outcomes, r, retry):
        check(outcomes, r, retry, n)

    found: list[Exception] = []
    try:
        agree()
    except* Finding as group:
        # Hypothesis reports each distinct disagreement it found; a HarnessBug is never caught.
        found.extend(group.exceptions)
    if found:
        for e in found:
            print(f"{label}: minimal disagreement\n{e}")
        return 1
    report(label, n)
    return 0


if __name__ == "__main__":
    LEAN = None
    try:
        LEAN = Lean()
        sys.exit(main())
    finally:
        if LEAN is not None:
            LEAN.p.stdin.close()
            LEAN.p.wait()
