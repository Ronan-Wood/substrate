#!/bin/bash
# The eval matrix behind README.md's Results: every (corpus, gold set) x (lexical-only, full stack)
# x (unicode61, substrate), one `substrate eval` per cell, stdout to RUNDIR/<corpus>.<mode>.<arm>.txt.
#
#   ./run_matrix.sh IDXDIR RUNDIR CACHE [SETS]
#
# IDXDIR holds build_index.py's output. CACHE is a COPY of the vector cache (HyDE and rerank outputs
# are cached in it, so both arms see identical expansions; never point it at out/vector-cache.db).
# IDXDIR must hold the corpora `fixture`, `vault-a` and `vault-b` (build_index.py NAMEs). SETS defaults
# to "gold id"; pass "id" to re-run only the identifier sets. The identifier gold sets are written
# against the operator's own notes, so they live outside every repository, in $PRIVATE_GOLD. Needs
# Ollama for the full stack, and the engine's environment on PY.
set -euo pipefail
[ $# -ge 3 ] || { echo "usage: $0 IDXDIR RUNDIR CACHE [SETS]" >&2; exit 2; }
SETS=${4:-"gold id"}
mkdir -p "$2"
# Absolute before the cd below, so relative arguments mean what the caller's shell meant.
IDX=$(cd "$1" && pwd)
RUNS=$(cd "$2" && pwd)
CACHE_DIR=$(cd "$(dirname "$3")" && pwd)
CACHE=$CACHE_DIR/$(basename "$3")
[ -f "$CACHE" ] || { echo "no cache at $CACHE" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
ENGINE="$HERE/../../substrate"
PY=${PY:-$ENGINE/.venv/bin/python}
PRIVATE_GOLD=${PRIVATE_GOLD:-$HOME/CodeHome/substrate-eval-private/fts5-tokenizer}
case " $SETS " in
  *" id "*) [ -d "$PRIVATE_GOLD" ] || { echo "no private gold sets at $PRIVATE_GOLD" >&2; exit 2; } ;;
esac
cd "$ENGINE"

cell() { # corpus gold mode arm flags...
  local corpus=$1 gold=$2 mode=$3 arm=$4; shift 4
  local out="$RUNS/$corpus.$mode.$arm.txt"
  local status=0
  "$PY" -I "$HERE/run_eval.py" --db "$IDX/$corpus.$arm.db" --gold "$gold" --cache "$CACHE" "$@" \
    >"$out" 2>&1 || status=$?
  # Exit 1 is both a gate miss (a valid measurement) and an uncaught exception, so a cell counts
  # only if it exited 0 or 1, reached the report's METRICS block, and has no FATAL or traceback.
  if [ "$status" -gt 1 ] || grep -qE '^(FATAL|Traceback)' "$out" || ! grep -q '^  METRICS' "$out"; then
    echo "FAILED: $corpus $mode $arm — see $out" >&2; exit 2
  fi
  echo "  $corpus $mode $arm"
}

LEX=(--no-vector --no-hyde --no-rerank)
FULL=(--cross-encoder)
for arm in unicode61 substrate; do
  for set in $SETS; do
    if [ "$set" = gold ]; then
      cell fixture eval/gold.json lex "$arm" "${LEX[@]}"
      cell fixture eval/gold.json full "$arm" "${FULL[@]}"
      cell vault-a eval/gold-vault.json lex "$arm" "${LEX[@]}"
      cell vault-a eval/gold-vault.json full "$arm" "${FULL[@]}"
    else
      for v in vault-a vault-b; do
        cell "$v" "$PRIVATE_GOLD/gold-identifiers-$v.json" idlex "$arm" "${LEX[@]}"
        cell "$v" "$PRIVATE_GOLD/gold-identifiers-$v.json" idfull "$arm" "${FULL[@]}"
      done
    fi
  done
done
