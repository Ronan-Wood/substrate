"""Runtime cost of the `substrate` tokenizer on the large set: unicode61 vs substrate, per scope.

    python bench_large.py IDXDIR [--sections match,cold,tokq,size,rebuild,e2e] [--stacks lex,hybrid]

IDXDIR holds build_index.py's `<scope>.{unicode61,substrate}.db`, built with the dylib this run loads:
a preflight checksum `integrity-check` re-tokenizes every substrate arm under it and refuses a
mismatch. The scopes are the keys of $PRIVATE_EVAL/fts5-tokenizer/labels.json, and each scope's
queries are every case in $PRIVATE_EVAL/gold/gold-<scope>.json. It measures wall time, so run it on a
quiet machine, one process at a time, and not under `nice`. It opens the IDXDIR copies read-write, as
the engine does (WAL mode, `-wal`/`-shm` beside them), and changes no row in them.

Sections:
  match    warm lexical latency per query: the bare MATCH (the engine's AND expression, bm25 top 10)
           and `store.search` (AND, then the OR top-up, with the chunk join). One warm-up pass, then
           --repeats passes; each case's median over the passes, then p50/p95 over cases.
  cold     --cold runs of --cold-queries sampled cases, each query on a fresh connection, so SQLite's
           page cache is empty; the OS file cache is warm unless --purge empties it before every
           query. Also times opening the store (substrate's open includes loading the extension), and
           loading the extension into a connection on its own (the process's first load apart).
  tokq     query-side tokenizing on its own: the engine's AND and OR expressions matched against a
           one-row in-memory FTS5 table per tokenizer, --tok-iters times each, so what is timed is the
           expression's parse and tokenizing, not an index lookup. A second unicode61 table, timed
           the same way, is an A/A control whose difference is the method's noise floor.
  size     `chunks_fts` bytes per arm (dbstat, every shadow table) and the whole file.
  rebuild  a full `chunks_fts` rebuild as shipping does it once per index: on a scratch copy of the
           unicode61 arm, drop, create under each tokenizer, 'rebuild', commit, then the WAL
           checkpoint (auto-checkpoint off, so the split is real). Arms alternate; median of
           --rebuild-repeats.
  e2e      `retrieve()` as the eval calls it (k=10, route on, scoped cases scoped), per stack: lex (no
           vector arm) and hybrid (vector arm on, no HyDE, no rerank). Hybrid embeds each query once
           (timed) and hands both arms the same vector, so the paired difference carries no embedding
           noise; the share of the total adds the embed time back. Every MATCH expression an e2e call
           runs is logged and its tokenizing timed as in tokq, which gives the tokenizer's direct
           query-side share of that call.

Arms alternate order per query and per pass, and the garbage collector is off while timing. The
unicode61 arm's connections never load the extension, as in production.

Prints aggregates only, under the neutral labels in labels.json. Writes the same aggregates, plus
per-case timings keyed by case id, to $PRIVATE_EVAL/fts5-tokenizer/bench/ after every scope. The
rebuild copies hold real notes, so --scratch (default IDXDIR/bench-rebuild) is refused inside any git
working tree; the two copies are overwritten per scope and left for you to delete.

--guard runs every section of every scope as its own block under the runtime audit's load guard: it
starts only when no build or test burst (pytest, swift-build, swift-test, xctest, xcodebuild) is
running and the 1-minute load is below 3, and it is discarded and redone (up to 3 times) if a burst
starts mid-block. Load, memory pressure and swap are logged at each block's start and end.

--purge runs `/usr/sbin/purge` through sudo, which asks for your password once, at the start. Run it
with `--sections cold` on its own, so sudo's credential does not lapse between scopes.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
ENGINE = Path(os.environ.get("SUBSTRATE_ENGINE") or HERE.parent.parent / "substrate")
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(HERE))

import substrate_tok  # noqa: E402
from build_index import FTS, fresh_destination, refuse_real  # noqa: E402
from score_large import PRIVATE, t975  # noqa: E402
from substrate.eval.runner import resolve_docs  # noqa: E402
from substrate.retrieve.retriever import retrieve  # noqa: E402
from substrate.store import fts, schema  # noqa: E402
from substrate.store.index_store import IndexStore, SchemaMismatch  # noqa: E402

LABELS = json.loads((PRIVATE / "fts5-tokenizer" / "labels.json").read_text())
ARMS = ("unicode61", "substrate")
SECTIONS = ("match", "cold", "tokq", "size", "rebuild", "e2e")
K = 10


def _is_substrate_arm(target: str) -> bool:
    path = target.removeprefix("file:").split("?", 1)[0]
    return os.path.basename(path).endswith(".substrate.db")


def _connect(*args, **kwargs):
    """Load the extension only into connections to a substrate arm, as production would."""
    conn = substrate_tok._orig_connect(*args, **kwargs)
    target = str(args[0] if args else kwargs.get("database", ""))
    return substrate_tok.load(conn) if _is_substrate_arm(target) else conn


sqlite3.connect = _connect


# ---------------------------------------------------------------- statistics

def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    if not s:
        return math.nan
    i = (len(s) - 1) * p
    lo, hi = math.floor(i), math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def dist(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": pct(xs, .5), "p95": pct(xs, .95),
            "mean": statistics.fmean(xs) if xs else math.nan}


def paired(u: dict[str, float], s: dict[str, float]) -> dict:
    """substrate − unicode61 per case over the cases both have; the CI is t over cases."""
    ids = sorted(set(u) & set(s))
    d = [s[i] - u[i] for i in ids]
    m = statistics.fmean(d) if d else math.nan
    hw = t975(len(d) - 1) * statistics.stdev(d) / math.sqrt(len(d)) if len(d) >= 2 else None
    ds = dist([s[i] for i in ids])
    return {"n": len(ids), "unicode61": dist([u[i] for i in ids]), "substrate": ds,
            "delta_mean": m, "delta_ci": hw, "delta_p50": pct(d, .5),
            "delta_pct_of_substrate": 100 * m / ds["mean"] if ds["mean"] else math.nan}


# ---------------------------------------------------------------- timing

def timed(fn) -> float:
    t = time.perf_counter_ns()
    fn()
    return (time.perf_counter_ns() - t) / 1e6


class quiet_gc:
    def __enter__(self):
        gc.collect()
        gc.disable()

    def __exit__(self, *exc):
        gc.enable()


def interleaved(ids: list[str], calls: dict[str, dict], repeats: int, seed: int) -> dict[str, dict[str, float]]:
    """calls[arm][case_id] is a thunk. One warm-up pass, then `repeats` passes in a shuffled order,
    arms alternating; returns each case's median ms per arm."""
    for i in ids:
        for arm in ARMS:
            calls[arm][i]()
    got: dict[str, dict[str, list[float]]] = {a: {i: [] for i in ids} for a in ARMS}
    rng = random.Random(seed)
    with quiet_gc():
        for r in range(repeats):
            order = ids[:]
            rng.shuffle(order)
            for n, i in enumerate(order):
                for arm in (ARMS if (n + r) % 2 == 0 else ARMS[::-1]):
                    got[arm][i].append(timed(calls[arm][i]))
    return {a: {i: statistics.median(v) for i, v in got[a].items()} for a in ARMS}


def load_avg() -> list[float]:
    return [round(x, 2) for x in os.getloadavg()]


BURST = "pytest|swift-build|swift-test|xctest|xcodebuild"


class Guard:
    """The runtime audit's guarded.sh, per block. The burst monitor is a separate shell process, so
    it never contends with the timed code for the interpreter; the regex reaches it through the
    environment, so its own command line cannot match it."""

    def __init__(self, on: bool, *, burst: str = BURST, max_load: float = 3.0, wait_s: int = 1800,
                 attempts: int = 3, resets=(), on_refuse=None):
        self.on, self.burst, self.max_load, self.wait_s = on, burst, max_load, wait_s
        self.attempts, self.resets, self.log, self.on_refuse = attempts, list(resets), [], on_refuse

    def _refuse(self, msg: str):
        if self.on_refuse is not None:
            self.on_refuse()
        sys.exit(msg)

    def _busy(self) -> bool:
        hit = subprocess.run(["pgrep", "-f", self.burst], capture_output=True).returncode == 0
        return hit or os.getloadavg()[0] >= self.max_load

    @staticmethod
    def conditions() -> dict:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout.strip().splitlines()
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()
        return {"load": load_avg(), "memory": mp[-1].strip() if mp else "", "swap": sw}

    def run(self, label: str, fn):
        if not self.on:
            return fn()
        for attempt in range(1, self.attempts + 1):
            waited = 0
            while self._busy():
                if waited >= self.wait_s:
                    self._refuse(f"REFUSING: {label}: never quiet in {self.wait_s} s; finished blocks are saved")
                time.sleep(5)
                waited += 5
            with tempfile.TemporaryDirectory() as d:
                flag, hit = Path(d) / "on", Path(d) / "hit"
                flag.touch()
                mon = subprocess.Popen(
                    ["/bin/sh", "-c", 'while [ -f "$FLAG" ]; do pgrep -f "$BURST" >/dev/null && : >"$HIT"; sleep 2; done'],
                    env={"PATH": "/usr/bin:/bin", "FLAG": str(flag), "HIT": str(hit), "BURST": self.burst})
                try:
                    start = self.conditions()
                    res = fn()
                finally:
                    flag.unlink()
                    try:
                        mon.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        mon.kill()
                        mon.wait()
                end, burst = self.conditions(), hit.exists()
            self.log.append({"block": label, "attempt": attempt, "waited_s": waited, "start": start,
                             "end": end, "kept": not burst})
            if not burst:
                print(f"    [{label}: kept, attempt {attempt}, waited {waited} s, load {start['load'][0]}→{end['load'][0]}]")
                return res
            print(f"    [{label}: DISCARDED, a build/test burst started mid-block]")
            for reset in self.resets:
                reset()
        self._refuse(f"REFUSING: {label}: discarded {self.attempts} times, no clean block; finished blocks are saved")


class TokTimer:
    """Times one MATCH expression's parse + tokenizing against a one-row table per tokenizer,
    memoised per (table, expression). `control` is a second unicode61 table, the A/A check."""

    TABLES = {"unicode61": "", "control": ", tokenize='unicode61'", "substrate": ", tokenize='substrate'"}

    def __init__(self, iters: int):
        self.iters, self.memo = iters, {}
        self.db = substrate_tok.load(substrate_tok._orig_connect(":memory:"))
        for name, tok in self.TABLES.items():
            self.db.execute(f"CREATE VIRTUAL TABLE t_{name} USING fts5(x{tok})")
            self.db.execute(f"INSERT INTO t_{name}(x) VALUES ('x')")

    def us(self, table: str, expr: str) -> float:
        key = (table, expr)
        if key not in self.memo:
            sql = f"SELECT count(*) FROM t_{table} WHERE t_{table} MATCH ?"
            t = time.perf_counter_ns()
            for _ in range(self.iters):
                self.db.execute(sql, (expr,)).fetchone()
            self.memo[key] = (time.perf_counter_ns() - t) / 1e3 / self.iters
        return self.memo[key]

    def warm(self, exprs, rotate: int = 0) -> None:
        """Time each expression under every table, the table order rotated, under quiet_gc."""
        names = list(self.TABLES)
        with quiet_gc():
            for n, e in enumerate(exprs):
                k = (n + rotate) % 3
                for t in names[k:] + names[:k]:
                    self.us(t, e)


# ---------------------------------------------------------------- the large set

def arm_path(idx: Path, scope: str, arm: str) -> Path:
    return idx / f"{scope}.{arm}.db"


def gold_cases(scope: str) -> list[dict]:
    cases = json.loads((PRIVATE / "gold" / f"gold-{scope}.json").read_text())["cases"]
    if len({c["id"] for c in cases}) != len(cases):
        sys.exit(f"REFUSING: {LABELS[scope]}'s gold file repeats a case id")
    return cases


def no_wal_content(p: Path, what: str) -> None:
    wal = Path(f"{p}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        sys.exit(f"REFUSING: {what} has uncheckpointed WAL content")


def snapshot(p: Path, what: str) -> sqlite3.Connection:
    """A read that writes nothing beside `p` (immutable=1 reads the main file only)."""
    no_wal_content(p, what)
    return sqlite3.connect(f"file:{quote(str(p.resolve()))}?immutable=1", uri=True)


def preflight(idx: Path, scope: str) -> dict:
    out = {}
    for arm in ARMS:
        p, what = arm_path(idx, scope, arm), f"{LABELS[scope]}'s {arm} arm"
        if not p.exists():
            sys.exit(f"REFUSING: {what} is missing")
        if p.is_symlink() or p.resolve().parent != idx:
            sys.exit(f"REFUSING: {what} is a link out of IDXDIR")
        no_wal_content(p, what)
        try:
            store = IndexStore(p, migrate=False)
        except SchemaMismatch:
            sys.exit(f"REFUSING: {what} is not schema v{schema.SCHEMA_VERSION}")
        db = store.db
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()[0]
        if ("tokenize='substrate'" in sql) != (arm == "substrate"):
            sys.exit(f"REFUSING: {what} declares the wrong tokenizer")
        out[arm] = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if arm == "substrate":
            # rank=1 makes FTS5 re-tokenize every row of the external content and compare checksums
            # with the index; without it, it checks only the index's own structure. A substrate arm
            # built by another build of the tokenizer fails here instead of being timed.
            try:
                db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)")
            except sqlite3.DatabaseError as e:
                store.close()
                sys.exit(f"REFUSING: {what} fails integrity-check under this dylib ({e}); rebuild it")
            db.rollback()
        store.close()
    if out["unicode61"] != out["substrate"]:
        sys.exit(f"REFUSING: {LABELS[scope]}'s arms hold different chunk counts")
    return {"chunks": out["substrate"]}


def raw_match(db: sqlite3.Connection, expr: str):
    sql = "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?"
    return lambda: db.execute(sql, (expr, K)).fetchall()


def sec_match(idx: Path, scope: str, cases: list[dict], a) -> tuple[dict, dict]:
    stores = {arm: IndexStore(arm_path(idx, scope, arm), migrate=False) for arm in ARMS}
    try:
        qs = [c for c in cases if fts.and_expression(c["query"])]
        ids = [c["id"] for c in qs]
        bare = {arm: {c["id"]: raw_match(stores[arm].db, fts.and_expression(c["query"])) for c in qs}
                for arm in ARMS}
        search = {arm: {c["id"]: (lambda s=stores[arm], q=c["query"]: s.search(q, k=K)) for c in qs}
                  for arm in ARMS}
        b = interleaved(ids, bare, a.repeats, a.seed)
        s = interleaved(ids, search, a.repeats, a.seed + 1)
    finally:
        for st in stores.values():
            st.close()
    return {"bare_match": paired(b["unicode61"], b["substrate"]),
            "store_search": paired(s["unicode61"], s["substrate"])}, {"bare_match": b, "store_search": s}


def purge() -> None:
    if subprocess.run(["/usr/bin/sudo", "-n", "/usr/sbin/purge"]).returncode != 0:
        sys.exit("REFUSING to go on: `sudo -n purge` failed (credential lapsed?); only finished scopes are saved")


def sec_cold(idx: Path, scope: str, cases: list[dict], a) -> dict:
    qs = [c for c in cases if fts.and_expression(c["query"])]
    q_ms = {arm: [] for arm in ARMS}
    open_ms = {arm: [] for arm in ARMS}
    for run in range(a.cold):
        sample = random.Random(a.seed * 1000 + run).sample(qs, min(a.cold_queries, len(qs)))
        for n, c in enumerate(sample):
            for arm in (ARMS if n % 2 == 0 else ARMS[::-1]):
                if a.purge:
                    purge()
                with quiet_gc():
                    t = time.perf_counter_ns()
                    st = IndexStore(arm_path(idx, scope, arm), migrate=False)
                    open_ms[arm].append((time.perf_counter_ns() - t) / 1e6)
                    try:
                        q_ms[arm].append(timed(lambda: st.search(c["query"], k=K)))
                    finally:
                        st.close()
    return {"query": {arm: dist(q_ms[arm]) for arm in ARMS},
            "open_store": {arm: dist(open_ms[arm]) for arm in ARMS}}


def load_cost(n: int) -> dict:
    ms = []
    with quiet_gc():
        for _ in range(n):
            db = substrate_tok._orig_connect(":memory:")
            ms.append(timed(lambda: substrate_tok.load(db)))
            db.close()
    return {"first_ms": ms[0], **dist(ms[1:] or ms)}


def sec_tokq(cases: list[dict], tt: TokTimer) -> tuple[dict, dict]:
    pairs = {c["id"]: [e for e in (fts.and_expression(c["query"]), fts.or_expression(c["query"])) if e]
             for c in cases}
    pairs = {i: es for i, es in pairs.items() if es}
    tt.warm([e for es in pairs.values() for e in es])
    per = {form: {t: {} for t in TokTimer.TABLES} for form in ("and", "and_or")}
    for i, es in pairs.items():
        for t in TokTimer.TABLES:
            per["and"][t][i] = tt.us(t, es[0])
            per["and_or"][t][i] = sum(tt.us(t, e) for e in es)
    res = {form: paired(v["unicode61"], v["substrate"]) for form, v in per.items()}
    res["aa_and_or"] = paired(per["and_or"]["unicode61"], per["and_or"]["control"])
    return res, per


def fts_bytes(p: Path, what: str) -> tuple[int, int]:
    db = snapshot(p, what)
    try:
        b = db.execute("SELECT coalesce(sum(pgsize), 0) FROM dbstat WHERE name LIKE 'chunks\\_fts%' "
                       "ESCAPE '\\'").fetchone()[0]
        f = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    finally:
        db.close()
    return b, f


def sec_size(idx: Path, scope: str) -> dict:
    (bu, fu), (bs, fs) = (fts_bytes(arm_path(idx, scope, arm), f"{LABELS[scope]}'s {arm} arm") for arm in ARMS)
    return {"chunks_fts": {"unicode61": bu, "substrate": bs, "delta": bs - bu,
                           "delta_pct": 100 * (bs - bu) / bu if bu else math.nan},
            "file": {"unicode61": fu, "substrate": fs, "delta": fs - fu,
                     "delta_pct": 100 * (fs - fu) / fu if fu else math.nan}}


def sec_rebuild(idx: Path, scope: str, scratch: Path, a) -> dict:
    """Both arms start from a fresh copy of the unicode61 arm each repeat, so each drops the table an
    existing index has and builds its own, as the shipping migration would."""
    base = arm_path(idx, scope, "unicode61")
    times = {arm: {"drop_create_rebuild_commit_s": [], "checkpoint_s": [], "total_s": []} for arm in ARMS}
    text = None
    for r in range(a.rebuild_repeats):
        for arm in (ARMS if r % 2 == 0 else ARMS[::-1]):
            dst = scratch / f"rebuild.{arm}.db"
            fresh_destination(dst)
            src, d = snapshot(base, f"{LABELS[scope]}'s unicode61 arm"), substrate_tok._orig_connect(dst)
            src.backup(d)
            src.close()
            d.close()
            db = schema.connect(str(dst))  # the engine's pragmas: WAL, synchronous=NORMAL
            db.isolation_level = None
            db.execute("PRAGMA wal_autocheckpoint=0")
            with quiet_gc():
                t = time.perf_counter_ns()
                db.execute("BEGIN")
                db.execute("DROP TABLE chunks_fts")
                db.execute(f"CREATE VIRTUAL TABLE chunks_fts USING fts5({FTS[arm]}\n)")
                db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                db.execute("COMMIT")
                t2 = time.perf_counter_ns()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                t3 = time.perf_counter_ns()
            if text is None:
                text = db.execute("SELECT coalesce(sum(length(CAST(text_with_path AS BLOB))), 0) FROM chunks").fetchone()[0]
            db.close()
            for key, v in (("drop_create_rebuild_commit_s", t2 - t), ("checkpoint_s", t3 - t2), ("total_s", t3 - t)):
                times[arm][key].append(v / 1e9)
    out = {arm: {k: statistics.median(v) for k, v in times[arm].items()} for arm in ARMS}
    u, s = out["unicode61"]["total_s"], out["substrate"]["total_s"]
    out.update(text_bytes=text, delta_s=s - u, ratio=s / u if u else math.nan)
    return out


class MemoEmbedder:
    """One embed call per query text, timed, shared by both arms."""

    def __init__(self, inner):
        self.inner, self.vecs, self.ms, self.asked = inner, {}, {}, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def embed_query(self, text: str):
        self.asked.append(text)
        v = self.vecs.get(text)
        if v is None:
            t = time.perf_counter_ns()
            v = self.inner.embed_query(text)
            self.ms[text] = (time.perf_counter_ns() - t) / 1e6
            self.vecs[text] = v
        return v


def make_embedder():
    from substrate.embed.engine import DEFAULT_MODEL, OllamaEmbedder

    e = OllamaEmbedder(model=DEFAULT_MODEL, prefix_style="auto")
    if not e.available():
        sys.exit(f"REFUSING: the hybrid stack needs {DEFAULT_MODEL} on the local Ollama, and it is not there")
    e.embed_query("load the model before anything is timed")
    return MemoEmbedder(e)


def sec_e2e(idx: Path, scope: str, cases: list[dict], stack: str, embedder, tt: TokTimer, a) -> tuple[dict, dict]:
    stores = {arm: IndexStore(arm_path(idx, scope, arm), migrate=False) for arm in ARMS}
    emb = embedder if stack == "hybrid" else None
    try:
        if emb is not None:
            for arm, st in stores.items():
                n = st.db.execute("SELECT count(*) FROM chunk_vectors WHERE embed_model=?", (emb.key,)).fetchone()[0]
                total = st.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
                if n < total:
                    sys.exit(f"REFUSING: {LABELS[scope]}'s {arm} arm has {n}/{total} vectors under {emb.key}")
        docs = {arm: resolve_docs(st) for arm, st in stores.items()}
        fallbacks, texts = [], {}

        def thunk(arm: str, c: dict):
            # Mirrors runner.run_case's timed call.
            scoped, target = c.get("doc"), c.get("doc") or c.get("expect_doc")
            doc_id = docs[arm].get(target) if target else None
            if target and doc_id is None:
                return None
            st = stores[arm]
            if c.get("kind") == "outline":
                return lambda: st.search(c["query"], k=K, kind="outline", doc_id=doc_id if scoped else None)

            def go():
                if emb is not None:
                    emb.asked.clear()
                r = retrieve(st, c["query"], k=K, doc_id=doc_id if scoped else None, route=True, embedder=emb)
                if r.capability is not None and r.capability.fallbacks:
                    fallbacks.append(c["id"])
                if emb is not None:
                    texts[c["id"]] = tuple(emb.asked)
            return go

        calls = {arm: {} for arm in ARMS}
        for c in cases:
            th = {arm: thunk(arm, c) for arm in ARMS}
            if all(th.values()):
                for arm in ARMS:
                    calls[arm][c["id"]] = th[arm]
        ids = list(calls["unicode61"])

        # One logged pass: every MATCH expression each call runs, per arm (an OR top-up runs only
        # when AND came up short, which depends on the index).
        exprs = {arm: {} for arm in ARMS}
        for arm, st in stores.items():
            for i in ids:
                log = exprs[arm][i] = []
                st._match = lambda e, _orig=type(st)._match, _st=st, _log=log, **kw: (_log.append(e), _orig(_st, e, **kw))[1]
                calls[arm][i]()
            st.__dict__.pop("_match", None)
        silent = [c["id"] for c in cases if c["id"] in calls["unicode61"] and c.get("kind") != "outline"
                  and fts.and_expression(c["query"]) and not all(exprs[arm][c["id"]] for arm in ARMS)]
        if silent:
            sys.exit(f"REFUSING: {len(silent)} {stack} case(s) in {LABELS[scope]} ran no logged MATCH; "
                     "the engine's lexical path no longer goes through IndexStore._match")
        got = interleaved(ids, calls, a.repeats, a.seed + 2)
        if fallbacks:
            sys.exit(f"REFUSING: {len(set(fallbacks))} {stack} case(s) in {LABELS[scope]} degraded "
                     "(an arm fell back), so the timing is not of the stack it is labelled")
    finally:
        for st in stores.values():
            st.close()

    tt.warm([e for arm in ARMS for es in exprs[arm].values() for e in es], rotate=1)
    tok = {arm: {i: sum(tt.us(arm, e) for e in exprs[arm][i]) / 1000 for i in ids} for arm in ARMS}
    got = {**got, "tok_ms": tok, "match_calls": {arm: {i: len(exprs[arm][i]) for i in ids} for arm in ARMS}}
    res = {"retrieve": paired(got["unicode61"], got["substrate"]), "tokenizing": paired(tok["unicode61"], tok["substrate"]),
           "match_calls_per_query": {arm: dist([len(v) for v in exprs[arm].values()]) for arm in ARMS}}
    embed_mean = 0.0
    if emb is not None:
        # A case's embed time is that of every text its query sent to the embedder (outline cases
        # send none), wherever the vector was first fetched; each text was embedded exactly once.
        e = {i: sum(emb.ms[t] for t in texts.get(i, ())) for i in ids}
        res["embed"] = dist([v for v in e.values() if v])
        embed_mean = statistics.fmean(e.values()) if e else 0.0
        got["embed_ms"] = e
    res["share"] = share(res["retrieve"], res["tokenizing"], embed_mean)
    return res, got


def share(ret: dict, tok: dict, embed_mean: float) -> dict:
    total = ret["substrate"]["mean"] + embed_mean
    return {"total_ms": total, "e2e_delta_pct": 100 * ret["delta_mean"] / total if total else math.nan,
            "tokenizing_delta_pct": 100 * tok["delta_mean"] / total if total else math.nan,
            "tokenizing_substrate_pct": 100 * tok["substrate"]["mean"] / total if total else math.nan}


def pooled(raw: dict) -> dict:
    """Every scope's per-case medians in one paired comparison, ids prefixed by scope."""
    def join(key: str, form: str | None, arms=ARMS) -> dict | None:
        if not raw or any(key not in rr for rr in raw.values()):
            return None
        parts = {arm: {} for arm in arms}
        for scope, rr in raw.items():
            got = rr[key][form] if form else rr[key]
            for arm in arms:
                parts[arm].update({f"{scope}:{i}": v for i, v in got[arm].items()})
        return paired(*parts.values())

    out = {}
    for name, key, form, arms in (("tok and", "tokq", "and", ARMS), ("tok and_or", "tokq", "and_or", ARMS),
                                  ("tok A/A", "tokq", "and_or", ("unicode61", "control")),
                                  ("bare match", "match", "bare_match", ARMS),
                                  ("store search", "match", "store_search", ARMS),
                                  ("e2e lex", "e2e_lex", None, ARMS), ("e2e lex tok", "e2e_lex", "tok_ms", ARMS),
                                  ("e2e hybrid", "e2e_hybrid", None, ARMS),
                                  ("e2e hybrid tok", "e2e_hybrid", "tok_ms", ARMS)):
        if (p := join(key, form, arms)) is not None:
            out[name] = p
    for stack in ("lex", "hybrid"):
        if f"e2e {stack}" in out:
            emb = [v for rr in raw.values() for v in rr[f"e2e_{stack}"].get("embed_ms", {}).values()]
            out[f"share {stack}"] = share(out[f"e2e {stack}"], out[f"e2e {stack} tok"],
                                          statistics.fmean(emb) if emb else 0.0)
    return out


# ---------------------------------------------------------------- report

def f(x: float | None, nd: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def line(label: str, p: dict, unit: str = "ms", nd: int = 3) -> str:
    u, s = p["unicode61"], p["substrate"]
    ci = f" ± {f(p['delta_ci'], nd)}" if p["delta_ci"] is not None else ""
    return (f"  {label:<15} n={p['n']:<5} unicode61 p50 {f(u['p50'], nd)} p95 {f(u['p95'], nd)}  |  "
            f"substrate p50 {f(s['p50'], nd)} p95 {f(s['p95'], nd)} {unit}  |  "
            f"Δ mean {f(p['delta_mean'], nd)}{ci} {unit} ({f(p['delta_pct_of_substrate'], 1)}% of substrate's mean)")


def aa_line(label: str, p: dict) -> str:
    return (f"  {label:<15} n={p['n']:<5} unicode61 vs unicode61: Δ mean {f(p['delta_mean'])} ± "
            f"{f(p['delta_ci'])} µs, the noise floor for the tokenizing lines")


def share_line(stack: str, z: dict) -> str:
    return (f"  share {stack:<9} of a {f(z['total_ms'])} ms call: the paired e2e Δ is {f(z['e2e_delta_pct'])}%; "
            f"substrate's query-side tokenizing is {f(z['tokenizing_substrate_pct'])}% "
            f"(its Δ vs unicode61's, {f(z['tokenizing_delta_pct'])}%)")


def report(name: str, p: dict) -> str:
    if name == "tok A/A":
        return aa_line(name, p)
    if name.startswith("share "):
        return share_line(name.removeprefix("share "), p)
    if name.startswith("tok"):
        return line(name, p, "µs", 2)
    return line(name, p, "ms", 4 if name.endswith(" tok") else 3)


def environment(a) -> dict:
    brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    prov = ENGINE / "PROVENANCE"
    return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "macos": platform.mac_ver()[0],
            "cpu": brand, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "dylib_sha256": hashlib.sha256(substrate_tok.LIB.read_bytes()).hexdigest(),
            "engine": prov.read_text().split()[0] if prov.exists() else None,
            "args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items() if k != "idx"}}


def refuse_in_git(p: Path) -> None:
    """Refuse a folder inside any git working tree, checked from its nearest existing ancestor (so a
    letter-case variant of a path, or another worktree of the same repository, is caught too)."""
    anc = p
    while not anc.exists():
        anc = anc.parent
    r = subprocess.run(["git", "-C", str(anc), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip() == "true":
        sys.exit("REFUSING: --scratch is inside a git working tree; the rebuild copies hold real notes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("idx", type=Path, nargs="?")
    ap.add_argument("--sections", default=",".join(SECTIONS))
    ap.add_argument("--stacks", default="lex,hybrid")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--cold", type=int, default=3)
    ap.add_argument("--cold-queries", type=int, default=25)
    ap.add_argument("--tok-iters", type=int, default=200)
    ap.add_argument("--rebuild-repeats", type=int, default=3)
    ap.add_argument("--load-iters", type=int, default=200)
    ap.add_argument("--scratch", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--guard", action="store_true")
    ap.add_argument("--scopes", default="", help="comma-separated neutral labels (default: all)")
    ap.add_argument("--pool", nargs="+", type=Path, metavar="CASES_JSON",
                    help="merge earlier runs' .cases.json (later files win per scope and section) and pool them")
    a = ap.parse_args()
    if a.pool:
        merged: dict = {}
        for f_ in a.pool:
            for scope, rr in json.loads(f_.read_text()).items():
                merged.setdefault(scope, {}).update(rr)
        print("all scopes pooled:")
        for name, p_ in pooled(merged).items():
            print(report(name, p_))
        return
    if a.idx is None:
        ap.error("IDXDIR is required unless --pool is given")

    sections = [s for s in a.sections.split(",") if s]
    stacks = [s for s in a.stacks.split(",") if s]
    if bad := [s for s in sections if s not in SECTIONS] + [s for s in stacks if s not in ("lex", "hybrid")]:
        sys.exit(f"unknown section or stack: {', '.join(bad)}")
    if min(a.repeats, a.cold_queries, a.tok_iters, a.rebuild_repeats, a.load_iters) < 1 or a.cold < 0:
        sys.exit("counts must be at least 1 (--cold may be 0)")
    want = [x.strip() for x in a.scopes.split(",") if x.strip()]
    if bad := [x for x in want if x not in LABELS.values()]:
        sys.exit(f"unknown scope label(s): {', '.join(bad)}")
    idx = a.idx.resolve()
    refuse_real(idx)
    scratch = (a.scratch or idx / "bench-rebuild").resolve()
    if "rebuild" in sections:
        refuse_real(scratch)
        refuse_in_git(scratch)
        scratch.mkdir(parents=True, exist_ok=True)
    if a.purge and "cold" in sections and a.cold:
        print("--purge: sudo asks for your password now, once")
        if subprocess.run(["/usr/bin/sudo", "-v"]).returncode != 0:
            sys.exit("REFUSING: sudo -v failed, so --purge cannot run")

    env = environment(a)
    print(f"bench_large: {env['utc']}  macOS {env['macos']}  {env['cpu']}  SQLite {env['sqlite']}  "
          f"dylib {env['dylib_sha256'][:12]}  load {load_avg()}")
    agg, raw = {"environment": env, "scopes": {}}, {}
    if "cold" in sections and a.cold:
        # Before anything else loads the extension, so the first sample is the process's first load.
        agg["extension_load_ms"] = z = load_cost(a.load_iters)  # unguarded: the first sample is one-off
        print(f"  extension load into a connection: first {f(z['first_ms'], 3)} ms, then p50 {f(z['p50'], 3)} ms")
    embedder = make_embedder() if "e2e" in sections and "hybrid" in stacks else None
    tt = TokTimer(a.tok_iters) if {"tokq", "e2e"} & set(sections) else None

    def forget() -> None:
        """A discarded block's timings must not be reused by its redo."""
        if tt is not None:
            tt.memo.clear()
        if embedder is not None:
            embedder.vecs.clear()
            embedder.ms.clear()

    guard = Guard(a.guard, resets=[forget], on_refuse=lambda: save())

    out = PRIVATE / "fts5-tokenizer" / "bench"
    out.mkdir(parents=True, exist_ok=True)
    stamp = env["utc"].replace(":", "")

    def save() -> None:
        agg["guard"] = guard.log
        (out / f"bench-{stamp}.json").write_text(json.dumps(agg, indent=1))
        (out / f"bench-{stamp}.cases.json").write_text(json.dumps(raw))

    for scope, label in LABELS.items():
        if want and label not in want:
            continue
        cases = gold_cases(scope)
        r, rr = {"preflight": preflight(idx, scope), "load_start": load_avg()}, {}
        agg["scopes"][label], raw[scope] = r, rr
        print(f"\n{label}: {r['preflight']['chunks']} chunks, {len(cases)} cases")
        if "size" in sections:
            r["size"] = z = sec_size(idx, scope)  # not timed
            print(f"  chunks_fts {z['chunks_fts']['unicode61']:,} → {z['chunks_fts']['substrate']:,} B "
                  f"({z['chunks_fts']['delta']:+,} B, {z['chunks_fts']['delta_pct']:+.1f}%); "
                  f"file {z['file']['delta']:+,} B ({z['file']['delta_pct']:+.1f}%)")
        if "rebuild" in sections:
            r["rebuild"] = z = guard.run(f"{label} rebuild", lambda: sec_rebuild(idx, scope, scratch, a))
            print(f"  rebuild {z['unicode61']['total_s']:.2f} s → {z['substrate']['total_s']:.2f} s "
                  f"({z['delta_s']:+.2f} s, ×{z['ratio']:.2f}; checkpoint {z['substrate']['checkpoint_s']:.2f} s of it)")
        if "tokq" in sections:
            r["tokq"], rr["tokq"] = guard.run(f"{label} tokq", lambda: sec_tokq(cases, tt))
            print(report("tok and", r["tokq"]["and"]))
            print(report("tok and_or", r["tokq"]["and_or"]))
            print(report("tok A/A", r["tokq"]["aa_and_or"]))
        if "match" in sections:
            r["match"], rr["match"] = guard.run(f"{label} match", lambda: sec_match(idx, scope, cases, a))
            print(report("bare match", r["match"]["bare_match"]))
            print(report("store search", r["match"]["store_search"]))
        if "cold" in sections and a.cold:
            r["cold"] = z = guard.run(f"{label} cold", lambda: sec_cold(idx, scope, cases, a))
            for arm in ARMS:
                print(f"  cold {arm:<10} query p50 {f(z['query'][arm]['p50'], 3)} p95 {f(z['query'][arm]['p95'], 3)} ms; "
                      f"open p50 {f(z['open_store'][arm]['p50'], 3)} ms")
        if "e2e" in sections:
            for stack in stacks:
                r[f"e2e_{stack}"], rr[f"e2e_{stack}"] = guard.run(
                    f"{label} e2e {stack}", lambda: sec_e2e(idx, scope, cases, stack, embedder, tt, a))
                z = r[f"e2e_{stack}"]
                print(report(f"e2e {stack}", z["retrieve"]))
                print(report(f"e2e {stack} tok", z["tokenizing"]))
                print(report(f"share {stack}", z["share"]))
        r["load_end"] = load_avg()
        save()

    agg["pooled"] = pooled(raw)
    save()
    if agg["pooled"]:
        print("\nall scopes pooled:")
        for name, p in agg["pooled"].items():
            print(report(name, p))
    print(f"\naggregates and per-case timings: $PRIVATE_EVAL/fts5-tokenizer/bench/bench-{stamp}{{,.cases}}.json")
    if "rebuild" in sections:
        print("rebuild copies (real notes) are left in --scratch (default IDXDIR/bench-rebuild); delete them by hand")


if __name__ == "__main__":
    main()
