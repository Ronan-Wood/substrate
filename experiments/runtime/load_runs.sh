#!/bin/bash
# Full substrate searches under load: today's stack (Ollama 0.32.5, Modelfile contexts) against the
# candidate (Ollama 0.40.14 + per-request num_ctx 4096), each on its own instance on 11435.
set -u
S="${S:?set S to a scratch folder for inputs and results}"
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
STORE="/Volumes/Disk Pro 2 1TB/ollama-models"
fp() { for p in "$@" $(for q in "$@"; do pgrep -P "$q"; done); do
         footprint -p "$p" 2>/dev/null | grep -oE "Footprint: [0-9.]+ (KB|MB|GB)"; done |
       awk '{v=$2; if($3=="MB")v/=1000; if($3=="KB")v/=1e6; t+=v} END{printf "%.2f GB", t}'; }
run() {  # binary label num_ctx
  local bin=$1 label=$2 nctx=$3
  env -i HOME="$HOME" PATH=/usr/bin:/bin OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="$STORE" \
    OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=32768 "$bin" serve >"$S/load-$label.log" 2>&1 &
  local pid=$!
  for _ in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1:11435/api/version && break; sleep 0.5; done
  echo "== $label  load: $(uptime | sed 's/.*averages: //')"
  for cfg in "1 0" "4 0" "4 1"; do
    read -r conc bg <<<"$cfg"
    NUM_CTX=$nctx PYTHONPATH="$REPO/substrate" "$REPO/substrate/.venv/bin/python" "$HERE/load_search.py" \
      11435 "$S/vault-a.db" "$S/parity/load_queries.json" "$S" "$conc" "$bg"
  done 2>&1 | grep -v Warning
  echo "   footprint (all three models resident): $(fp $pid)"
  kill $pid; wait $pid 2>/dev/null
}
run /opt/homebrew/bin/ollama current-0.32 ""
run "$S/ollama-0.40.14/x/ollama" candidate-0.40-ctx4k 4096
