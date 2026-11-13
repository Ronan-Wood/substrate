"""Same measurements as mlx_bench.py, over HTTP to Ollama or llama.cpp's llama-server.

  http_bench.py <ollama|llamacpp> <port> embed  <index.db>    <out.json>
  http_bench.py <ollama|llamacpp> <port> rerank <pairs.json>  <out.json>
  http_bench.py ollama            <port> hyde   <prompts.json> <out.json>
"""
import json, sqlite3, statistics, struct, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

import os

import numpy as np

NCTX = {"num_ctx": int(os.environ["NUM_CTX"])} if os.environ.get("NUM_CTX") else {}

backend, port, role, src, out = sys.argv[1], int(sys.argv[2]), *sys.argv[3:6]
URL = f"http://127.0.0.1:{port}"
EMB, GEN, RR = "qwen3-embedding:0.6b", "qwen3.5:4b-mlx", "dengcao/Qwen3-Reranker-4B:Q4_K_M"
m = statistics.median
q95 = lambda xs: sorted(xs)[int(0.95 * (len(xs) - 1))]

def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    s = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return (time.perf_counter() - s) * 1000, d

def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
                          ).stdout.split("used = ")[1].split()[0]

def embed(texts):
    if backend == "ollama":
        ms, d = post("/api/embed", {"model": EMB, "input": texts, **({"options": NCTX} if NCTX else {})})
        vs = d["embeddings"]
    else:
        ms, d = post("/v1/embeddings", {"input": texts})
        vs = [x["embedding"] for x in sorted(d["data"], key=lambda x: x["index"])]
    vs = np.asarray(vs, dtype=np.float32)
    return ms, vs / np.linalg.norm(vs, axis=1, keepdims=True)

def complete(prompt, n_predict, raw=True):
    if backend == "ollama":
        body = {"model": RR if raw else GEN, "prompt": prompt, "stream": False,
                "options": {"temperature": 0, "num_predict": n_predict, **NCTX}}
        body.update({"raw": True} if raw else {"think": False})
        ms, d = post("/api/generate", body)
        return ms, d.get("response", "")
    ms, d = post("/completion", {"prompt": prompt, "n_predict": n_predict, "temperature": 0,
                                 "cache_prompt": False})
    return ms, d.get("content", "")

res = {"backend": backend, "role": role}
if role == "embed":
    embed(["warm up"])
    lat = [embed([f"how does failover work when the primary dies {i}"])[0] for i in range(20)]
    res["query_ms_p50"], res["query_ms_p95"] = round(m(lat), 1), round(q95(lat), 1)
    with ThreadPoolExecutor(8) as ex:
        s = time.perf_counter()
        each = list(ex.map(lambda i: embed([f"failover question {i}"])[0], range(48)))
        res["query_8way_rps"] = round(48 / (time.perf_counter() - s), 1)
        res["query_8way_p95"] = round(q95(each), 1)
    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    # text_with_path is what `embed` stores a vector of (index_store.py:1117).
    rows = db.execute("SELECT c.chunk_id, c.text_with_path, v.vector FROM chunks c JOIN chunk_vectors v "
                      "USING (chunk_id) WHERE v.embed_model='qwen3-embedding:0.6b#raw' AND c.kind='passage' "
                      "ORDER BY c.chunk_id LIMIT 200").fetchall()
    texts = [r[1] for r in rows[:64]]
    s = time.perf_counter(); single = np.concatenate([embed([t])[1] for t in texts])
    res["doc_serial_ms_per_doc"] = round((time.perf_counter() - s) * 1000 / 64, 1)
    s = time.perf_counter(); b32 = np.concatenate([embed(texts[i:i + 32])[1] for i in (0, 32)])
    res["doc_batch32_ms_per_doc"] = round((time.perf_counter() - s) * 1000 / 64, 1)
    stored = np.asarray([struct.unpack(f"{len(r[2]) // 4}f", r[2]) for r in rows[:64]], np.float32)
    cos = (stored * single).sum(1); bcos = (single * b32).sum(1)
    res["cos_vs_stored_min"], res["cos_vs_stored_p50"] = round(float(cos.min()), 5), round(float(np.median(cos)), 5)
    res["cos_batch_vs_serial_min"] = round(float(bcos.min()), 5)
    np.save(out.replace(".json", ".npy"), single)
elif role == "rerank":
    pairs = json.load(open(src))
    complete(pairs[0]["prompt"], 1)
    lat, verdicts = [], []
    for p in pairs:
        ms, t = complete(p["prompt"], 1); lat.append(ms); verdicts.append(t)
    res["pair_ms_p50"], res["pair_ms_p95"] = round(m(lat), 1), round(q95(lat), 1)
    with ThreadPoolExecutor(4) as ex:            # substrate's own rerank concurrency
        s = time.perf_counter()
        list(ex.map(lambda p: complete(p["prompt"], 1), pairs[:20]))
        res["pool20_4way_ms"] = round((time.perf_counter() - s) * 1000)
    res["verdicts"] = verdicts
elif role == "hyde":
    prompts = json.load(open(src))
    complete(prompts[0], 160, raw=False)
    lat, texts = [], []
    for p in prompts:
        ms, t = complete(p, 160, raw=False); lat.append(ms); texts.append(t.strip())
    res["gen_ms_p50"], res["gen_ms_p95"] = round(m(lat), 1), round(q95(lat), 1)
    res["texts"] = texts
res["swap"] = swap()
json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k not in ("verdicts", "texts")}))
