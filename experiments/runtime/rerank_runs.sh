#!/bin/bash
# Rerank, one runtime resident at a time (three 4B models side by side page each other out on this
# 32 GB machine). Alternating order over two rounds; footprint taken while each is loaded.
set -u
S="${S:?set S to a scratch folder for inputs and results}"
HERE=$(cd "$(dirname "$0")" && pwd)
STORE="/Volumes/Disk Pro 2 1TB/ollama-models"
RR_GGUF="$STORE/blobs/sha256-143cbc3a4b2ebbd0b4022931c22a257c1c501cf20eeae0e65b1131183506ce65"
RRMLX=$(ls -d $S/hf/hub/models--mlx-community--Qwen3-Reranker-4B-mxfp8/snapshots/*)
PAIRS=$S/parity/pairs.json
fp() { local t=0; for p in "$@" $(for q in "$@"; do pgrep -P "$q"; done); do
         footprint -p "$p" 2>/dev/null | grep -oE "Footprint: [0-9.]+ (KB|MB|GB)"; done |
       awk '{v=$2; if($3=="MB")v/=1000; if($3=="KB")v/=1e6; t+=v} END{printf "%.2f GB", t}'; }
wait_up() { for _ in $(seq 120); do curl -sf -o /dev/null "http://127.0.0.1:$1/$2" && return; sleep 0.5; done; }

ollama_run() {  # binary label
  env -i HOME="$HOME" PATH=/usr/bin:/bin OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="$STORE" \
    OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=32768 "$1" serve >"$S/rr-$2.log" 2>&1 &
  local pid=$!; wait_up 11435 api/version
  NUM_CTX=${3:-} $S/mlxenv/bin/python "$HERE/http_bench.py" ollama 11435 rerank "$PAIRS" "$S/rr-$2.json"
  echo "   $2 footprint $(fp $pid)  swap $(sysctl -n vm.swapusage | sed 's/.*used = //;s/ free.*//')"
  kill $pid; wait $pid 2>/dev/null
}
llama_run() {
  /opt/homebrew/bin/llama-server --host 127.0.0.1 --port 11437 --no-webui -m "$RR_GGUF" \
    -c 16384 -np 4 -b 2048 -ub 2048 -fa auto >"$S/rr-$1.log" 2>&1 &
  local pid=$!; wait_up 11437 health
  $S/mlxenv/bin/python "$HERE/http_bench.py" llamacpp 11437 rerank "$PAIRS" "$S/rr-$1.json"
  echo "   $1 footprint $(fp $pid)  swap $(sysctl -n vm.swapusage | sed 's/.*used = //;s/ free.*//')"
  kill $pid; wait $pid 2>/dev/null
}
mlx_run() {
  $S/mlxenv/bin/python "$HERE/mlx_bench.py" rerank "$RRMLX" "$PAIRS" "$S/rr-$1.json"
}
N=$S/ollama-0.40.14/x/ollama; O=/opt/homebrew/bin/ollama
echo "== rerank  load: $(uptime | sed 's/.*averages: //')"
ollama_run $O r1-ollama-0.32; ollama_run $N r1-ollama-0.40-ctx4k 4096; llama_run r1-llamacpp; mlx_run r1-mlx
mlx_run r2-mlx; llama_run r2-llamacpp; ollama_run $N r2-ollama-0.40-ctx4k 4096; ollama_run $O r2-ollama-0.32
