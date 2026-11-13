#!/bin/bash
# Does OLLAMA_KV_CACHE_TYPE=q8_0 (with flash attention) keep embed vectors and rerank verdicts?
set -u
S="${S:?set S to a scratch folder for inputs and results}"
HERE=$(cd "$(dirname "$0")" && pwd)
env -i HOME="$HOME" PATH=/usr/bin:/bin OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="/Volumes/Disk Pro 2 1TB/ollama-models" \
  OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=32768 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 \
  "$S/ollama-0.40.14/x/ollama" serve >"$S/q8.log" 2>&1 &
P=$!; for _ in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1:11435/api/version && break; sleep 0.5; done
NUM_CTX=4096 $S/mlxenv/bin/python "$HERE/http_bench.py" ollama 11435 embed "$S/vault-a.db" "$S/q8-embed.json"
NUM_CTX=4096 $S/mlxenv/bin/python "$HERE/http_bench.py" ollama 11435 rerank "$S/parity/pairs.json" "$S/q8-rr.json"
kill $P; wait $P 2>/dev/null
