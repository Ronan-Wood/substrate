"""Paired, interleaved runtime comparison for one model role. Every trial calls every runtime once,
in a rotating order, so machine-wide drift (other sessions on the same GPU) hits all of them alike.

  interleave.py embed  <index.db>  <hf_embed_dir> <out.json> name=kind:port ...
  interleave.py rerank <pairs.json> <hf_rr_dir>   <out.json> name=kind:port ...
  interleave.py hyde   <prompts.json> <mlx_dir>   <out.json> name=kind:port ...
kind is ollama | llamacpp; an in-process `mlx` runtime is always added (the hf/mlx dir).
Memory is the process's phys_footprint (`footprint`), which counts Metal buffers; `ollama ps`
does not, and under-reports by ~40% for a runner with a large KV cache.
"""
import json, re, sqlite3, statistics, struct, subprocess, sys, time, urllib.request

import mlx.core as mx
import numpy as np

# MLX keeps freed buffers for reuse; across variable-length inputs that cache grew to 16 GB in a
# first run, which pushed the machine into swap and voided every number in it.
mx.set_cache_limit(1 << 30)

role, src, mdir, out = sys.argv[1:5]
RT = [a.split("=", 1) for a in sys.argv[5:]]
RT = {n: (k.split(":")[0], int(k.split(":")[1])) for n, k in RT}
EMB, GEN, RR = "qwen3-embedding:0.6b", "qwen3.5:4b-mlx", "dengcao/Qwen3-Reranker-4B:Q4_K_M"
NCTX = 4096
med = statistics.median
q95 = lambda xs: sorted(xs)[int(0.95 * (len(xs) - 1))]

def post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())

def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
                          ).stdout.split("used = ")[1].split()[0]

def footprint_gb(pattern):
    pids = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    tot = 0.0
    for p in pids:
        o = subprocess.run(["footprint", "-p", p], capture_output=True, text=True).stdout
        mm = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", o)
        if mm:
            tot += float(mm.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1}[mm.group(2)]
    return round(tot, 2)

# ---------------------------------------------------------------- in-process MLX
from mlx_lm import load
if role == "embed":
    # Qwen3-Embedding ships the bare Qwen3Model (no `model.` prefix, no lm_head), which
    # mlx_lm.load refuses; build the causal LM and load the trunk weights into it.
    import glob
    from pathlib import Path
    from mlx_lm.models import qwen3
    from mlx_lm.tokenizer_utils import load as load_tokenizer
    model = qwen3.Model(qwen3.ModelArgs.from_dict(json.loads((Path(mdir) / "config.json").read_text())))
    w = {}
    for f in glob.glob(f"{mdir}/*.safetensors"):
        w.update({"model." + k: v for k, v in mx.load(f).items()})
    model.load_weights(list(w.items()), strict=False); mx.eval(model.parameters())
    tok = load_tokenizer(Path(mdir))
else:
    model, tok = load(mdir)

if role == "embed":
    inner = model.model
    def mlx_embed(texts):
        out_ = []
        for t in texts:                                   # unpadded: exact last-token pooling
            h = inner(mx.array([tok.encode(t)]))   # the tokenizer appends <|endoftext|> itself
            v = h[0, -1].astype(mx.float32); v = v / mx.linalg.norm(v); mx.eval(v)
            out_.append(np.array(v))
        return np.stack(out_)
    def http_embed(kind, port, texts):
        if kind == "ollama":
            vs = post(port, "/api/embed", {"model": EMB, "input": texts, "options": {"num_ctx": NCTX}})["embeddings"]
        else:
            d = post(port, "/v1/embeddings", {"input": texts})["data"]
            vs = [x["embedding"] for x in sorted(d, key=lambda x: x["index"])]
        vs = np.asarray(vs, np.float32)
        return vs / np.linalg.norm(vs, axis=1, keepdims=True)
    call = {"mlx": mlx_embed, **{n: (lambda texts, k=k, p=p: http_embed(k, p, texts)) for n, (k, p) in RT.items()}}

    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    # text_with_path is what `embed` stores a vector of (index_store.py:1117), not text.
    rows = db.execute("SELECT c.text_with_path, v.vector FROM chunks c "
                      "JOIN chunk_vectors v USING (chunk_id) "
                      "WHERE v.embed_model='qwen3-embedding:0.6b#raw' AND c.kind='passage' "
                      "ORDER BY c.chunk_id LIMIT 64").fetchall()
    texts = [r[0] for r in rows]
    stored = np.asarray([struct.unpack(f"{len(r[1]) // 4}f", r[1]) for r in rows], np.float32)
    names = list(call)
    for n in names:
        call[n](["warm"])
    lat = {n: [] for n in names}; doc = {n: [] for n in names}; b32 = {n: [] for n in names}
    vecs = {n: [] for n in names}
    for i in range(30):
        order = names[i % len(names):] + names[:i % len(names)]
        for n in order:
            s = time.perf_counter(); call[n]([f"how does failover work when the primary dies {i}"])
            lat[n].append((time.perf_counter() - s) * 1000)
    for i, t in enumerate(texts):
        order = names[i % len(names):] + names[:i % len(names)]
        for n in order:
            s = time.perf_counter(); v = call[n]([t]); doc[n].append((time.perf_counter() - s) * 1000)
            vecs[n].append(v[0])
    for i in range(3):
        for n in names:
            s = time.perf_counter(); call[n](texts[:32]); b32[n].append((time.perf_counter() - s) * 1000)
    res = {}
    for n in names:
        V = np.stack(vecs[n]); cos = (V * stored).sum(1)
        res[n] = {"query_p50": round(med(lat[n]), 1), "query_p95": round(q95(lat[n]), 1),
                  "doc_p50": round(med(doc[n]), 1), "batch32_ms": round(med(b32[n]), 0),
                  "cos_vs_stored_min": round(float(cos.min()), 5),
                  "cos_vs_stored_p50": round(float(np.median(cos)), 6)}
        np.save(out.replace(".json", f".{n}.npy"), V)
    res["mlx"]["peak_gb"] = round(mx.get_peak_memory() / 1e9, 2)

elif role == "rerank":
    yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
    def mlx_score(p):
        logits = model(mx.array([tok.encode(p)]))[0, -1]
        return tok.decode([int(mx.argmax(logits).item())])
    def http_score(kind, port, p):
        if kind == "ollama":
            return post(port, "/api/generate", {"model": RR, "prompt": p, "raw": True, "stream": False,
                        "options": {"temperature": 0, "num_predict": 1, "num_ctx": NCTX}})["response"]
        return post(port, "/completion", {"prompt": p, "n_predict": 1, "temperature": 0,
                                          "cache_prompt": False})["content"]
    call = {"mlx": mlx_score, **{n: (lambda p, k=k, pt=pt: http_score(k, pt, p)) for n, (k, pt) in RT.items()}}
    pairs = json.load(open(src)); names = list(call)
    for n in names:
        call[n](pairs[0]["prompt"])
    lat = {n: [] for n in names}; verdict = {n: [] for n in names}
    for i, pr in enumerate(pairs):
        order = names[i % len(names):] + names[:i % len(names)]
        for n in order:
            s = time.perf_counter(); v = call[n](pr["prompt"])
            lat[n].append((time.perf_counter() - s) * 1000); verdict[n].append(v.strip().lower()[:3])
    from concurrent.futures import ThreadPoolExecutor
    pool4 = {}
    for n in names:
        reps = []
        for r in range(3):
            chunk = pairs[r * 20:(r + 1) * 20]
            s = time.perf_counter()
            if n == "mlx":                                  # MLX is single-stream in-process
                [call[n](p["prompt"]) for p in chunk]
            else:
                with ThreadPoolExecutor(4) as ex:
                    list(ex.map(lambda p: call[n](p["prompt"]), chunk))
            reps.append((time.perf_counter() - s) * 1000)
        pool4[n] = round(med(reps))
    ref = names[1] if len(names) > 1 else names[0]
    res = {}
    for n in names:
        agree = sum(a == b for a, b in zip(verdict[n], verdict[ref]))
        res[n] = {"pair_p50": round(med(lat[n]), 1), "pair_p95": round(q95(lat[n]), 1),
                  "pool20_4way_ms": pool4[n], "yes": verdict[n].count("yes"),
                  "agree_with_" + ref: f"{agree}/{len(pairs)}"}
    res["verdicts"] = verdict
    res["mlx"]["peak_gb"] = round(mx.get_peak_memory() / 1e9, 2)

elif role == "hyde":
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_logits_processors, make_sampler
    greedy = make_sampler(temp=0.0)
    procs = make_logits_processors(presence_penalty=1.5, presence_context_size=64)
    def mlx_gen(p):
        chat = tok.apply_chat_template([{"role": "user", "content": p}], add_generation_prompt=True,
                                       tokenize=False, enable_thinking=False)
        return generate(model, tok, chat, max_tokens=160, sampler=greedy, logits_processors=procs)
    def http_gen(port, p):
        return post(port, "/api/generate", {"model": GEN, "prompt": p, "stream": False, "think": False,
                    "options": {"temperature": 0, "num_predict": 160}})["response"]
    call = {"mlx": mlx_gen, **{n: (lambda p, pt=pt: http_gen(pt, p)) for n, (_, pt) in RT.items()}}
    prompts = json.load(open(src)); names = list(call)
    for n in names:
        call[n](prompts[0])
    lat = {n: [] for n in names}; texts = {n: [] for n in names}
    for i, p in enumerate(prompts):
        order = names[i % len(names):] + names[:i % len(names)]
        for n in order:
            s = time.perf_counter(); t = call[n](p).strip()
            lat[n].append((time.perf_counter() - s) * 1000); texts[n].append(t)
    res = {}
    for n in names:
        ntok = sum(len(tok.encode(t)) for t in texts[n])
        res[n] = {"gen_p50": round(med(lat[n]), 0), "gen_p95": round(q95(lat[n]), 0),
                  "tok_per_s": round(ntok / (sum(lat[n]) / 1000), 1),
                  "identical_to_" + names[1]: sum(a == b for a, b in zip(texts[n], texts[names[1]]))}
    res["texts"] = texts
    res["mlx"]["peak_gb"] = round(mx.get_peak_memory() / 1e9, 2)

import os
res["mlx"]["footprint_gb"] = footprint_gb(f"^{sys.executable} .*interleave.py {role}") or None
o = subprocess.run(["footprint", "-p", str(os.getpid())], capture_output=True, text=True).stdout
mm = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", o)
res["mlx"]["footprint_gb"] = round(float(mm.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1}[mm.group(2)], 2) if mm else None
res["swap"] = swap()
json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k not in ("verdicts", "texts")}, indent=1))
