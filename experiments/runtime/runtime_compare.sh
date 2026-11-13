#!/bin/bash
# Start the servers one role needs, run the interleaved comparison, record each server's memory
# (phys_footprint of its whole process tree), stop everything.  runtime_compare.sh embed|rerank|hyde
set -u
ROLE=$1
S="${S:?set S to a scratch folder for inputs and results}"
HERE=$(cd "$(dirname "$0")" && pwd)
STORE="/Volumes/Disk Pro 2 1TB/ollama-models"
EMB_GGUF="$STORE/blobs/sha256-06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
RR_GGUF="$STORE/blobs/sha256-143cbc3a4b2ebbd0b4022931c22a257c1c501cf20eeae0e65b1131183506ce65"
HF=$S/hf/hub
PIDS=(); SPECS=""
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT

ollama_on() {  # binary port label
  env -i HOME="$HOME" PATH=/usr/bin:/bin OLLAMA_HOST=127.0.0.1:$2 OLLAMA_MODELS="$STORE" \
    OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=32768 "$1" serve >"$S/cmp-$ROLE-$3.log" 2>&1 &
  PIDS+=($!); SPECS="$SPECS $3=ollama:$2:$!"
}
llama_on() {   # port label args...
  local port=$1 label=$2; shift 2
  /opt/homebrew/bin/llama-server --host 127.0.0.1 --port "$port" --no-webui "$@" >"$S/cmp-$ROLE-$label.log" 2>&1 &
  PIDS+=($!); SPECS="$SPECS $label=llamacpp:$port:$!"
}
tree_gb() {    # sum phys_footprint over a pid and its descendants
  local all="$1" kids
  kids=$(pgrep -P "$1"); for k in $kids; do all="$all $(pgrep -P "$k") $k"; done
  for p in $all; do footprint -p "$p" 2>/dev/null | grep -oE "Footprint: [0-9.]+ (KB|MB|GB)"; done |
    awk '{v=$2; if($3=="MB")v/=1000; if($3=="KB")v/=1e6; t+=v} END{printf "%.2f GB", t}'
}

N=$S/ollama-0.40.14/x/ollama; O=/opt/homebrew/bin/ollama
case $ROLE in
  embed)
    ollama_on $N 11435 ollama-0.40; ollama_on $O 11436 ollama-0.32
    llama_on 11437 llamacpp -m "$EMB_GGUF" --embedding --pooling last -c 16384 -np 4 -b 2048 -ub 2048 -fa auto
    INPUT=$S/vault-a.db; MDIR=$(ls -d $HF/models--Qwen--Qwen3-Embedding-0.6B/snapshots/*) ;;
  rerank)
    ollama_on $N 11435 ollama-0.40
    llama_on 11437 llamacpp -m "$RR_GGUF" -c 16384 -np 4 -b 2048 -ub 2048 -fa auto
    INPUT=$S/parity/pairs.json; MDIR=$(ls -d $HF/models--mlx-community--Qwen3-Reranker-4B-mxfp8/snapshots/*) ;;
  hyde)
    ollama_on $N 11435 ollama-0.40; ollama_on $O 11436 ollama-0.32
    INPUT=$S/parity/hyde_prompts.json; MDIR=$S/qwen35-4b-mlx ;;
esac
for spec in $SPECS; do port=${spec#*:}; port=${port%%:*}
  for _ in $(seq 120); do curl -sf -o /dev/null "http://127.0.0.1:$port/" 2>/dev/null && break
    curl -sf -o /dev/null "http://127.0.0.1:$port/health" 2>/dev/null && break; sleep 0.5; done
done
ARGS=""; for s in $SPECS; do ARGS="$ARGS ${s%:*}"; done
echo "== $ROLE  load: $(uptime | sed 's/.*averages: //')  swap: $(sysctl -n vm.swapusage | sed 's/.*used = //;s/ free.*//')"
$S/mlxenv/bin/python "$HERE/interleave.py" "$ROLE" "$INPUT" "$MDIR" "$S/cmp-$ROLE.json" $ARGS
for s in $SPECS; do echo "   footprint ${s%%=*}: $(tree_gb "${s##*:}")"; done
