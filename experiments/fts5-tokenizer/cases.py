"""Per-case ranks, unicode61 vs substrate, lexical-only, through the engine's own eval runner.

    python cases.py IDX_DIR CORPUS GOLD.json

report() prints a lexical-cohort case only when it fails, so an MRR that moved cannot be traced
from its output. This calls `substrate.eval.runner.run` with no embedder, expander or reranker,
the same lexical-only configuration `substrate eval --no-vector --no-hyde --no-rerank` runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SUBSTRATE_ENGINE: the engine tree to import (the directory holding the `substrate` package).
sys.path.insert(0, os.environ.get("SUBSTRATE_ENGINE") or str(HERE.parent.parent / "substrate"))
sys.path.insert(0, str(HERE))

import substrate_tok  # noqa: E402

substrate_tok.install()

from substrate.eval.runner import run  # noqa: E402


def main() -> None:
    idx, corpus, gold = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    res = {}
    for arm in ("unicode61", "substrate"):
        summary, _ = run(str(idx / f"{corpus}.{arm}.db"), gold)
        res[arm] = {c.id: (c.cohort, c.both_rank) for c in summary.cases}
    fmt = lambda r: f"{r}" if r else "miss"  # noqa: E731
    up = down = 0
    for cid, (cohort, a) in res["unicode61"].items():
        b = res["substrate"][cid][1]
        if a == b:
            continue
        better = (b or 99) < (a or 99)
        up, down = up + better, down + (not better)
        print(f"  {'↑' if better else '↓'} {cohort:8} {cid:34} {fmt(a):>4} → {fmt(b)}")
    print(f"  {corpus} {gold.name}: {up} better, {down} worse, "
          f"{len(res['unicode61']) - up - down} unchanged")


if __name__ == "__main__":
    main()
