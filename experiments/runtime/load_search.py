"""Full substrate searches under realistic load: C concurrent searchers, optionally with a background
`embed`-shaped job (batches of 32 documents) running against the same daemon.
  PYTHONPATH=substrate python load_search.py <port> <index.db> <queries.json> <scratch_dir> <C> <bg:0|1>
Fresh generator caches per run, so every HyDE and rerank call reaches the daemon.
"""
import json, sqlite3, statistics, subprocess, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from substrate.embed.cache import VectorCache
from substrate.embed.engine import OllamaEmbedder
from substrate.retrieve import retriever
from substrate.retrieve.expand import HyDE
from substrate.retrieve.rerank_cross import CrossEncoderReranker
from substrate.store.index_store import IndexStore

import os
if os.environ.get("NUM_CTX"):
    # EXPERIMENT ONLY: what a per-request `options.num_ctx` would do, injected at the one transport
    # function every arm calls, without touching substrate's code.
    import urllib.request as _u
    from substrate import net
    from substrate.embed import engine as _e
    from substrate.retrieve import expand as _x, rerank_cross as _r
    _orig = net.open_local
    def _with_ctx(req, *, timeout):
        if isinstance(req, _u.Request) and req.data and req.full_url.endswith(("/api/embed", "/api/generate")):
            body = json.loads(req.data)
            body.setdefault("options", {})["num_ctx"] = int(os.environ["NUM_CTX"])
            req = _u.Request(req.full_url, data=json.dumps(body).encode(), headers=dict(req.headers))
        return _orig(req, timeout=timeout)
    for mod in (_e, _x, _r):
        mod.open_local = _with_ctx

port, db, qfile, scratch, C, bg =sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]), int(sys.argv[5]), sys.argv[6] == "1"
host = f"http://127.0.0.1:{port}"
cache = VectorCache(str(scratch / f"load-cache-{uuid.uuid4().hex[:8]}.db"))
emb = OllamaEmbedder(model="qwen3-embedding:0.6b", host=host)
hyde = HyDE(model="qwen3.5:4b-mlx", host=host, cache=cache)
rr = CrossEncoderReranker(host=host, cache=cache, gate=True)
queries = json.load(open(qfile))

def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
                          ).stdout.split("used = ")[1].split()[0]

stop = threading.Event(); bg_docs = [0]
def background():
    texts = [r[0] for r in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "SELECT text FROM chunks WHERE kind='passage' ORDER BY chunk_id LIMIT 2000")]
    i = 0
    while not stop.is_set():
        emb.embed_documents(texts[i % len(texts): i % len(texts) + 32]); bg_docs[0] += 32; i += 32

def one(q):
    store = IndexStore(db, migrate=False)
    try:
        s = time.perf_counter()
        r = retriever.retrieve(store, q, k=5, with_outlines=3, embedder=emb, expander=hyde, reranker=rr)
        return (time.perf_counter() - s) * 1000, r.capability
    finally:
        store.close()

emb.embed_query("warm"); hyde.expand("warm up query " + uuid.uuid4().hex)
t = threading.Thread(target=background, daemon=True)
if bg:
    t.start(); time.sleep(2)
s = time.perf_counter()
with ThreadPoolExecutor(C) as ex:
    results = list(ex.map(one, queries))
wall = time.perf_counter() - s
stop.set()
if bg:
    t.join()
lat = [x[0] for x in results]
fb = sum(1 for x in results if x[1].fallbacks)
q95 = sorted(lat)[int(0.95 * (len(lat) - 1))]
print(f"C={C} bg={int(bg)}  n={len(lat)}  p50 {statistics.median(lat):7.0f}  p95 {q95:7.0f} ms  "
      f"throughput {len(lat) / wall:5.2f} q/s  fallbacks {fb}  bg docs/s "
      f"{bg_docs[0] / wall:6.1f}  reranked {sum(1 for x in results if x[1].reranker == 'ran')}  "
      f"swap {swap()}", flush=True)
