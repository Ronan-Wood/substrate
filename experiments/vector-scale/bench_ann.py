"""One ANN library at one corpus size: build, ef sweep, filtered search, disk and load.

usage: .venv/bin/python bench_ann.py LIB N [--m 16 --efc 128 --ef-max 512]
       .venv/bin/python bench_ann.py LIB --real SCOPE
       LIB is hnswlib | faiss | usearch | usearch_i8 | sqlite_vec
       appends one JSON line to results/raw/ann.jsonl (or real.jsonl with --real)

Every graph index is HNSW, by default M = 16 neighbours and ef_construction = 128, so the libraries
differ in implementation, not in parameters. Queries are single, sequential and single-threaded,
which is how substrate searches. Builds use every core.

FILTERS, three ways, at k = 10 and at k = 30 (what the vector arm actually requests):
  native   — the library's own filtered search: faiss IDSelector and hnswlib's filter callback walk
             the graph and only ADMIT allowed ids; sqlite-vec puts the filter in the SQL. usearch's
             Python binding has no filtered search, so it has no native row.
  post     — ask for min(k / selectivity x 2, POST_CAP) neighbours, drop disallowed ones, keep k.
             What you get from any ANN library that cannot filter. Can return fewer than k.
  planned  — the plan that cannot return short BY CONSTRUCTION: exact over the subset when at most
             EXACT_ROWS rows pass, otherwise `post`, and exact over the mask whenever `post` comes back
             short. What is measured is its recall, its latency, and how often each path runs.

SELF-RECALL: after the build, a sample of corpus rows is searched for itself at the widest ef. A row
that cannot find itself is unreachable in the graph, which caps recall at any ef.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import sqlite3
import time

import numpy as np

import common as c

EXACT_ROWS = 10_000  # planned: at most this many allowed rows → exact over the subset (bench_brute subset_curve)
POST_CAP = 4_096     # post: never ask an index for more than this many neighbours
SELF_SAMPLE = 1_000
BUILD_THREADS = c.threads()  # --build-threads; 1 reproduces the serial usearch build


# ------------------------------------------------------------------ adapters
#
# Each adapter: build(x), search(q, k, ef) -> int64 labels (-1 padded), prepare(mask, fname) -> ctx,
# native(q, k, ef, ctx) -> labels (or native = None), save(path), load(path), drop().

class Hnswlib:
    def __init__(self, m, efc, attrs=None):
        self.m, self.efc = m, efc
        self.raised = 0  # knn_query raises, returning NOTHING, whenever it finds fewer than k

    def build(self, x):
        import hnswlib
        self.ix = hnswlib.Index(space="ip", dim=c.DIM)
        self.ix.init_index(max_elements=len(x), M=self.m, ef_construction=self.efc, random_seed=1)
        self.ix.add_items(x, np.arange(len(x)), num_threads=BUILD_THREADS)

    def _query(self, q, k, ef, flt=None):
        self.ix.set_ef(max(ef, k))
        try:
            lab, _ = self.ix.knn_query(q, k=k, num_threads=1, filter=flt)
        except RuntimeError as e:
            if "contiguous 2D array" not in str(e):  # only "fewer than k found" is an expected outcome
                raise
            self.raised += 1  # counted and reported: the API gives the caller nothing back, not a partial answer
            return np.zeros(0, dtype=np.int64)
        return lab[0].astype(np.int64)

    def search(self, q, k, ef):
        return self._query(q, k, ef)

    def prepare(self, mask, fname):
        # bytes indexing is a C-level lookup returning 0/1, and `tobytes` is one copy: the cheapest
        # Python callable hnswlib's filter can take. (A per-query `tolist()` would cost more than the search.)
        return mask.tobytes().__getitem__

    def native(self, q, k, ef, ctx):
        return self._query(q, k, ef, ctx)

    def save(self, path):
        self.ix.save_index(str(path))

    def load(self, path):
        import hnswlib
        self.ix = hnswlib.Index(space="ip", dim=c.DIM)
        self.ix.load_index(str(path))

    def drop(self):
        del self.ix


class Faiss:
    def __init__(self, m, efc, attrs=None):
        self.m, self.efc = m, efc

    def build(self, x):
        import faiss
        faiss.omp_set_num_threads(BUILD_THREADS)
        self.ix = faiss.IndexHNSWFlat(c.DIM, self.m, faiss.METRIC_INNER_PRODUCT)
        self.ix.hnsw.efConstruction = self.efc
        self.ix.add(np.ascontiguousarray(x))
        faiss.omp_set_num_threads(1)

    def _query(self, q, k, ef, sel=None):
        import faiss
        p = faiss.SearchParametersHNSW(efSearch=max(ef, k), sel=sel)
        _, lab = self.ix.search(q[None, :], k, params=p)
        return lab[0]

    def search(self, q, k, ef):
        return self._query(q, k, ef)

    def prepare(self, mask, fname):
        import faiss
        bits = np.packbits(mask, bitorder="little")
        return (faiss.IDSelectorBitmap(len(mask), faiss.swig_ptr(bits)), bits)  # bits kept alive

    def native(self, q, k, ef, ctx):
        return self._query(q, k, ef, ctx[0])

    def save(self, path):
        import faiss
        faiss.write_index(self.ix, str(path))

    def load(self, path):
        import faiss
        self.ix = faiss.read_index(str(path))

    def drop(self):
        del self.ix


class Usearch:
    dtype = "f32"
    native = None

    def __init__(self, m, efc, attrs=None):
        self.m, self.efc = m, efc

    def build(self, x):
        from usearch.index import Index
        self.ix = Index(ndim=c.DIM, metric="ip", dtype=self.dtype, connectivity=self.m,
                        expansion_add=self.efc, expansion_search=64)
        self.ix.add(np.arange(len(x), dtype=np.uint64), np.ascontiguousarray(x), threads=BUILD_THREADS)

    def search(self, q, k, ef):
        self.ix.expansion_search = max(ef, k)
        return np.asarray(self.ix.search(q, k, threads=1).keys, dtype=np.int64)

    def prepare(self, mask, fname):
        return None

    def save(self, path):
        self.ix.save(str(path))

    def load(self, path):
        from usearch.index import Index
        self.ix = Index.restore(str(path), view=True)  # memory-mapped, no copy

    def drop(self):
        del self.ix


class UsearchI8(Usearch):
    dtype = "i8"


class SqliteVec:
    """Exact search inside SQLite: not ANN, the in-database comparator. ef is ignored."""

    WHERE = "AND status <= 1 AND conv = 0 AND vault = 0"  # the synthetic `default` filter, in SQL

    def __init__(self, m, efc, attrs):
        self.attrs = attrs
        self.path = c.DATA / "sqlite_vec.db"

    def reset(self):
        """Open the database and clear the previous size's tables: NOT part of the build time."""
        import sqlite_vec
        self.db = sqlite3.connect(self.path)
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)  # load_extension() is not reachable from SQL afterwards
        for stmt in ("DROP TABLE IF EXISTS v", "DROP TABLE IF EXISTS ent", "VACUUM"):
            self.db.execute(stmt)

    def build(self, x):
        for stmt in (f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{c.DIM}], "
                     "status integer, conv integer, vault integer)",
                     "CREATE TABLE ent(r INTEGER PRIMARY KEY)"):
            self.db.execute(stmt)
        a = self.attrs
        with self.db:
            self.db.executemany(
                "INSERT INTO v(rowid, embedding, status, conv, vault) VALUES (?,?,?,?,?)",
                ((i, np.asarray(x[i]).tobytes(), int(a.status[i]), int(a.conv[i]), int(a.vault[i]))
                 for i in range(len(x))))

    def search(self, q, k, ef):
        rows = self.db.execute("SELECT rowid FROM v WHERE embedding MATCH ? AND k = ?",
                               (q.tobytes(), k)).fetchall()
        return np.array([r[0] for r in rows], dtype=np.int64)

    def prepare(self, mask, fname):
        """The SQL for this filter. Entity-shaped filters go through `rowid IN (SELECT r FROM ent)`."""
        if fname == "default":
            return self.WHERE
        rows = {"entity": self.attrs.ent_common, "rare": self.attrs.ent_rare}.get(fname)
        rows = np.flatnonzero(rows) if rows is not None else np.flatnonzero(mask)  # `few`: the rows themselves
        with self.db:
            self.db.execute("DELETE FROM ent")
            self.db.executemany("INSERT INTO ent(r) VALUES (?)", ((int(r),) for r in rows))
        return self.WHERE + " AND rowid IN (SELECT r FROM ent)"

    def native(self, q, k, ef, ctx):
        rows = self.db.execute(f"SELECT rowid FROM v WHERE embedding MATCH ? AND k = ? {ctx}",
                               (q.tobytes(), k)).fetchall()
        return np.array([r[0] for r in rows], dtype=np.int64)

    def save(self, path):
        self.db.commit()

    def drop(self):
        self.db.close()


LIBS = {"hnswlib": Hnswlib, "faiss": Faiss, "usearch": Usearch, "usearch_i8": UsearchI8,
        "sqlite_vec": SqliteVec}


# ------------------------------------------------------------------ strategies

def post_filter(lib, q, build, ef, k):
    """Build the filter's mask (the per-query cost), ask for k / selectivity x 2 neighbours, drop the rest."""
    mask = build()
    sel = max(float(np.count_nonzero(mask)) / len(mask), 1e-9)
    kk = int(min(len(mask), POST_CAP, math.ceil(k / sel * 2)))
    lab = lib.search(q, kk, max(ef, kk))
    lab = lab[lab >= 0]
    return lab[mask[lab]][:k]


def planned(lib, x, q, build, ef, k, paths, path_log):
    """Build the mask, then: exact over the subset when few rows pass; else post-filter; exact on short."""
    mask = build()
    idx = np.flatnonzero(mask)
    if len(idx) <= EXACT_ROWS:
        paths["exact"] += 1
        path_log.append("exact")
        return idx[c.exact_topk(x[idx], q, k)] if len(idx) else idx
    sel = len(idx) / len(mask)
    kk = int(min(len(mask), POST_CAP, math.ceil(k / sel * 2)))
    paths["ef_used"] = max(ef, kk)
    lab = lib.search(q, kk, max(ef, kk))
    lab = lab[lab >= 0]
    lab = lab[mask[lab]][:k]
    if len(lab) < k:
        paths["fallback"] += 1
        path_log.append("fallback")
        return c.exact_topk(x, q, k, mask=mask)
    paths["ann"] += 1
    path_log.append("ann")
    return lab


def self_miss(lib, x, ef):
    """Share of sampled rows whose top-1 is neither the row itself nor an identical vector.

    An identical twin counts as found: real scopes hold duplicate passages, and either copy is a
    correct top-1. The synthetic corpora have no duplicates, so there the test is exact. For an int8
    index a miss is unreachability OR quantization ranking another row above the row itself; the two
    are not separated.
    """
    rows = np.random.default_rng(c.SEED).choice(len(x), min(SELF_SAMPLE, len(x)), replace=False)
    found = []
    for i in rows:
        r = lib.search(np.asarray(x[i]), 1, ef)
        found.append(len(r) > 0 and r[0] >= 0 and (r[0] == i or np.array_equal(x[r[0]], x[i])))
    return 1.0 - float(np.mean(found))


def main() -> None:
    global BUILD_THREADS
    ap = argparse.ArgumentParser()
    ap.add_argument("lib", choices=LIBS)
    ap.add_argument("n", type=int, nargs="?", default=0)
    ap.add_argument("--real", help="scope name: scratch copy under data/real, queries held out")
    ap.add_argument("--ef", type=int, default=128, help="ef for the filtered runs")
    ap.add_argument("--m", type=int, default=16, help="graph degree")
    ap.add_argument("--efc", type=int, default=128, help="ef_construction")
    ap.add_argument("--ef-max", type=int, default=512, help="largest ef in the sweep")
    ap.add_argument("--build-threads", type=int, default=c.threads(), help="threads for the build (default: all)")
    a = ap.parse_args()
    BUILD_THREADS = a.build_threads
    sweep = [e for e in (16, 32, 64, 128, 256, 512, 1024, 2048) if e <= a.ef_max]

    if a.real:
        assert a.lib != "sqlite_vec", "sqlite_vec is exact; its filters need the synthetic attributes"
        x, q, ms, q_passes = c.real_corpus(a.real)
        gt = {k: c.ground_truth(x, q, ms, k) for k in (c.K, c.K_ARM)}
        attrs = None
        # Real masks are precomputed: at ≤ 3.5k rows building one costs microseconds (not timed).
        builders = {f: (lambda m: lambda: m)(m) for f, m in ms.items() if f != "none"}
    else:
        x, q, attrs, ms, gt = c.setup(a.n)
        q_passes = None
        builders = c.mask_fns(attrs)
    n = len(x)
    label = a.lib if (a.m, a.efc) == (16, 128) else f"{a.lib}_m{a.m}_efc{a.efc}"
    if a.build_threads != c.threads():
        label += f"_t{a.build_threads}"
    nq = len(q) if a.lib != "sqlite_vec" or n <= 100_000 else 30
    qs = q[:nq]
    out = {"lib": label, "n": n, "real": a.real, "M": a.m, "ef_construction": a.efc,
           "build_threads": a.build_threads, "k": c.K, "k_arm": c.K_ARM, "queries": nq,
           "ef_filtered": a.ef, "exact_rows": EXACT_ROWS, "post_cap": POST_CAP,
           "conditions_start": c.memory_conditions()}

    lib = LIBS[a.lib](a.m, a.efc, attrs)
    if hasattr(lib, "reset"):
        lib.reset()
    gc.collect()
    rss0 = c.rss()
    t = time.perf_counter()
    lib.build(x)
    out["build_s"] = time.perf_counter() - t
    out["index_rss_mb"] = (c.rss() - rss0) / 2**20
    if a.lib == "sqlite_vec":  # an on-disk table: one untimed pass so no timed run is the cold first read
        for qv in qs[: min(10, nq)]:
            lib.search(qv, c.K, 0)

    def sc(res, truth, m, k, rows=None):
        qq = qs if rows is None else qs[rows]
        return c.score(res, truth, m, k, x=x, qs=qq)

    out["sweep"] = []
    efs = [0] if a.lib == "sqlite_vec" else sweep
    timed = c.timed_interleaved({ef: (lambda e: lambda qv: lib.search(qv, c.K, e))(ef) for ef in efs}, qs)
    for ef in efs:
        res, med, p95 = timed[ef]
        out["sweep"].append({"ef": ef, "ms": med, "p95": p95,
                             "recall": sc(res, gt[c.K]["none"][:nq], ms["none"], c.K)["recall"]})
    if a.lib != "sqlite_vec":
        res, med, _ = c.timed_each(lambda qv: lib.search(qv, c.K_ARM, a.ef), qs)
        out["unfiltered_at_k_arm"] = {"ef": a.ef, "ms": med,
                                      "recall": sc(res, gt[c.K_ARM]["none"][:nq], ms["none"], c.K_ARM)["recall"]}
        out["self_miss"] = self_miss(lib, x, sweep[-1])
    out["sweep_raised"] = getattr(lib, "raised", 0)  # must be 0: an unfiltered search never finds < k

    out["filters"] = {}
    for fname in [f for f in ms if f != "none"]:
        m, build = ms[fname], builders[fname]
        row = {"allowed": int(m.sum())}
        static_ctx = lib.prepare(m, fname) if a.lib == "sqlite_vec" else None  # SQL; entity rows live in the db
        for k in (c.K, c.K_ARM):
            truth = gt[k][fname][:nq]
            raised = {}

            def counting(name, fn):
                raised[name] = 0

                def g(qv):
                    before = getattr(lib, "raised", 0)
                    r = fn(qv)
                    raised[name] += getattr(lib, "raised", 0) - before
                    return r
                return g

            fns = {}
            if lib.native is not None:
                if static_ctx is not None:
                    fns["native"] = (lambda kk: lambda qv: lib.native(qv, kk, a.ef, static_ctx))(k)
                else:  # the filter's bitmap / callback is built per query, as the filter arrives with it
                    fns["native"] = counting("native", (lambda kk: lambda qv: lib.native(
                        qv, kk, a.ef, lib.prepare(build(), fname)))(k))
            paths, path_log = {"exact": 0, "ann": 0, "fallback": 0}, []
            if a.lib != "sqlite_vec":
                fns["post"] = counting("post", (lambda kk: lambda qv: post_filter(lib, qv, build, a.ef, kk))(k))
                fns["planned"] = counting("planned", (lambda kk: lambda qv: planned(
                    lib, x, qv, build, a.ef, kk, paths, path_log))(k))
            timed = c.timed_interleaved(fns, qs)
            for how, (res, med, p95) in timed.items():
                cell = {"ms": med, "p95": p95, "raised": raised.get(how, 0), **sc(res, truth, m, k)}
                if how == "planned":
                    cell.update(paths)
                    ann_rows = np.array([p == "ann" for p in path_log])
                    if ann_rows.any():  # recall on the ANN path alone, without the exact paths' 1.0
                        cell["recall_ann_path"] = sc([r for r, b in zip(res, ann_rows) if b],
                                                     [t for t, b in zip(truth, ann_rows) if b], m, k,
                                                     rows=np.flatnonzero(ann_rows))["recall"]
                    if "ef_used" in paths:  # the like-for-like baseline: unfiltered, same k, same ef
                        ub, _, _ = c.timed_each(lambda qv: lib.search(qv, k, paths["ef_used"]), qs)
                        cell["unfiltered_same_ef"] = sc(ub, gt[k]["none"][:nq], ms["none"], k)["recall"]
                if q_passes is not None:  # split short answers by whether the query's own row passes
                    qp = q_passes[fname][:nq]
                    cell["short_query_passes"] = c.score([r for r, p in zip(res, qp) if p],
                                                         [t for t, p in zip(truth, qp) if p], m, k)["short"]
                    cell["queries_passing"] = int(qp.sum())
                row[f"{how}@{k}"] = cell
        out["filters"][fname] = row

    if not a.real:  # real-scope indexes hold the user's embeddings: never written to disk
        path = c.DATA / f"idx_{a.lib}.bin"
        lib.save(path)
        out["disk_mb"] = (lib.path if a.lib == "sqlite_vec" else path).stat().st_size / 2**20
        if hasattr(lib, "load"):
            before = [lib.search(qv, c.K, 64) for qv in q[:10]]
            lib.drop()
            gc.collect()
            t = time.perf_counter()
            lib.load(path)  # just written, so this is a WARM-cache load: deserialization, not a cold disk read
            out["load_s"] = time.perf_counter() - t
            out["load_is_mmap"] = a.lib.startswith("usearch")  # a view: no reads at load time
            after = [lib.search(qv, c.K, 64) for qv in q[:10]]
            assert all(np.array_equal(b, f) for b, f in zip(before, after)), "reloaded index answers differently"
            if out["load_is_mmap"]:  # what the view costs to query (its first touches fault pages in)
                _, out["view_query_ms"], out["view_query_p95"] = c.timed_each(lambda qv: lib.search(qv, c.K, 128), qs[:50])
    out["peak_rss_mb"] = c.peak_rss() / 2**20
    out["conditions_end"] = c.memory_conditions()

    (c.HERE / "results" / "raw").mkdir(parents=True, exist_ok=True)
    with open(c.HERE / "results" / "raw" / ("real.jsonl" if a.real else "ann.jsonl"), "a") as f:
        f.write(json.dumps(out) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
