"""Every theorem `Axioms.lean` names is checked, and none rests on more than Lean's standard axioms.

usage: python3 experiments/lean-blob-gc/lean/check_axioms.py      (after `lake build`)

Runs `lake env lean Axioms.lean` here and reads what it prints. It fails when Lean fails, when a
theorem `Axioms.lean` asks about is not reported, or when one depends on an axiom outside
`propext`, `Classical.choice` and `Quot.sound`: `sorryAx` (a `sorry`), or anything a proof assumed
rather than proved. The theorems are the ones `Axioms.lean` lists, so adding one there is enough.
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STANDARD = {"propext", "Classical.choice", "Quot.sound"}

wanted = re.findall(r"^#print axioms (\S+)", (HERE / "Axioms.lean").read_text(), re.M)
run = subprocess.run(["lake", "env", "lean", "Axioms.lean"], cwd=HERE, capture_output=True,
                     text=True)
print(run.stdout, end="")
if run.returncode:
    print(run.stderr, end="", file=sys.stderr)
    sys.exit(f"lean exited {run.returncode} on Axioms.lean")
uses = {n.rsplit(".", 1)[-1]: {a.strip() for a in axs.split(",") if a.strip()}
        for n, axs in re.findall(r"'([^']+)' depends on axioms: \[([^\]]*)\]", run.stdout)}
uses.update({n.rsplit(".", 1)[-1]: set()
             for n in re.findall(r"'([^']+)' does not depend on any axioms", run.stdout)})
missing = [n for n in wanted if n not in uses]
other = {n: sorted(a - STANDARD) for n, a in uses.items() if a - STANDARD}
print(f"{len(wanted)} theorems asked about, {len(uses)} reported; "
      f"not reported: {missing or 'none'}; outside the standard axioms: {other or 'none'}")
sys.exit(1 if not wanted or missing or other else 0)
