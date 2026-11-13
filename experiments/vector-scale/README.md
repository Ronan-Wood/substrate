---
title: Keep brute-force vector search until a scope holds ~250k–500k passages; then usearch behind an exact-fallback planner
status: active
doc_type: decision
confidence: proposed
domains: [software-dev, scripta, performance]
---

# Keep brute force until ~250k–500k passages per scope; then usearch, built serially, behind a planner

Substrate's vector arm should stay brute force. **That holds only once the matrix cache in progress
lands.** Today's arm rebuilds the matrix from SQL on every call (~25 ms on vault-a, per the 2026-11-06
runtime profile), and the fix for that is the cache, not an index. With the cache, brute force holds
until one scope reaches roughly **250k–500k passage vectors**, which is **~67–134× vault-a's 3,721
passages today**. Past that, the index to use is usearch, with three conditions:
- built single-threaded, or self-checked after the build;
- every filter routed through the planner in `bench_ann.planned`;
- a bigger graph as the scope grows. M 32 with ef_construction 256 was measured only at 1M; at
  500k, M 16 already needs ef 512 to reach recall 0.95.

The measurements behind every number below are in [FINDINGS.md](FINDINGS.md) and
[results/RESULTS.md](results/RESULTS.md), taken on an M2 Max with Ollama resident and swap nearly
full. Swap is recorded per row.

| question | answer |
|---|---|
| crossover | 250k passages: brute force 23.1 ms per query. 500k: 44.5 ms. That is 16–36% of the `fast` path's 124–147 ms, invisible next to a full search's 5.8–7.3 s. Those two path times are `[stated]` from the 2026-11-06 runtime profile, not measured here. **`[inferred]`**: the threshold is a judgement on those costs |
| headroom today | vault-a 3.7k passages: 0.55 ms with the cache (measured at 7.2k vectors) |
| what ANN buys at 1M | brute force 90.6 ms. usearch M 32: recall@10 0.963 at 3.5 ms (26×), 0.977 at 6.9 ms (13×). faiss M 32: 0.956 at 1.0 ms (89×), 0.988 at 2.0 ms (45×). ANN queries run on one thread, while brute force uses Accelerate's own threading, and the two were measured in separate passes |
| what ANN does not buy | memory: an HNSW index is the vectors + 3.5–6.8%, 3.95–4.07 GiB at 1M. Only int8 shrinks it (1.09 GiB), and it caps recall@10 at 0.88 (1M) to 0.96 (10k) |
| what it costs in quality | under the default filter, recall on the planner's ANN path at 1M (M 32) is 0.87–0.88 at k 30, against 0.94–0.95 unfiltered at the same k and ef. At k 10 it is 0.90 against 0.96. The ANN-path figure leaves out the fallback queries, so the gap is if anything understated |
| filters | **no library's own filtering meets the every-arm rule** (details below). The planner cannot be short, by construction. With ≤ 10k rows passing it goes exact: 0.6–4.9 ms at 1M, mask build included. Otherwise it uses ANN with an exact fallback, which fired on 0–6.5% of queries |
| shipping usearch | one dependency, 2 Mach-O files (3.4 MB), 2 licence files. No entitlement, no interpreter-pin change. A matching `cpXY` wheel is needed on each Python minor bump `[stated]` |

Filters at 1M, k = 30, in detail:
- **faiss `IDSelector`** is short on 102–195 of 200 entity queries (M 32 / M 16), and on 200 of 200
  rare-entity or 5-row queries.
- **hnswlib's callback** is short on none while at least k rows pass, but costs 8.4 ms (default
  filter) to 1.03 s (rare filter). With 5 rows passing, all 200 calls raise and return nothing.
- **A post-filter** is short on 151–158 entity queries, and on 200 of 200 rare or 5-row queries.

**Why usearch.** `[stated]` from PyPI and `otool`: it is Apache-2.0 and released four times in
October 2026, and its wheels link only libc++ and libSystem. It supports deletes, which edited notes
need. Measured: it opens its 3.95–4.07 GiB indexes as memory-mapped views in 0.06–0.09 s, and
queries on those views ran 1.8–2.3× slower than on the in-memory index, still from page cache.

**Its two costs, both measured.**
- **No filtered search in the Python binding**, so the planner is mandatory. Every library needs it
  anyway.
- **Its 12-thread build leaves rows unreachable.**
  - On the real scopes, 0.4–2.4% of rows cannot find themselves, and recall@10 plateaus at
    0.979–0.997.
  - Built single-threaded (`--build-threads 1`), the same scopes show 0.0%, ≥ 0.9995 by ef 64 and
    1.000 by ef 256, in 0.15–2.4 s instead of 0.04–0.67 s.
  - Synthetic 10k: 0.3% → 0.0%.
  - At 100k the serial build gained nothing measurable (0.2% → 0.1%). Its build took 8× longer,
    but it ran under higher swap, so that ratio is not like-for-like.
  - At 1M only 12-thread builds were measured. There M 16 leaves 1.5–2.9% unreachable in every
    library, faiss and hnswlib included.

**Rejected.** The release history, wheel and licence facts below are `[stated]` from PyPI; the rest
is measured or reproduced here.
- **faiss.** It aborts the engine: its bundled libomp and torch's cannot share a process (`OMP:
  Error #15`, exit 134, reproduced against the engine's own `.venv`). Its HNSW cannot delete. Its
  native filter returns short answers. It is the fastest float32 index per query, so it is worth
  reconsidering only if docling ever drops torch.
- **hnswlib.** Last released December 2023, no wheel (built from source), and 4.0–7.5× slower per
  query than faiss in the unfiltered sweep (2.7–10.4× on real data), on Apple silicon. Its filter
  callback holds only while at least k rows pass.
- **sqlite-vec.** Exact, so its filters hold, but unfiltered it is 8.3–9.8× slower than the cached
  brute force it would replace, at every size (810 ms against 90.6 ms at 1M). Its ANN modes are
  undocumented 0.1.10 alphas.
- **Doing nothing about memory.** At 500k the engine would hold 1.9 GiB of float32 beside Ollama's
  ~12 GB. Memory-mapping the matrix (what this experiment did) is the cheap first step. float16
  storage would need a non-numpy kernel: numpy's float16 matvec is 18–21× slower.

**Consequence.** Nothing changes in the engine now. The trigger to revisit is a scope passing ~250k
passages, or the `fast` path's vector-arm time passing ~20 ms with the cache in place. The planner
and the build self-check are the parts to carry over. The two library-level problems found here are
unreproduced upstream and unreported: usearch's parallel-build reachability and faiss's libomp
conflict.
