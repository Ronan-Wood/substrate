"""Latency, concurrency and resident memory per model role against one Ollama-compatible host.

  load_probe.py <port> <roles: embed,gen,rr> [--conc 8]
Each role: single-request median/p95, then CONC concurrent requests (throughput + per-request p95),
then the daemon's resident size for that model and the machine's swap.
"""
import json, statistics, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

PORT = int(sys.argv[1]); ROLES = sys.argv[2].split(",")
CONC = int(sys.argv[sys.argv.index("--conc") + 1]) if "--conc" in sys.argv else 8
NCTX = int(sys.argv[sys.argv.index("--num-ctx") + 1]) if "--num-ctx" in sys.argv else None
OPTS = {"num_ctx": NCTX} if NCTX else {}
URL = f"http://127.0.0.1:{PORT}"
MODELS = {"embed": "qwen3-embedding:0.6b", "gen": "qwen3.5:4b-mlx",
          "rr": "dengcao/Qwen3-Reranker-4B:Q4_K_M"}
DOC = ("When the leader fails, one of the followers must be promoted to be the new leader, clients "
       "reconfigured to send writes to it, and the other followers start consuming changes from "
       "the new leader. This process is called failover. ") * 4
HYDE = ("Write one short factual paragraph, in precise technical language, that would appear in a "
        "reference book and would directly answer this question. Use the domain's standard "
        "terminology. Do not preamble, do not hedge, do not mention the question.\n\nQuestion: "
        "how does a database recover after its primary node crashes {i}\n\nParagraph:")

def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    s = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return (time.perf_counter() - s) * 1000, d

def call(role, i):
    if role == "embed":
        return post("/api/embed", {"model": MODELS[role], "input": [f"Instruct: retrieve\nQuery: failover {i}"], **({"options": OPTS} if OPTS else {})})[0]
    if role == "gen":
        return post("/api/generate", {"model": MODELS[role], "stream": False, "think": False,
                    "prompt": HYDE.format(i=i), "options": {"temperature": 0, "num_predict": 160, **OPTS}})[0]
    return post("/api/generate", {"model": MODELS[role], "raw": True, "stream": False,
                "prompt": f"Judge yes/no.\nQ: failover {i}\nD: {DOC}\nA:",
                "options": {"temperature": 0, "num_predict": 1, **OPTS}})[0]

def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
                          ).stdout.split("used = ")[1].split()[0]

def q95(xs):
    return sorted(xs)[int(0.95 * (len(xs) - 1))]

n = [0]
def nxt():
    n[0] += 1
    return n[0]

for role in ROLES:
    call(role, nxt())
    reps = 12 if role != "gen" else 5
    single = [call(role, nxt()) for _ in range(reps)]
    total = CONC * (3 if role != "gen" else 1)
    with ThreadPoolExecutor(CONC) as ex:
        s = time.perf_counter()
        each = list(ex.map(lambda _: call(role, nxt()), range(total)))
        wall = time.perf_counter() - s
    _, ps = post("/api/ps", {}) if False else (0, json.loads(urllib.request.urlopen(URL + "/api/ps").read()))
    size = next((round(m["size"] / 1e9, 1) for m in ps["models"] if m["name"] == MODELS[role]), None)
    print(f"{role:<6} single p50 {statistics.median(single):7.1f} p95 {q95(single):7.1f} ms | "
          f"{CONC}-way: {total / wall:6.2f} req/s, per-req p50 {statistics.median(each):7.1f} "
          f"p95 {q95(each):7.1f} ms | resident {size} GB | swap {swap()}", flush=True)
