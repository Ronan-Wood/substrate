---
title: Vector search at scale — measurements
status: active
doc_type: reference
confidence: verified
domains: [software-dev, scripta, performance]
---

# Vector search at scale — measurements (2026-11-06)

The decision these support is in [README.md](README.md). Every table here is generated into
[results/RESULTS.md](results/RESULTS.md) by `report.py` from `results/raw/*.jsonl`. The two
inventories at the end (libraries, cost to ship) are **`[stated]`**: they come from wheel metadata,
PyPI and `otool`, not from a benchmark.

## 1. Brute force: linear at ~9 ms per 100k vectors, bandwidth-bound, memory first

This is the arm **with** a cached float32 matrix, memory-mapped and warmed. Substrate does not have
that yet: today's `vector_search` joins in SQL and rebuilds the matrix on every call, ~25 ms on vault-a.
That figure is `[stated]` from the 2026-11-06 runtime profile, not measured here. Every condition in
a group is timed interleaved per query (kernels, filtered conditions and the subset curve are three
groups), and the raw kernels write into a preallocated buffer.

| vectors | matrix | unfiltered, median (p95) | matvec | 8 threads | float16 | default filter, mask | entity filter, subset | mask build | swap used/total |
|---|---|---|---|---|---|---|---|---|---|
| 7.2k | 28 MiB | 0.55 (0.66) ms | 0.56 | 0.43 | 11.7 | 0.55 | 0.031 | 0.02 | 16.3/17.0 GB |
| 100k | 391 MiB | 9.45 (10.5) ms | 9.11 | 5.21 | 160 | 9.63 | 0.38 | 0.15 | 16.3/17.0 |
| 250k | 977 MiB | 23.1 (25.4) ms | 21.8 | 12.8 | — | 23.8 | 1.08 | 0.31 | 16.2/17.0 |
| 500k | 1.91 GiB | 44.5 (49.2) ms | 43.0 | 25.6 | — | 45.8 | 1.9 | 0.59 | 16.2/17.0 |
| 1M | 3.81 GiB | 90.6 (106) ms | 91.2 | 51.0 | — | 91.2 | 4.0 | 1.18 | 16.2/17.0 |

- **Bandwidth-bound.** The matvec reads the matrix at 45–48 GB/s (decimal) from 100k up, and at
  50–53 GB/s at 7.2k–10k.
  - `matvec` is numpy's default call, where Accelerate picks its own threading. Splitting it over 8
    Python threads helps 1.3–1.8× on top of that.
  - numpy's float16 matvec is 18–21× *slower*. The likely reason is that it has no BLAS path
    **`[inferred]`**.
  - `argpartition`, timed alone, is 6–15% of the unfiltered median.
  - The raw kernels (matvec, 8 threads, float16, argpartition) and the filtered conditions are two
    separate interleaved groups, and the subset curve is a third. So their figures do not add up
    exactly: the bare matvec at 1M (91.2 ms) is slower than the full unfiltered top-k (90.6 ms).
  - The unfiltered column's p95s are within 1.19× of their medians in this pass. Other conditions
    spread more: up to 1.6× for argpartition at 1M and 1.7× for the subset paths.
- **Mask unless the filter is selective.** A mask costs the full matvec whatever passes.
- **Subset cost is 0.25–0.67 ms per 1,000 rows** (RESULTS §1 curve): 5.3–6.4 ms at 10k rows on every
  corpus that holds 10k rows (100k–1M). At 50k rows it grows with the corpus (17.7 ms at 100k →
  30.8 ms at 1M). The planner's exact threshold sits at 10k rows, where the cost is ≤ 6.4 ms.
- **A planner also pays the mask build**: 0.02–1.18 ms per query to build the default filter from
  cached attribute arrays and find its rows.
- **Memory**: 4 KB per vector, resident while warm.

## 2. Filters: every library breaks the every-arm rule unless it is wrapped

`short` = queries of 200 returning fewer than min(k, rows passing). sqlite-vec runs 30 queries at
≥ 500k. Synthetic, 1M, ef = 128 (the planner's and post-filter's ANN searches widen to
max(128, k / selectivity × 2), 199 here), k = 30 (what the arm requests). Each strategy builds the filter's
mask per query inside its timing. The native filters also build their bitmap or callback per query,
because the filter arrives with the query. The exception is sqlite-vec, whose filter is SQL over its
own tables, prepared once and untimed. hnswlib's callback is a `bytes` lookup, the cheapest
callable found for it **`[inferred]`**. faiss's per-query cost also includes building its
`SearchParametersHNSW`, which the other libraries do not pay.

| strategy | default (30% pass) | entity (0.6%) | rare (0.03%) | few (5 rows) |
|---|---|---|---|---|
| faiss `IDSelector`, M 16 / M 32 | 0 / 0 short; recall 0.69 / 0.81 | **195 / 102 short** | **200 / 200** | **200 / 200** |
| hnswlib callback | 0 short, 0.88, 8.4 ms | 0 short, 0.98, **167 ms** | 0 short, 0.99, **1.03 s** | **200 short: all 200 calls raise**, 1.38 s |
| post-filter, any library (cap 4,096) | 0–1 short | **151–158 short** | **200** | **200** |
| **planned** | **0 short**, recall 0.72–0.88, 1.9–7.1 ms, fallback 0–1 | **0, exact**, 4.0–4.9 ms | **0, exact**, 0.8–0.9 ms | **0, exact**, 0.6–0.7 ms |
| sqlite-vec (SQL `WHERE`, exact; 30 queries) | 0, 810 ms | 0, 853 ms | 0, 879 ms | 0, 875 ms |

- **faiss's in-graph filter is fast and wrong once a filter is selective.** It pads with `-1`. At
  k = 10 the entity filter is short on 19 of 200 (M 16) and 0 (M 32) at 1M, and on 75 at 100k.
- **hnswlib's callback is correct only while at least k rows pass.** It is a Python call per
  visited node, so its 0.17–1.4 s at 1M are mostly callback overhead rather than traversal
  **`[inferred]`**. Asked for k with fewer than k found, `knn_query` raises and returns nothing
  (counted as `raised` in RESULTS), so a 5-row filter gets no answer. A caller can avoid that only by
  capping k at the passing count, which needs the mask the planner builds anyway. That cap was not
  measured.
- **The planner** goes exact when ≤ 10k rows pass. Otherwise it post-filters, and falls back to
  exact over the mask when that comes back short. **Zero short is guaranteed by its code, not
  measured.** What is measured:
  - The fallback rate: 0–13 of 200 queries, ≤ 6.5%.
  - Recall on the queries that took the ANN path alone (`ann-path recall`), against unfiltered
    recall at the same k and the same ef the planner searched at. At 1M under the default filter
    with M 32:
    - k = 30: 0.871–0.876 against 0.943–0.954, at ef 199;
    - k = 10: 0.897–0.899 against 0.956–0.963, at ef 128.

    The two sides are not the same query set. The ANN-path figure leaves out the 0–13 queries that
    fell back, which are the hardest, and the unfiltered figure covers all 200. So the gap is if
    anything understated. A vault filter removing whole regions of the graph is the likely cause
    **`[inferred]`**.
  - Latency with the mask build included.

  It needs the float32 vectors kept addressable for the exact paths. usearch (`get`) and faiss
  (`reconstruct`) can return stored vectors, so that need not be a second copy **`[inferred]`**: it
  was not tried here.

**Real scopes** (passage chunks, held-out queries, RESULTS §4). The post-filter short counts, as
ranges across libraries:

| filter | rows passing | queries whose own row passes | k = 10 | k = 30 |
|---|---|---|---|---|
| vault-a default | 2,448 | 137 | 44–45 of 200 | 42–44 |
| vault-a vault | 2,210 | 119 | 48 | 42–43 |
| vault-b default | 1,857 | 196 | 3 | 0 |
| vault-b vault | 1,632 | 165 | 36 | 36 |
| vault-b entity | 105 | 17 | 56–59 | 0 (at k = 30 the post-filter asks for 1,093 of 1,912 rows) |
| scripta default | 537 | 63 of 73 | 3–4 | 1 |
| scripta vault | 305 | 39 of 73 | 17–18 | 2–3 |

**All but 0–2 per filter come from queries whose own row the filter excludes**, such as a conversation
chunk under the default filter. A real question can land in that region, but that is not the
typical case. faiss's native filter on vault-b's top entity (105 rows) was short on 31 (k = 10) and
82 (k = 30) of 200, none of them among the 17 queries that pass it. The planner was exact on every
real filter (all ≤ 10k rows) and never short, which is brute force by construction and says nothing
about its ANN path on real vectors. Real masks are precomputed and not timed: at ≤ 3.5k
rows, building one is cheap next to the search (1M rows took 1.18 ms, §1).

## 3. ANN at scale: much faster, no smaller, and the graph has to grow

| 1M vectors | build | disk | load (warm cache) | unreachable rows | recall@10 at ef 128 / 256 / 512 | ms at ef 128 / 256 / 512 | recall@30, ef 128 |
|---|---|---|---|---|---|---|---|
| brute force | none | 3.81 GiB | n/a | 0 | 1.000 | 90.6 | 1.000 (exact by construction; only k 10 is scored) |
| faiss, M 16 | 189 s | 3.95 GiB | 0.69 s | 2.9% | 0.805 / 0.848 / 0.892 | 0.55 / 1.1 / 2.2 | 0.800 |
| usearch, M 16 | 303 s | 3.95 GiB | 0.06 s view; queries 3.3 ms | 1.5% | 0.853 / 0.887 / 0.923 | 1.4 / 2.9 / 5.7 | 0.822 |
| usearch int8, M 16 | 138 s | 1.09 GiB | 0.04 s view; queries 1.5 ms | 2.0% | 0.813 / 0.856 / 0.883 | 0.6 / 1.1 / 2.3 | 0.762 |
| hnswlib, M 16 | 430 s | 3.95 GiB | 2.70 s | 1.5% | 0.847 / 0.893 / 0.917 | 2.7 / 4.9 / 9.0 | 0.821 |
| **faiss, M 32, efc 256** | 653 s | 4.07 GiB | 1.28 s | 0.0%* | **0.956 / 0.988 / 0.992** | **1.0 / 2.0 / 3.9** | 0.919 |
| **usearch, M 32, efc 256** | 904 s | 4.07 GiB | 0.09 s view; queries 6.3 ms | 0.1%* | **0.963 / 0.977 / 0.993** | **3.5 / 6.9 / 13.4** | 0.915 |

Unreachable rows are measured at the widest ef of each sweep: 512 for M 16, 2048 for M 32 (*). So
the M 32 figures are not like-for-like with M 16's. The ANN queries are single-threaded, while the
brute-force baseline is numpy's default call (Accelerate picks its threads), so the speedups compare
one query thread against Accelerate's. The brute-force row comes from the later brute-force pass
(§1), at 16.2 of 17.0 GB swap. The 1M M 16 ANN rows ran at 14.3–15.7 of 16–17 GB, and the M 32 rows
at 14.3–17.9 of 16–19 GB.

The loads are timed right after the save, so they are warm-cache deserialization, not a cold disk
read. A usearch view reads nothing at load time. Its query time at ef 128 (`queries`) is measured
after a 10-query reload check has touched the upper graph, still from page cache. It is 2.3× the
in-memory index's for usearch M 16 (3.95 GiB), 1.8× for M 32 (4.07 GiB) and 2.7× for int8. Every other latency in the
table is from the in-memory index.

- **M = 16 is enough at 100k** for the float32 indexes (0.98–0.99 at ef 128) **and not at 1M**
  (≤ 0.923 even at ef 512).
  - At 1M, 1.5–2.9% of rows are unreachable.
  - M = 32 together with ef_construction 256 restores recall@10 at 3.0–3.5× the build time and +3%
    disk. The two were not varied separately, and the larger graph was run only at 1M.
  - At 500k, M 16 reaches 0.95 only at ef 256 (hnswlib) or ef 512 (usearch), and faiss and int8
    do not by ef 512, the widest swept.

  So a shipped index would have to pick its graph shape by size, and somewhere below 1M
  **`[inferred]`**.
- **usearch's parallel build leaves rows unreachable at small sizes** (`--build-threads 1` is the
  control):

  | | 12-thread build | serial build |
  |---|---|---|
  | synthetic 10k | 0.3% unreachable, recall@10 0.995 at ef 512, 0.8 s | 0.0%, 1.000, 6.9 s |
  | synthetic 100k | 0.2%, 0.9995, 20 s | 0.1%, 0.998, 157 s |
  | real vault-a / vault-b / scripta | 1.1% / 0.4% / 2.4%, plateau 0.990 / 0.997 / 0.979 | 0.0% everywhere, ≥ 0.9995 by ef 64, 1.000 by ef 256 |

  The serial runs came last in the sweep, at higher swap (17.6–17.9 of 19.0 GB against 13.5–13.6 of
  15.0), so the build-time ratios (8×) are not like-for-like. faiss and hnswlib show 0.0–0.1% on the
  same real data. At 1M only 12-thread builds were run:
  there, M 16 leaves 1.5–2.9% unreachable in every library, faiss and hnswlib included. Whether a
  serial usearch build helps at 1M was not measured.
- **An HNSW index saves no memory**: on disk it is the vectors + 3.5–6.8%. Resident size could not
  be measured: process-RSS deltas were unusable under this swap pressure. int8 is the only reduction
  (−72%), at a recall@10 ceiling of 0.88 (1M) to 0.96 (10k), unless results are re-scored against
  float32. For int8, an "unreachable" row may also be a quantization miss; the two are not separated.
- **Deletes**: usearch `remove`, hnswlib `mark_deleted`. **faiss HNSW has none**:
  `remove_ids not implemented`, also through `IndexIDMap2` (checked).
- **hnswlib is 4.0–7.5× slower per query than faiss in the unfiltered sweep** at the same ef on
  synthetic data, and 2.7–10.4× on real data. Its distance kernels appear to have only SSE/AVX fast
  paths, so on Apple silicon it would run scalar code **`[inferred]`** from its source, not profiled.
  In the filtered runs the ratio varies widely: there the post-filter's large k and the per-query
  mask dominate **`[inferred]`**.
- **sqlite-vec scales linearly and is 8.3–9.8× slower than the cached brute force** at every size,
  unfiltered (810 ms against 90.6 ms at 1M). It now gets one untimed warm-up pass, and its build time
  no longer includes dropping the previous size's table.

## 4. Real vectors against the synthetic corpus

Recall@10 at ef 16 / 32 / 64, unfiltered and tie-aware: a row that scores at least the k-th true
score counts as a hit, because real scopes hold duplicate passages.

| | faiss | usearch, serial build | hnswlib |
|---|---|---|---|
| real: vault-a 3,521 · vault-b 1,912 · scripta 658 passages | 0.986–0.992 / 0.996–0.998 / 0.9995–1.000 | 0.987–0.996 / 0.995–1.000 / 0.9995–1.000 | 0.968–0.974 / 0.984–0.997 / 0.998–0.999 |
| synthetic 10k | 0.973 / 0.988 / 0.998 | 0.977 / 0.988 / 0.999 | 0.977 / 0.988 / 0.999 |

- faiss and serial-built usearch are at or above synthetic on real data at every ef.
- hnswlib is up to 0.010 below at ef 16 and 0.005 at ef 32, and within 0.001 by ef 64.

The real corpora are smaller than 10k, which likely favours them **`[inferred]`**. So this shows **no sign that the
synthetic corpus is easier than real data**, and no more than that. It cannot test the scale
behaviour itself, because no real scope exceeds 3.7k passages. Held-out chunks also stand in for
real query embeddings.

## Conditions

- M2 Max, 12 cores, 32 GB, macOS 27.0.1, on AC power. CPython 3.14.7 (the engine's pinned
  interpreter) and numpy 2.5.1 (Accelerate BLAS, the engine's locked version).
- **Memory pressure, recorded per row** (`swap` columns in RESULTS.md). Two Ollama runners held
  ~12 GB throughout, and swap was 13.5–17.9 of 15.0–19.0 GB used across all rows. The brute-force and
  sqlite-vec rows come from a second invocation of those two benches after the full `run_all.sh`
  pass (which runs everything once), at 15.5–16.3 of 17.0 GB. Corpora are
  memory-mapped and warmed before timing. Conditions, including the ef sweep, are interleaved per
  query, so drift spreads across them instead of landing on whichever is timed first. p95 is
  nearest-rank. Load average was ~4 from other sessions, so treat single-digit-percent
  differences as noise.
- **The synthetic corpus is calibrated to vault-a, not uniform on the sphere** (`common.synth`,
  `results/calibration.txt`). It was tuned against all 7,206 vault-a chunk vectors:
  - random-pair cosine 0.36 vs 0.35;
  - 1st and 10th-neighbour cosine 0.87 / 0.70 vs 0.89 / 0.71;
  - norm of the mean 0.60 vs 0.60;
  - 100th-neighbour cosine 0.49 vs 0.56, and participation ratio 107 vs 88, so its tail is harder
    than real.

  Against the 3,721 passage chunks the arm actually searches, the synthetic set's first neighbour is
  tighter (0.87 vs 0.80) and its tail looser (100th neighbour 0.47 vs 0.53).
- **Filters have substrate's shape**:
  - `default` = status IN (active, complete), class ≠ conversation and one vault: 25.5–31.1% of
    rows, depending on size (30.2% at 1M).
  - `entity` adds a document-level entity: 0.56–0.80%.
  - `rare` adds a rarer entity: 40–300 rows at 100k, 500k and 1M, and zero at 7.2k, 10k and 250k.
  - `few` passes exactly 5 rows, fewer than k.

  Vault is assigned per cluster, so a vault filter removes whole regions of the space.
- **HNSW, the same shape in every library**: M = 16 and ef_construction = 128, plus M = 32 and
  ef_construction = 256 at 1M. Builds use 12 threads, except the `_t1` rows (`--build-threads 1`).
  Queries are single, sequential and single-threaded, 200 per configuration (30 for sqlite-vec at
  ≥ 500k, 73 on scripta). Filtered runs are at ef = 128 and at k = 10 and k = 30 (the arm asks for k × 3). The
  planner's ANN path searches at ef = max(128, k / selectivity × 2), and its baseline is measured at
  that same ef.
- **Recall is tie-aware**: a returned row that passes the filter and scores at least the k-th true
  score counts as a hit. The synthetic corpora have no duplicate rows (checked at 10k and 100k), so
  this changes nothing there.
- **Real data** is scratch copies of `vault-a`, `vault-b` and `scripta`, opened `mode=ro`, passage chunks
  only. Queries are up to 200 held-out passage chunks: stand-ins for query embeddings, not real
  ones. The real-scope indexes are never written to disk.
- **Stated, not recorded in `results/raw/`**: the machine, OS, power and package versions; Ollama's
  ~12 GB; the load average; the two "(checked)" results (no duplicate synthetic rows; faiss
  `remove_ids` failing); and the data size. Each was observed once at the shell.
- **Nothing in `substrate/` changed.** Nothing was written to `substrate/out-vault/` or `~/.substrate`.

## Reproduce

Run this from the main checkout's `experiments/vector-scale`. `out-vault/` is git-ignored and exists
only there.

```bash
SRC=$HOME/CodeHome/CallTranscriber/substrate/out-vault
uv venv --python 3.14.7 --managed-python .venv
uv pip install --python .venv/bin/python numpy==2.5.1 usearch==2.26.6 faiss-cpu==1.15.1 hnswlib==0.8.0 sqlite-vec==0.1.9 psutil==7.2.2
mkdir -p data/real && for s in vault-a vault-b scripta; do cp "$SRC/$s.db" "$SRC/$s.db-wal" data/real/ 2>/dev/null || cp "$SRC/$s.db" data/real/; done
for s in vault-a vault-b scripta; do .venv/bin/python -c "import sqlite3; print('$s', sqlite3.connect('file:data/real/$s.db?mode=ro', uri=True).execute('pragma quick_check').fetchone()[0])"; done
./run_all.sh
```

- `cp` only reads `out-vault/`. The `-wal` is copied alongside so un-checkpointed writes come
  with it, and `quick_check` refuses a torn copy.
- **`data/` holds private content.** `data/real/` holds copies of the user's notes and call
  transcripts. Delete `data/` when done. It is git-ignored, and after a full run it is ~25 GB.
- The corpus and ground-truth caches are keyed on the generator constants and attribute
  probabilities (`common.params_key`), not on the filter code. After editing `mask_fns` or
  `attrs_for`, delete `data/gt_*.v2.npz`. `run_all.sh` would catch a stale cache anyway: brute force
  asserts recall 1.000 against it at every size.
- The wheels are pinned by version, not by hash, so provenance is unchecked. faiss-cpu's wheels are
  built outside Meta's own release, and hnswlib compiles from an sdist, which runs its build code.

## Libraries `[stated]`

| library | version | licence | maintenance (PyPI, 2026-11-06) | cp314 macOS arm64 wheel | filtered search from Python | delete |
|---|---|---|---|---|---|---|
| usearch | 2.26.6 | Apache-2.0 (+ numkong, Apache-2.0) | 4 releases in October 2026 alone | yes, `cp314` (re-wheel per Python minor) | **no**: neither `Index.search` nor the compiled module takes a filter or predicate | `remove` |
| faiss-cpu | 1.15.1 | MIT (bundles LLVM libomp, Apache-2.0 + LLVM exception) | 4 releases Mar–Sep 2026 | yes, `cp310-abi3`, needs macOS 14 | yes: `IDSelector` inside the graph walk | **none for HNSW** (`remove_ids not implemented`, also through `IndexIDMap2`; checked) |
| hnswlib | 0.8.0 | Apache-2.0 | **last release Dec 2023** | **no wheel at all**: built from sdist | yes, a Python callable per visited node | `mark_deleted` |
| sqlite-vec | 0.1.9 | MIT / Apache-2.0 | 0.1.9 Mar 2026. ANN (DiskANN, rescore) only in undocumented 0.1.10 alphas | `py3-none` loadable `vec0.dylib` | yes, SQL `WHERE` on metadata columns and `rowid IN (…)`. **Exact only** | SQL `DELETE` |

All four are GPL-3.0-or-later compatible.

## Cost to ship it in the signed app `[stated]`

What each library would add to `substrate-engine/` inside Scripta.app. The rules checked are
`tools/build-bundled-engine`'s:
- every Mach-O signed with the identity. Only the interpreter carries an entitlement, exactly
  `allow-jit`; the build refuses any other binary that has one;
- no load path outside `/System`, `/usr/lib` or the bundle;
- no GPL media libraries.

| | usearch | faiss-cpu | hnswlib | sqlite-vec |
|---|---|---|---|---|
| Mach-O files added | 2 (`compiled.so`, numkong's `.so`) | 3 (`_swigfaiss.so`, `libfaiss.dylib`, `.dylibs/libomp.dylib`) + a `.metallib` | 1 | 1 (`vec0.dylib`) |
| size | 3.4 MB | 17 MB | 0.6 MB | 0.2 MB |
| links | libc++, libSystem | Accelerate, Metal, MetalKit, MPS, Foundation; its own `libomp` via `@loader_path` | libc++, libSystem | libSystem |
| foreign-link check | passes | passes: libomp's own install name `/DLC/faiss/.dylibs/libomp.dylib` is skipped as its `-D` name | passes | passes (`dist/vec0.dylib` is its own install name) |
| signing | ad-hoc in the wheel; re-signed by the build like every other Mach-O | same | same | same |
| interpreter pin | untouched (packages live in `.venv/`, the pin covers `runtime/`). **A Python minor bump needs a matching `cpXY` wheel** | untouched; `abi3` survives a bump | rebuilt from source on every bump, which needs the compiler toolchain, as OpenCV already does | untouched; `py3-none` |
| in-process conflicts | none found | **aborts the engine.** Its libomp and torch's (`torch/lib/libomp.dylib`, already in the engine for docling) cannot share a process: `import faiss; import torch` exits 134 with `OMP: Error #15` (reproduced against the engine's own `.venv`). The workarounds are relinking faiss to torch's libomp, or `KMP_DUPLICATE_LIB_OK=TRUE`, which libomp itself calls unsafe | none found | none found |
| hardened runtime | nothing new | nothing new beyond the libomp conflict | nothing new | **extension loading must be switched on, then off**: `con.enable_load_extension(True); sqlite_vec.load(con); con.enable_load_extension(False)`. CPython 3.14.7 from python-build-standalone supports it (checked). SQLite 3.53.1 marks `load_extension()` `SQLITE_DIRECTONLY`, so a trigger in a planted database cannot call it. A same-team-signed dylib passes library validation |
| licence file | `usearch`, `numkong` under `third_party_licenses/` | faiss **and** its `THIRD_PARTY_NOTICES` (LLVM libomp) | hnswlib | sqlite-vec |
| lock | `uv.lock` gains 2 packages | 1 (+ `packaging`) | 1 sdist | 1 |

For usearch, the work is one dependency line, two licence files, and confirming the build's signing
pass picks up the new `.so` files. It finds Mach-O files with `file`, not by suffix, so it should.
No entitlement, no `allow-*` key, no pin change.
