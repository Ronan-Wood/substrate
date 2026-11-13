"""Where Ollama's per-request time goes, one factor at a time. Read-only against the live daemon.

Every row reports client wall plus Ollama's own total/load/prompt_eval durations, medians over N
requests after a discarded warm-up, so a reload triggered by an option change is not counted.
"""
import http.client, json, statistics, subprocess, sys, time, urllib.request

HOST, PORT = "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 11434
N = int(sys.argv[2]) if len(sys.argv) > 2 else 15
EMB = "qwen3-embedding:0.6b"
GEN = "qwen3.5:4b-mlx"
RR = "dengcao/Qwen3-Reranker-4B:Q4_K_M"
TEXT = ("Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query: what happens when the primary node fails and a replica must take over")
DOC = ("When the leader fails, one of the followers must be promoted to be the new leader, "
       "clients reconfigured to send writes to it, and the other followers must start consuming "
       "changes from the new leader. This process is called failover. ") * 6

ns = lambda d, k: d.get(k, 0) / 1e6

def swap():
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    return out.split("used = ")[1].split()[0]

def fresh(path, body):
    req = urllib.request.Request(f"http://{HOST}:{PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    s = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return (time.perf_counter() - s) * 1000, d

_conn = [None]
def reuse(path, body):
    if _conn[0] is None:
        _conn[0] = http.client.HTTPConnection(HOST, PORT, timeout=120)
    s = time.perf_counter()
    _conn[0].request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    d = json.loads(_conn[0].getresponse().read())
    return (time.perf_counter() - s) * 1000, d

def row(label, path, body, send=fresh, n=N):
    send(path, body)                                      # warm-up / absorb any reload
    walls, tot, load, pe = [], [], [], []
    for _ in range(n):
        w, d = send(path, body)
        walls.append(w); tot.append(ns(d, "total_duration")); load.append(ns(d, "load_duration"))
        pe.append(ns(d, "prompt_eval_duration"))
    m = statistics.median
    q = lambda xs: sorted(xs)[int(0.95 * (len(xs) - 1))]
    print(f"{label:<52} wall {m(walls):6.1f} (p95 {q(walls):6.1f})  total {m(tot):6.1f}  "
          f"load {m(load):6.1f}  prompt_eval {m(pe):6.1f}  swap {swap()}", flush=True)
    return d

which = sys.argv[3] if len(sys.argv) > 3 else "embed,gen,rr"
if "embed" in which:
    e = {"model": EMB, "input": [TEXT]}
    row("embed /api/embed baseline", "/api/embed", e)
    row("embed keep_alive=30m", "/api/embed", {**e, "keep_alive": "30m"})
    row("embed keep_alive=-1", "/api/embed", {**e, "keep_alive": -1})
    row("embed persistent HTTP connection", "/api/embed", e, send=reuse)
    row("embed truncate=false", "/api/embed", {**e, "truncate": False})
    row("embed /api/embeddings (legacy)", "/api/embeddings", {"model": EMB, "prompt": TEXT})
    for b in (8, 32):
        d = row(f"embed batch of {b} docs (whole request)", "/api/embed",
                {"model": EMB, "input": [DOC + str(i) for i in range(b)]}, n=5)
    row("embed one doc (~1.5k chars)", "/api/embed", {"model": EMB, "input": [DOC]})
    row("embed num_ctx=32768 stated", "/api/embed", {**e, "options": {"num_ctx": 32768}})
    row("embed num_ctx=8192 (reloads once)", "/api/embed", {**e, "options": {"num_ctx": 8192}})
    row("embed back to baseline (reloads once)", "/api/embed", e)
if "gen" in which:
    g = {"model": GEN, "stream": False, "think": False, "prompt": "Say OK.",
         "options": {"temperature": 0, "num_predict": 1}}
    row("generate (HyDE model) 1 token", "/api/generate", g, n=8)
    row("generate 1 token, persistent conn", "/api/generate", g, send=reuse, n=8)
if "rr" in which:
    r = {"model": RR, "raw": True, "stream": False, "prompt": "Judge.\nQ: failover\nD: " + DOC[:800] +
         "\nA:", "options": {"temperature": 0, "num_predict": 1}}
    row("rerank score (1 token)", "/api/generate", r, n=8)
    row("rerank score, persistent conn", "/api/generate", r, send=reuse, n=8)
