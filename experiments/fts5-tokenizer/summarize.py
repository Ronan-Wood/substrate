"""Table the `substrate eval` outputs a run directory holds: one row per (corpus, set, stack).

    python summarize.py RUNDIR

Reads `<corpus>.<mode>.<arm>.txt`, the redirected stdout `run_matrix.sh` writes, and pulls
the two MRRs the engine's report() prints: the lexical cohort's (METRICS block) and the semantic cohort's.
Per-case ranks are diffed so a moved MRR can be traced to the cases that moved it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ARMS = ("unicode61", "substrate")


def parse(path: Path) -> dict:
    t = path.read_text("utf-8")
    out: dict = {"fatal": "FATAL" in t}
    m = re.search(r"^\s+LEXICAL\s+(\d+)/(\d+) pass", t, re.M)
    if m:
        out["lex_pass"], out["lex_n"] = int(m[1]), int(m[2])
    m = re.search(r"^\s+SEMANTIC\s+(\d+)/(\d+) pass", t, re.M)
    if m:
        out["sem_pass"], out["sem_n"] = int(m[1]), int(m[2])
    m = re.search(r"^\s+mrr\s+([\d.]+)", t, re.M)
    if m:
        out["lex_mrr"] = float(m[1])
    m = re.search(r"SEMANTIC COHORT — mrr ([\d.]+)", t)
    if m:
        out["sem_mrr"] = float(m[1])
    out["cases"] = {cid: r.lower() for cid, r in
                    re.findall(r"^\s+(?:PASS|gap)\s+(\S+)\s+(rank \d+|miss)", t, re.M | re.I)}
    return out


def main() -> None:
    d = Path(sys.argv[1])
    keys = sorted({p.name.rsplit(".", 2)[0] for p in d.glob("*.txt")})
    print("| corpus | set · stack | lexical MRR (n) | semantic MRR (n) |")
    print("|---|---|---|---|")
    moved = []
    for k in keys:
        r = {a: parse(d / f"{k}.{a}.txt") for a in ARMS if (d / f"{k}.{a}.txt").exists()}
        if len(r) < 2 or any(v["fatal"] for v in r.values()):
            print(f"| {k} | INCOMPLETE OR FATAL | | |")
            continue
        u, s = r["unicode61"], r["substrate"]
        corpus, mode = k.split(".")

        def cell(m, n):
            return (f"{u[m]:.3f} → **{s[m]:.3f}** ({s[m]-u[m]:+.3f}) · {u[n]}"
                    if m in u and m in s else "—")

        print(f"| {corpus} | {mode} | {cell('lex_mrr', 'lex_n')} | {cell('sem_mrr', 'sem_n')} |")
        for cid in sorted(set(u["cases"]) | set(s["cases"])):
            a, b = u["cases"].get(cid, "?"), s["cases"].get(cid, "?")
            if a != b:
                moved.append(f"{k}: {cid}  {a} → {b}")
    if moved:
        print("\nsemantic-cohort cases that moved (report() lists lexical cases only when failing):")
        for line in moved:
            print("  " + line)


if __name__ == "__main__":
    main()
