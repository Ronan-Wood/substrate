#!/bin/bash
# Run one throwaway `ollama serve` on 11435 against the real model store, read-only (NOPRUNE),
# with extra env from the arguments, run a probe against it, then stop it. The launchd daemon on
# 11434 is never touched.
#   instance.sh <ollama-binary> <label> <probe-args> [VAR=value ...]
set -u
BIN=$1; LABEL=$2; PROBE=$3; shift 3
S="${S:?set S to a scratch folder for inputs and results}"
LOG=$S/instance-$LABEL.log
HERE=$(cd "$(dirname "$0")" && pwd)
env -i HOME="$HOME" PATH=/usr/bin:/bin \
  OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="/Volumes/Disk Pro 2 1TB/ollama-models" \
  OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=32768 OLLAMA_DEBUG=1 "$@" \
  "$BIN" serve >"$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null' EXIT
for _ in $(seq 60); do curl -sf http://127.0.0.1:11435/api/version >/dev/null && break; sleep 0.5; done
echo "== $LABEL ($("$BIN" --version 2>/dev/null | tail -1)) env: $*  load: $(uptime | sed 's/.*averages: //')"
set -- $PROBE
SCRIPT=$1; shift
python3 "$HERE/$SCRIPT" 11435 "$@"
curl -s http://127.0.0.1:11435/api/ps | python3 -c "import json,sys; [print('   resident', m['name'], round(m['size']/1e9,1), 'GB', 'ctx', m.get('context_length')) for m in json.load(sys.stdin)['models']]"
