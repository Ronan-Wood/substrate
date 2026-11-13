#!/bin/bash
# HyDE, one runtime resident at a time, alternating over two rounds. No llama.cpp row: the only
# copy of these weights on disk is Ollama's MLX (nvfp4) one, and there is no GGUF of them.
set -u
S="${S:?set S to a scratch folder for inputs and results}"
HERE=$(cd "$(dirname "$0")" && pwd)
STORE="/Volumes/Disk Pro 2 1TB/ollama-models"
HMLX=$S/qwen35-4b-mlx
PROMPTS=$S/parity/hyde_prompts.json
fp() { for p in "$@" $(for q in "$@"; do pgrep -P "$q"; done); do
         footprint -p "$p" 2>/dev/null | grep -oE "Footprint: [0-9.]+ (KB|MB|GB)"; done |
       awk '{v=$2; if($3=="MB")v/=1000; if($3=="KB")v/=1e6; t+=v} END{printf "%.2f GB", t}'; }
wait_up() { for _ in $(seq 120); do curl -sf -o /dev/null "http://127.0.0.1:$1/$2" && return; sleep 0.5; done; }
ollama_run() {  # binary label
  env -i HOME="$HOME" PATH=/usr/bin:/bin OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="$STORE" \
    OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=32768 "$1" serve >"$S/hy-$2.log" 2>&1 &
  local pid=$!; wait_up 11435 api/version
  $S/mlxenv/bin/python "$HERE/http_bench.py" ollama 11435 hyde "$PROMPTS" "$S/hy-$2.json"
  echo "   $2 footprint $(fp $pid)  swap $(sysctl -n vm.swapusage | sed 's/.*used = //;s/ free.*//')"
  kill $pid; wait $pid 2>/dev/null
}
mlx_run() { $S/mlxenv/bin/python "$HERE/mlx_bench.py" hyde "$HMLX" "$PROMPTS" "$S/hy-$1.json"; }
N=$S/ollama-0.40.14/x/ollama; O=/opt/homebrew/bin/ollama
echo "== hyde  load: $(uptime | sed 's/.*averages: //')"
ollama_run $O r1-ollama-0.32; ollama_run $N r1-ollama-0.40; mlx_run r1-mlx
mlx_run r2-mlx; ollama_run $N r2-ollama-0.40; ollama_run $O r2-ollama-0.32
