"""Paired scoring of the large private eval set: unicode61 vs substrate, per case family.

    python score_large.py [STACK ...]

Reads `<scope>.<stack>.<arm>.json` (the engine's --json-out, written by run_large.sh) and the gold
files from $PRIVATE_EVAL (default ~/CodeHome/substrate-eval-private). Prints AGGREGATES ONLY: no
query, note, heading or case id. Scopes are printed under the neutral labels in
$PRIVATE_EVAL/fts5-tokenizer/labels.json, so the output can go into a public README.

Per positive case, at k=10: RR = 1/both_rank (0 past 10), R@5 and R@10 are hits within 5 and 10.
The difference substrate − unicode61 is paired per case, and its 95% CI (t, g−1 df) is cluster-robust by target
note (`_note_sha256`; a core-vault note shared by several scopes is one cluster), because cases about
the same note are not independent. Families strip the `core-` prefix: identifier, kw (agent-style
keyword bag), lexq (question in the note's words), para (blind paraphrase), real (mined from
transcripts), and respelled-* for make_respelled.py's derived cases (stack names `resp-<stack>`,
and `snk-<stack>` for its camel→snake set).
NEGATIVE cases (neg-*) report leaks per arm; UNANSWERABLE cases are not scored.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

PRIVATE = Path(os.environ.get("PRIVATE_EVAL") or Path.home() / "CodeHome/substrate-eval-private")
RESULTS = PRIVATE / "fts5-tokenizer" / os.environ.get("RESULTS", "results")
SCOPES = tuple(json.loads((PRIVATE / "fts5-tokenizer" / "labels.json").read_text()))
FAMILIES = ("identifier", "kw", "lexq", "para", "real")


def family(stratum: str) -> str:
    return stratum.removeprefix("core-")


def load(scope: str, stack: str):
    sets = {"resp-": "gold-respelled-", "snk-": "gold-respelled-snake-"}
    pre = next((v for k, v in sets.items() if stack.startswith(k)), None)
    gp = PRIVATE / "fts5-tokenizer" / f"{pre}{scope}.json" if pre else PRIVATE / "gold" / f"gold-{scope}.json"
    gold = {c["id"]: c for c in json.loads(gp.read_text())["cases"]}
    arms = {}
    for arm in ("unicode61", "substrate"):
        p = RESULTS / f"{scope}.{stack}.{arm}.json"
        if not p.exists():
            return None
        arms[arm] = {c["id"]: c for c in json.loads(p.read_text())["cases"]}
    if set(arms["unicode61"]) != set(arms["substrate"]):
        sys.exit(f"REFUSING: {scope}.{stack} arms ran different case sets")
    if not set(arms["unicode61"]) <= set(gold):
        sys.exit(f"REFUSING: {scope}.{stack} results hold cases its gold file does not; stale results?")
    return gold, arms


def rr(c) -> float:
    r = c.get("both_rank")
    return 1.0 / r if r and r <= 10 else 0.0


def hit(c, k: int) -> float:
    r = c.get("both_rank")
    return 1.0 if r and r <= k else 0.0


# Two-sided 97.5% quantiles of Student's t by degrees of freedom; past the table, the normal's.
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000,
        120: 1.980}


def t975(df: int) -> float:
    """The table's value at the largest tabulated df not above `df`, so never too narrow."""
    return next((T975[k] for k in sorted(T975, reverse=True) if k <= df), 1.96) if df < 1000 else 1.96


def paired(rows):
    """rows: (cluster, a, b). Mean of a, of b, of b−a, and a cluster-robust 95% half-width using
    t with g−1 degrees of freedom. The half-width is None with fewer than 2 clusters."""
    n = len(rows)
    if n == 0:
        return None
    ma = sum(r[1] for r in rows) / n
    mb = sum(r[2] for r in rows) / n
    md = mb - ma
    by = defaultdict(float)
    for cl, a, b in rows:
        by[cl] += (b - a) - md
    g = len(by)
    if g < 2:
        return ma, mb, md, None, n, g
    var = sum(v * v for v in by.values()) / (n * n) * (g / (g - 1))
    return ma, mb, md, t975(g - 1) * math.sqrt(var), n, g


def delta(p, digits=3):
    md, hw = p[2], p[3]
    if hw is None:
        return f"{md:+.{digits}f} (no CI: 1 note)"
    sig = "**" if abs(md) > hw else ""
    return f"{sig}{md:+.{digits}f} ± {hw:.{digits}f}{sig}"


def fmt(p, digits=3):
    if p is None:
        return "—"
    return f"{p[0]:.{digits}f} → {p[1]:.{digits}f} ({delta(p, digits)})"


def main() -> None:
    stacks = sys.argv[1:] or ["lex", "hybrid"]
    labels = json.loads((PRIVATE / "fts5-tokenizer" / "labels.json").read_text())
    for stack in stacks:
        pooled = defaultdict(list)   # metric, family -> rows
        per_scope = defaultdict(list)
        leaks = defaultdict(lambda: [0, 0, 0])
        have = []
        for scope in SCOPES:
            got = load(scope, stack)
            if got is None:
                continue
            have.append(scope)
            gold, arms = got
            for cid, u in arms["unicode61"].items():
                s = arms["substrate"][cid]
                g = gold[cid]
                st = family(g.get("stratum", ""))
                if st.startswith("neg-"):
                    leaks[labels[scope]][0] += 1
                    leaks[labels[scope]][1] += u.get("absent_rank") is not None
                    leaks[labels[scope]][2] += s.get("absent_rank") is not None
                    continue
                # Unanswerable cases are counted, never scored; `real` cases are graded by `targets`.
                if st.startswith("unans") or st == "real-none" or not (g.get("answer") or g.get("targets")):
                    continue
                cl = g.get("_note_sha256") or f"{scope}:{cid}"
                fam = st
                for m, f in (("mrr", rr), ("r5", lambda c: hit(c, 5)), ("r10", lambda c: hit(c, 10))):
                    row = (cl, f(u), f(s))
                    pooled[(m, fam)].append(row)
                    pooled[(m, "all")].append(row)
                    pooled[(m, "cohort:" + (g.get("cohort") or "none"))].append(row)
                    if m == "mrr":
                        per_scope[(labels[scope], fam)].append(row)
                        per_scope[(labels[scope], "all")].append(row)
        if not have:
            print(f"## {stack}: no results yet\n")
            continue
        print(f"## {stack} — {len(have)} vaults, unicode61 → substrate (Δ ± 95% CI, clustered by note; "
              "bold = CI excludes 0)\n")
        print("| family | n (notes) | MRR@10 | recall@5 | recall@10 |")
        print("|---|---|---|---|---|")
        fams = sorted({f for (m, f) in pooled if m == "mrr" and f != "all" and not f.startswith("cohort:")},
                      key=lambda f: (FAMILIES.index(f) if f in FAMILIES else len(FAMILIES), f))
        for fam in (*fams, "all", "cohort:lexical", "cohort:semantic"):
            p = paired(pooled[("mrr", fam)])
            if p is None:
                continue
            print(f"| {fam} | {p[4]} ({p[5]}) | {fmt(p)} | {fmt(paired(pooled[('r5', fam)]))} | "
                  f"{fmt(paired(pooled[('r10', fam)]))} |")
        print("\n| vault | " + " | ".join(fams) + " | all |")
        print("|---|" + "---|" * (len(fams) + 1))
        for lab in sorted({labels[s] for s in have}):
            cells = []
            for fam in (*fams, "all"):
                p = paired(per_scope[(lab, fam)])
                cells.append("—" if p is None else f"{delta(p)} (n {p[4]})")
            print(f"| {lab} | " + " | ".join(cells) + " |")
        if leaks:
            print("\nNegative cases leaked (must be 0): " + ", ".join(
                f"{lab} {v[1]}→{v[2]} of {v[0]}" for lab, v in sorted(leaks.items())))
        print()


if __name__ == "__main__":
    main()
