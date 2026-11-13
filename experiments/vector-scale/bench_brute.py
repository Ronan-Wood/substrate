"""Brute force at scale: the cached-matrix vector arm, timed per single query.

usage: .venv/bin/python bench_brute.py N   (appends one JSON line to results/raw/brute.jsonl)

This is the arm AFTER a matrix cache, not substrate's arm today: `IndexStore.vector_search` runs a SQL
join and rebuilds the matrix from blobs on every call (~25 ms on vault-a, profile of 2026-11-06).

Ways to apply a filter, because they cost differently at scale:
  mask    — matvec over every row, set the excluded scores to -inf, argpartition. Cost is flat in
            selectivity: it is the full matvec whatever the filter keeps.
  subset  — gather the allowed rows (`x[idx]`), matvec over those only. Cost scales with what the
            filter keeps, plus the gather copy. `subset_curve` times it at fixed row counts, which is
            what the planner's EXACT_ROWS threshold in bench_ann.py is read from.
  maskbuild — what a planner pays per query before it can choose a path: build the filter's mask
            from cached attribute arrays and find its rows (`common.mask_fns`).
Variants that were claims before they were measurements:
  threaded — the matvec split across 8 threads (numpy releases the GIL)
  f16      — numpy float16 storage, at n <= 100k only (there is no BLAS for it)

Every group of conditions is timed INTERLEAVED per query (`common.timed_interleaved`), so drift over
the run does not land on whichever condition happens to be measured first.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import common as c

SUBSET_ROWS = (2_000, 5_000, 10_000, 20_000, 50_000)
THREADS = 8


def main(n: int) -> None:
    t0 = time.perf_counter()
    x, q, attrs, ms, gt = c.setup(n)
    out = {"n": n, "matrix_mb": x.nbytes / 2**20, "rss_mb": c.rss() / 2**20,
           "setup_s": time.perf_counter() - t0, "queries": len(q), **c.memory_conditions()}
    gt10 = gt[c.K]

    # The raw kernels, interleaved, each writing into a preallocated buffer and keeping nothing: a
    # baseline that allocates and retains 200 fresh score vectors is slower for reasons unrelated to
    # the matvec.
    pool = ThreadPoolExecutor(THREADS)
    bounds = np.linspace(0, n, THREADS + 1).astype(int)
    buf1, buf8 = np.empty(n, dtype=np.float32), np.empty(n, dtype=np.float32)

    def threaded(qv):
        list(pool.map(lambda j: np.dot(x[bounds[j]:bounds[j + 1]], qv, out=buf8[bounds[j]:bounds[j + 1]]),
                      range(THREADS)))
        return buf8

    assert np.allclose(threaded(q[0]), x @ q[0], atol=1e-5)
    sims = x @ q[0]
    kern = {"matvec": lambda qv: np.dot(x, qv, out=buf1), "threaded_matvec": threaded,
            "argpartition": lambda qv: np.argpartition(-sims, c.K - 1)[: c.K]}
    if n <= 100_000:
        x16 = np.asarray(x, dtype=np.float16)
        kern["f16_matvec"] = lambda qv: x16 @ qv.astype(np.float16)
    for name, (_, med, p95) in c.timed_interleaved(kern, q, keep=False).items():
        out[f"{name}_ms"], out[f"{name}_p95"] = med, p95

    # Every filtered way to search, interleaved per query, results checked against ground truth.
    builders = c.mask_fns(attrs)
    full = {}
    for fname, m in ms.items():
        idx = np.flatnonzero(m)
        full[f"{fname}_mask"] = (lambda m: lambda qv: c.exact_topk(x, qv, mask=m))(None if fname == "none" else m)
        if fname != "none":
            full[f"{fname}_subset"] = (lambda idx: lambda qv: idx[c.exact_topk(x[idx], qv)] if len(idx) else idx)(idx)
            # What a planner pays per query before it can choose a path: build the mask, find its rows.
            # Returns None: 200 kept row arrays would be ~480 MB at 1M, the pressure being measured.
            full[f"{fname}_maskbuild"] = (lambda b: lambda qv: np.flatnonzero(b()) is None)(builders[fname])
        out[f"{fname}_allowed"] = int(m.sum())
    for name, (res, med, p95) in c.timed_interleaved(full, q).items():
        out[f"{name}_ms"], out[f"{name}_p95"] = med, p95
        fname, how = name.rsplit("_", 1)
        if how != "maskbuild":
            sc = c.score(res, gt10[fname], ms[fname], c.K)
            assert sc["recall"] == 1.0 and sc["short"] == 0 and sc["violations"] == 0, (name, sc)

    rng = np.random.default_rng(c.SEED)
    curve = {}
    for rows in SUBSET_ROWS:
        if rows < n:
            idx = np.sort(rng.choice(n, rows, replace=False))
            curve[rows] = (lambda idx: lambda qv: idx[c.exact_topk(x[idx], qv)])(idx)
    out["subset_curve"] = {r: med for r, (_, med, _) in c.timed_interleaved(curve, q, keep=False).items()}

    out["peak_rss_mb"] = c.peak_rss() / 2**20
    (c.HERE / "results" / "raw").mkdir(parents=True, exist_ok=True)
    with open(c.HERE / "results" / "raw" / "brute.jsonl", "a") as f:
        f.write(json.dumps(out) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(int(sys.argv[1]))
