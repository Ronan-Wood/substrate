#!/bin/bash
# The large private eval set: every scope x (lexical-only, hybrid) x (unicode61, substrate), at k=10,
# with per-case --json-out. Accuracy only: run at low priority, one process at a time.
#
#   ./run_large.sh IDXDIR CACHE [STACKS]
#
# IDXDIR holds build_index.py's arms, named by scope. CACHE is a scratch query-vector cache, shared
# by both arms so the second reuses the first's embeddings. STACKS defaults to "lex hybrid".
# SET=respelled runs make_respelled.py's derived cases instead of the set itself (results named
# <scope>.resp-<stack>.<arm>), and SET=snake its camel→snake cases (<scope>.snk-<stack>.<arm>).
# RESULTS names the results subfolder (default `results`); a run whose JSON already exists there is
# skipped. Cases, logs and per-case JSON carry real queries and note headings, so everything is read
# from and written to $PRIVATE_EVAL (default ~/CodeHome/substrate-eval-private), never this directory.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: $0 IDXDIR CACHE [STACKS]" >&2; exit 2; }
IDX=$(cd "$1" && pwd)
CACHE_DIR=$(cd "$(dirname "$2")" && pwd)
CACHE=$CACHE_DIR/$(basename "$2")
STACKS=${3:-"lex hybrid"}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-$HERE/../../substrate/.venv/bin/python}
PRIVATE_EVAL=${PRIVATE_EVAL:-$HOME/CodeHome/substrate-eval-private}
OUT=$PRIVATE_EVAL/fts5-tokenizer/${RESULTS:-results}
SET=${SET:-main}
# The scopes are the keys of the private labels.json, so no vault is named here.
SCOPES=$(python3 -I -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1]))))' \
  "$PRIVATE_EVAL/fts5-tokenizer/labels.json")
mkdir -p "$OUT"
cd "$HERE"

for stack in $STACKS; do
  case $stack in
    lex) flags=(--no-vector --no-hyde --no-rerank) ;;
    hybrid) flags=(--no-hyde --no-rerank) ;;
    *) echo "unknown stack $stack" >&2; exit 2 ;;
  esac
  for scope in $SCOPES; do
    for arm in unicode61 substrate; do
      if [ "$SET" = respelled ]; then
        run="$scope.resp-$stack.$arm" gold="$PRIVATE_EVAL/fts5-tokenizer/gold-respelled-$scope.json"
      elif [ "$SET" = snake ]; then
        run="$scope.snk-$stack.$arm" gold="$PRIVATE_EVAL/fts5-tokenizer/gold-respelled-snake-$scope.json"
      elif [ "$SET" = main ]; then
        run="$scope.$stack.$arm" gold="$PRIVATE_EVAL/gold/gold-$scope.json"
      else
        echo "unknown SET $SET" >&2; exit 2
      fi
      [ -s "$OUT/$run.json" ] && { echo "  $run (done)"; continue; }
      status=0
      : >"$OUT/$run.json.tmp"
      nice -n 15 "$PY" -I "$HERE/run_eval.py" --db "$IDX/$scope.$arm.db" \
        --gold "$gold" --k 10 --cache "$CACHE" \
        --json-out "$OUT/$run.json.tmp" "${flags[@]}" >"$OUT/$run.log" 2>&1 || status=$?
      if [ "$status" -gt 1 ] || grep -qE '^(FATAL|Traceback)' "$OUT/$run.log" \
          || ! grep -q '^  METRICS' "$OUT/$run.log" || [ ! -s "$OUT/$run.json.tmp" ]; then
        echo "FAILED: $run, see $OUT/$run.log" >&2; exit 2
      fi
      mv "$OUT/$run.json.tmp" "$OUT/$run.json"
      echo "  $run"
    done
  done
done
