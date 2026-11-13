"""Shared pieces of the vector-scale experiment: synthetic corpus, filters, exact search, metrics.

The synthetic corpus is calibrated against the real vault-a index (qwen3-embedding:0.6b, 1024-d), not
uniform on the sphere. Uniform random vectors are the worst case for every graph index and say
little about embeddings, which are anisotropic (a large shared mean direction) and low in intrinsic
dimension. `calibrate.py` prints the real statistics next to the synthetic ones.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import resource
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DIM = 1024
K = 10           # recall@K is the headline metric
K_ARM = 30       # what substrate's vector arm actually asks for: k * 3 at k = 10 (retriever.py:488)
SEED = 20261106

# Tuned by calibrate.py against vault-a's 7,206 vectors. It matches random-pair cosine, 1st- and
# 10th-neighbour cosine and the mean's norm closely; the 100th-neighbour cosine is ~0.08 low and the
# participation ratio ~20% high, so the synthetic tail is a little harder than the real one.
SPECTRUM_ALPHA = 0.8    # eigenvalue i ∝ (i+1)^-alpha
MEAN_WEIGHT = 0.75      # weight of the shared mean direction before normalization
CLUSTER_SPREAD = 0.55   # within-cluster noise relative to the between-cluster spread
CHUNKS_PER_CLUSTER = 10  # one cluster ≈ one note's worth of chunks plus its close neighbours
CHUNKS_PER_DOC = 20
# Document attribute probabilities (attrs_for): vault, status, conversation, entities.
P_VAULT, P_STATUS, P_CONV, P_ENT_COMMON, P_ENT_RARE = (0.5, 0.3, 0.2), (0.6, 0.15, 0.15, 0.1), 0.2, 0.02, 0.001
FEW_ROWS = 5     # the `few` filter passes exactly this many rows: fewer than k, so partial truth is exercised


def params_key() -> str:
    """Everything the cached corpus and ground truth depend on. A cache built under other values is refused."""
    p = [DIM, K, K_ARM, SEED, SPECTRUM_ALPHA, MEAN_WEIGHT, CLUSTER_SPREAD, CHUNKS_PER_CLUSTER,
         CHUNKS_PER_DOC, P_VAULT, P_STATUS, P_CONV, P_ENT_COMMON, P_ENT_RARE, FEW_ROWS]
    return hashlib.sha256(json.dumps(p).encode()).hexdigest()[:12]


def _basis(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    scale = (np.arange(DIM) + 1.0) ** (-SPECTRUM_ALPHA / 2)
    rot, _ = np.linalg.qr(rng.standard_normal((DIM, DIM)))
    mean = rng.standard_normal(DIM)
    mean /= np.linalg.norm(mean)
    return scale.astype(np.float32), rot.astype(np.float32), mean.astype(np.float32)


def synth(n: int, n_queries: int = 200, *, seed: int = SEED) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (corpus[n, DIM], queries[n_queries, DIM], cluster_of_each_corpus_row)."""
    rng = np.random.default_rng(seed)
    scale, rot, mean = _basis(rng)
    n_clusters = max(1, n // CHUNKS_PER_CLUSTER)
    centers = rng.standard_normal((n_clusters, DIM), dtype=np.float32) * scale

    def draw(count: int, r: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
        cl = r.integers(0, n_clusters, count)
        out = np.empty((count, DIM), dtype=np.float32)
        step = 65536
        for s in range(0, count, step):
            e = min(count, s + step)
            z = centers[cl[s:e]] + CLUSTER_SPREAD * r.standard_normal((e - s, DIM), dtype=np.float32) * scale
            z = z @ rot
            z /= np.linalg.norm(z, axis=1, keepdims=True)
            z += MEAN_WEIGHT * mean
            z /= np.linalg.norm(z, axis=1, keepdims=True)
            out[s:e] = z
        return out, cl

    corpus, clusters = draw(n, rng)
    queries, _ = draw(n_queries, np.random.default_rng(seed + 1))
    return corpus, queries, clusters


def _check_key(z, path: Path) -> None:
    got = str(z["params"]) if "params" in z.files else "none"
    if got != params_key():
        raise SystemExit(f"{path} was built under parameters {got}, not {params_key()}: delete it to rebuild")


def load_or_make(n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The corpus MEMORY-MAPPED, queries and clusters in memory.

    Mapped so the corpus is file-backed pages the kernel can drop and re-read, not a second
    anonymous 4 GB copy beside the index under test: this Mac runs with Ollama holding ~12 GB and
    swap nearly full, and an anonymous copy was being swapped out mid-measurement (1M brute force
    p95 was 2x its median). Every reader warms it with `warm()` before timing.
    """
    xp, mp = DATA / f"synth_{n}.x.npy", DATA / f"synth_{n}.meta.npz"
    if not (xp.exists() and mp.exists()):
        DATA.mkdir(exist_ok=True)
        x, q, cl = synth(n)
        np.save(xp, x)
        np.savez(mp, q=q, cl=cl, params=params_key())
        del x
    z = np.load(mp)
    _check_key(z, mp)
    return np.load(xp, mmap_mode="r"), z["q"], z["cl"]


def warm(x: np.ndarray) -> None:
    """Fault every page of a mapped corpus in, so the first timed query is not a disk read."""
    x @ np.zeros(x.shape[1], dtype=np.float32)


# ------------------------------------------------------------------ filters
#
# The shape of substrate's vector-arm WHERE clause, reduced to what changes selectivity:
#   status IN (active, complete)  AND  document_class NOT IN (conversation)  AND  vault IN (...)
#   [AND doc_id IN (SELECT doc_id FROM document_entities WHERE entity_id = ?)]
# Vault is assigned per CLUSTER, so a vault filter removes whole regions of the space. That is the
# realistic case (a vault is a topic) and the hard one for in-graph filtering: about half the queries
# land in a region where nothing is allowed.

@dataclass(frozen=True)
class Attrs:
    status: np.ndarray   # 0 active, 1 complete, 2 archived, 3 superseded
    conv: np.ndarray     # bool: document_class == conversation
    vault: np.ndarray    # 0..2
    ent_common: np.ndarray  # bool: chunk's doc mentions a common entity (~2% of docs)
    ent_rare: np.ndarray    # bool: chunk's doc mentions a rare entity (~0.1% of docs)


def attrs_for(n: int, clusters: np.ndarray, *, seed: int = SEED) -> Attrs:
    rng = np.random.default_rng(seed + 7)
    n_clusters = int(clusters.max()) + 1
    vault_of_cluster = rng.choice(3, n_clusters, p=list(P_VAULT))
    # Docs are runs of chunks inside a cluster, so status/class/entity are per document.
    order = np.argsort(clusters, kind="stable")
    doc = np.empty(n, dtype=np.int64)
    doc[order] = np.arange(n) // CHUNKS_PER_DOC
    n_docs = int(doc.max()) + 1
    d_status = rng.choice(4, n_docs, p=list(P_STATUS))
    d_conv = rng.random(n_docs) < P_CONV
    d_ent_c = rng.random(n_docs) < P_ENT_COMMON
    d_ent_r = rng.random(n_docs) < P_ENT_RARE
    # Vault is document-level provenance: a doc takes the vault of the cluster its first chunk is in.
    d_vault = vault_of_cluster[clusters[order[::CHUNKS_PER_DOC]]]
    return Attrs(d_status[doc], d_conv[doc], d_vault[doc], d_ent_c[doc], d_ent_r[doc])


def mask_fns(a: Attrs) -> dict:
    """Each filter as a function that BUILDS its mask from cached per-row attribute arrays.

    A planner in front of a cached matrix pays this on every query, because the filter arrives with
    the query; bench_ann times it inside every filtered strategy. `few` is a fixed set of rows, so its
    builder ANDs the default with a precomputed flag, which costs what an entity filter would.
    """
    def default():
        return (a.status <= 1) & ~a.conv & (a.vault == 0)

    d = default()
    few_flag = np.zeros(len(d), dtype=bool)
    few_flag[np.flatnonzero(d)[:: max(1, int(d.sum()) // FEW_ROWS)][:FEW_ROWS]] = True
    return {
        "default": default,                               # ~29-30%
        "entity": lambda: default() & a.ent_common,       # ~0.6%
        "rare": lambda: default() & a.ent_rare,           # ~0.03%; zero rows at 10k
        "few": lambda: default() & few_flag,              # exactly FEW_ROWS rows, fewer than k
    }


def masks(a: Attrs) -> dict[str, np.ndarray]:
    return {"none": np.ones(len(a.status), dtype=bool), **{k: fn() for k, fn in mask_fns(a).items()}}


# ------------------------------------------------------------------ exact search

def _select(sims: np.ndarray, k: int) -> np.ndarray:
    """Top-k of a score vector best-first, dropping -inf (masked) entries."""
    kk = min(k, len(sims))
    part = np.argpartition(-sims, kk - 1)[:kk]
    part = part[np.argsort(-sims[part])]
    return part[np.isfinite(sims[part])]


def exact_topk(x: np.ndarray, q: np.ndarray, k: int = K, mask: np.ndarray | None = None) -> np.ndarray:
    """Brute force the way a cached-matrix vector arm would: one matvec, mask, argpartition.

    Returns row indices best-first; shorter than k only when fewer than k rows pass the mask.
    """
    sims = x @ q
    if mask is not None:
        sims[~mask] = -np.inf
    return _select(sims, k)


def ground_truth(x: np.ndarray, qs: np.ndarray, ms: dict[str, np.ndarray], k: int) -> dict[str, list[np.ndarray]]:
    sims = qs @ x.T
    out: dict[str, list[np.ndarray]] = {}
    for name, m in ms.items():
        rows = []
        for i in range(len(qs)):
            s = sims[i].copy()
            s[~m] = -np.inf
            rows.append(_select(s, k))
        out[name] = rows
    return out


def score(results: list[np.ndarray], truth: list[np.ndarray], mask: np.ndarray, k: int,
          x: np.ndarray | None = None, qs: np.ndarray | None = None) -> dict:
    """recall@k, plus the two correctness counts substrate cares about.

    short      — queries that returned fewer than min(k, rows passing the filter)
    violations — returned rows that fail the filter (a filter that did not hold)

    TIE-AWARE when x and qs are given: a returned row that passes the filter and scores at least the
    k-th true score (within 1e-6) is a hit even if the ground truth picked its tied twin. Real scopes
    hold duplicate passages; without this, every library is charged for the order of exact ties.
    """
    allowed = int(mask.sum())
    rec, short, viol = [], 0, 0
    for i, (r, t) in enumerate(zip(results, truth)):
        r = np.asarray(r, dtype=np.int64)
        r = r[r >= 0]
        viol += int((~mask[r]).sum())
        r = np.unique(r[mask[r]])  # a leaked or repeated row must not hide a short answer
        if len(r) < min(k, allowed):
            short += 1
        if not len(t):
            rec.append(1.0)
            continue
        hits = len(set(r.tolist()) & set(t.tolist()))
        if x is not None and qs is not None and hits < len(t) and len(r):
            kth = float(np.asarray(x[t[-1]]) @ qs[i])
            ok = r[mask[r]]
            hits = int(((np.asarray(x[ok]) @ qs[i]) >= kth - 1e-6).sum())
        rec.append(min(hits, len(t)) / len(t))
    return {"recall": float(np.mean(rec)), "short": short, "violations": viol, "allowed": allowed}


# ------------------------------------------------------------------ measurement helpers

def rss() -> int:
    import psutil
    return psutil.Process().memory_info().rss


def peak_rss() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS


def pctl(ts: list[float]) -> tuple[float, float]:
    """Median and nearest-rank p95 of a list of millisecond timings."""
    ts = sorted(ts)
    return ts[len(ts) // 2], ts[max(0, math.ceil(0.95 * len(ts)) - 1)]


def timed_each(fn, items) -> tuple[list, float, float]:
    """Call fn on every item; return the results and the median and p95 wall time in ms."""
    res, ts = [], []
    for it in items:
        t = time.perf_counter()
        res.append(fn(it))
        ts.append((time.perf_counter() - t) * 1e3)
    return (res, *pctl(ts))


def timed_interleaved(fns: dict, items, keep: bool = True) -> dict:
    """Time several functions over the same items, INTERLEAVED: each item runs every function, in an
    order rotated per item, so drift over the run (swap, thermal, other load) spreads evenly instead of
    landing on whichever condition happens to be measured first. Results are kept only when `keep`,
    because holding 200 full score vectors at 1M is 800 MB of the very memory pressure being measured.

    Returns {name: (results or None, median ms, p95 ms)}.
    """
    names = list(fns)
    ts = {n: [] for n in names}
    res = {n: [] for n in names}
    for j, it in enumerate(items):
        for n in names[j % len(names):] + names[: j % len(names)]:
            t = time.perf_counter()
            out = fns[n](it)
            ts[n].append((time.perf_counter() - t) * 1e3)
            if keep:
                res[n].append(out)
            del out
    return {n: ((res[n] if keep else None), *pctl(ts[n])) for n in names}


def threads() -> int:
    return os.cpu_count() or 1


def memory_conditions() -> dict:
    """Swap in use and the largest resident process, recorded with every result."""
    import psutil
    sw = psutil.swap_memory()
    vm = psutil.virtual_memory()
    return {"swap_used_gb": round(sw.used / 2**30, 1), "swap_total_gb": round(sw.total / 2**30, 1),
            "ram_available_gb": round(vm.available / 2**30, 1)}


def load_or_make_gt(n: int, x: np.ndarray, q: np.ndarray, ms: dict[str, np.ndarray]) -> dict[int, dict[str, list[np.ndarray]]]:
    """Exact filtered top-k at k = K and K_ARM for every query, cached beside the corpus (-1 padded)."""
    p = DATA / f"gt_{n}.v2.npz"
    if p.exists():
        z = np.load(p)
        _check_key(z, p)
        return {k: {name: [row[row >= 0] for row in z[f"{name}@{k}"]] for name in ms} for k in (K, K_ARM)}
    out, padded = {}, {"params": params_key()}
    for k in (K, K_ARM):
        gt = ground_truth(x, q, ms, k)
        out[k] = gt
        for name, rows in gt.items():
            a = np.full((len(rows), k), -1, dtype=np.int64)
            for i, r in enumerate(rows):
                a[i, : len(r)] = r
            padded[f"{name}@{k}"] = a
    np.savez(p, **padded)
    return out


def setup(n: int):
    """Corpus (mapped, warmed), queries, attributes, masks and ground truth for a synthetic size."""
    x, q, cl = load_or_make(n)
    warm(x)
    attrs = attrs_for(n, cl)
    ms = masks(attrs)
    return x, q, attrs, ms, load_or_make_gt(n, x, q, ms)


# ------------------------------------------------------------------ real scopes

def _ro_connect(name: str) -> sqlite3.Connection:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise SystemExit(f"not a scope name: {name!r}")
    return sqlite3.connect((DATA / "real" / f"{name}.db").as_uri() + "?mode=ro", uri=True)


def real_vectors(name: str) -> np.ndarray:
    """Every vector of a scratch copy of a real index, opened read-only (calibration only)."""
    db = _ro_connect(name)
    rows = db.execute("SELECT vector FROM chunk_vectors ORDER BY chunk_id").fetchall()
    db.close()
    return np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float32).reshape(-1, DIM).copy()


def real_corpus(name: str, n_queries: int = 200):
    """A real scope's PASSAGE vectors and the masks its default retrieval would apply, read-only.

    `kind = 'passage'` because the vector arm always asks for it (retriever.py:437, :488): outline
    chunks are never searched. They also sit close to their passages: including them lifts vault-a's
    mean 1st-neighbour cosine from 0.80 to 0.89 (results/calibration.txt), so a corpus with them is
    not the one the arm has.

    Queries are held-out passage chunks, removed from the corpus. A held-out chunk stands in for a
    query embedding; it is not one. Each query also carries, per filter, whether its OWN row would
    pass that filter, so short answers can be split: a query drawn from an excluded region (a
    conversation chunk under the default filter) is the hard case for a post-filter, and a real
    question can land there too, but it is not the typical one.

    Masks follow substrate's defaults: status IN (active, complete), class != conversation;
    `vault` adds the project vault (the "my notes, not core" ask); `entity` is the scope's most
    mentioned entity, when it has any.
    """
    db = _ro_connect(name)
    rows = db.execute(
        "SELECT v.vector, c.status, c.document_class, d.vault, c.doc_id FROM chunks c "
        "JOIN documents d ON d.doc_id = c.doc_id JOIN chunk_vectors v ON v.chunk_id = c.chunk_id "
        "WHERE c.kind = 'passage' ORDER BY c.chunk_id").fetchall()
    top = db.execute(
        "SELECT entity_id FROM document_entities de JOIN chunks c USING(doc_id) "
        "WHERE c.kind = 'passage' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1").fetchone()
    ent_docs = set()
    if top:
        ent_docs = {r[0] for r in db.execute(
            "SELECT doc_id FROM document_entities WHERE entity_id = ?", (top[0],))}
    db.close()

    def flags(rs):
        default = np.array([r[1] in ("active", "complete") and r[2] != "conversation" for r in rs], dtype=bool)
        ms = {"none": np.ones(len(rs), dtype=bool), "default": default,
              "vault": default & np.array([r[3] == f"{name}-vault" for r in rs], dtype=bool)}
        if ent_docs:
            ms["entity"] = default & np.array([r[4] in ent_docs for r in rs], dtype=bool)
        return ms

    allx = np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float32).reshape(-1, DIM)
    perm = np.random.default_rng(SEED).permutation(len(allx))
    nq = min(n_queries, len(allx) // 10)
    qi, xi = perm[:nq], np.sort(perm[nq:])
    ms = flags([rows[i] for i in xi])
    q_passes = flags([rows[i] for i in qi])
    return allx[xi].copy(), allx[qi].copy(), ms, q_passes
