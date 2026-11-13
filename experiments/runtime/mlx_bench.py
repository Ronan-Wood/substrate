"""The three substrate model roles run in-process on MLX: latency, memory, and the outputs for parity.

  mlx_bench.py embed  <hf_dir> <index.db> <out.json>
  mlx_bench.py rerank <hf_dir> <pairs.json> <out.json>
  mlx_bench.py hyde   <mlx_dir> <queries.json> <out.json>

Inputs are read at run time from scratch copies; nothing from a vault is written into this file.
"""
import json, sqlite3, statistics, struct, subprocess, sys, time

import mlx.core as mx

mx.set_cache_limit(1 << 30)   # an uncapped buffer cache grew to 16 GB in an earlier run

role, model_dir, src, out = sys.argv[1:5]
m = statistics.median
q95 = lambda xs: sorted(xs)[int(0.95 * (len(xs) - 1))]

def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
                          ).stdout.split("used = ")[1].split()[0]

def mem():
    return {"mlx_active_gb": round(mx.get_active_memory() / 1e9, 2),
            "mlx_peak_gb": round(mx.get_peak_memory() / 1e9, 2), "swap": swap()}

res = {"role": role}
t0 = time.perf_counter()

if role == "embed":
    from mlx_lm import load
    model, tok = load(model_dir)
    inner = model.model
    res["load_s"] = round(time.perf_counter() - t0, 2)

    def embed(texts):
        # Last-token pooling, L2-normalised: Qwen3-Embedding's definition, and what Ollama's GGUF
        # declares (pooling_type 3 = LAST). Inputs end with the EOS token, as the model card says.
        ids = [tok.encode(t) + [tok.eos_token_id] for t in texts]
        L = max(map(len, ids))
        pad = tok.pad_token_id or 0
        arr = mx.array([[pad] * (L - len(x)) + x for x in ids])          # LEFT pad → last = EOS
        if len(texts) > 1:
            pos = mx.arange(L)[None, :] >= mx.array([L - len(x) for x in ids])[:, None]
            mask = mx.where(pos[:, None, None, :], 0.0, -1e9).astype(mx.bfloat16)
            causal = mx.triu(mx.full((L, L), -1e9), k=1).astype(mx.bfloat16)
            h = inner(arr, mask=mask + causal)
        else:
            h = inner(arr)
        v = h[:, -1, :].astype(mx.float32)
        v = v / mx.linalg.norm(v, axis=-1, keepdims=True)
        mx.eval(v)
        return v

    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    rows = db.execute("SELECT c.chunk_id, c.text, v.vector FROM chunks c JOIN chunk_vectors v "
                      "USING (chunk_id) WHERE v.embed_model='qwen3-embedding:0.6b#raw' "
                      "ORDER BY c.chunk_id LIMIT 200").fetchall()
    embed(["warm up"])
    lat = []
    for i in range(20):
        s = time.perf_counter(); embed([f"how does failover work when the primary dies {i}"])
        lat.append((time.perf_counter() - s) * 1000)
    res["query_ms_p50"], res["query_ms_p95"] = round(m(lat), 1), round(q95(lat), 1)
    texts = [r[1] for r in rows]
    s = time.perf_counter(); single = [embed([t])[0] for t in texts[:64]]
    res["doc_serial_ms_per_doc"] = round((time.perf_counter() - s) * 1000 / 64, 1)
    s = time.perf_counter()
    batched = [embed(texts[i:i + 8]) for i in range(0, 64, 8)]
    res["doc_batch8_ms_per_doc"] = round((time.perf_counter() - s) * 1000 / 64, 1)
    cos = []
    for (cid, text, blob), v in zip(rows[:64], single):
        stored = mx.array(struct.unpack(f"{len(blob) // 4}f", blob))
        cos.append(float((stored * v).sum()))
    bcos = [float((a * b).sum()) for a, b in zip(single, mx.concatenate(batched))]
    res["cos_vs_stored_min"], res["cos_vs_stored_p50"] = round(min(cos), 5), round(m(cos), 5)
    res["cos_batch_vs_serial_min"] = round(min(bcos), 5)
    res.update(mem())

elif role == "rerank":
    from mlx_lm import load
    model, tok = load(model_dir)
    res["load_s"] = round(time.perf_counter() - t0, 2)
    yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
    pairs = json.load(open(src))           # [{"prompt": <substrate TEMPLATE, filled>}, ...]
    def score(p):
        logits = model(mx.array([tok.encode(p)]))[0, -1]
        top = int(mx.argmax(logits).item())
        return tok.decode([top]), float(logits[yes] - logits[no])
    score(pairs[0]["prompt"])
    lat, verdicts = [], []
    for p in pairs:
        s = time.perf_counter(); tokstr, margin = score(p["prompt"])
        lat.append((time.perf_counter() - s) * 1000); verdicts.append([tokstr, round(margin, 3)])
    res["pair_ms_p50"], res["pair_ms_p95"] = round(m(lat), 1), round(q95(lat), 1)
    res["pool20_serial_ms"] = round(m(lat) * 20)
    res["verdicts"] = verdicts
    res.update(mem())

elif role == "hyde":
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler
    model, tok = load(model_dir)
    res["load_s"] = round(time.perf_counter() - t0, 2)
    prompts = json.load(open(src))          # substrate's HyDE prompt, already filled
    from mlx_lm.sample_utils import make_logits_processors
    greedy = make_sampler(temp=0.0)
    # The Ollama model's own params carry presence_penalty 1.5, which still applies under
    # substrate's temperature 0. Ollama's penalty window is repeat_last_n (64).
    procs = make_logits_processors(presence_penalty=1.5, presence_context_size=64)
    def gen(p):
        chat = tok.apply_chat_template([{"role": "user", "content": p}], add_generation_prompt=True,
                                       tokenize=False, enable_thinking=False)
        return generate(model, tok, chat, max_tokens=160, sampler=greedy, logits_processors=procs)
    gen(prompts[0])
    lat, texts = [], []
    for p in prompts:
        s = time.perf_counter(); t = gen(p); lat.append((time.perf_counter() - s) * 1000)
        texts.append(t)
    toks = [len(tok.encode(t)) for t in texts]
    res["gen_ms_p50"], res["gen_ms_p95"] = round(m(lat), 1), round(q95(lat), 1)
    res["tok_per_s"] = round(sum(toks) / (sum(lat) / 1000), 1)
    res["texts"] = texts
    res.update(mem())

json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k not in ("verdicts", "texts")}))
