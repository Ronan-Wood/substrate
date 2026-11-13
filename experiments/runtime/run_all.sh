#!/bin/bash
# Every timing block, each behind guarded.sh. Versions alternate across two rounds so drift
# cannot favour one.
set -u
cd "$(dirname "$0")"
S="${S:?set S to a scratch folder for inputs and results}"
N=$S/ollama-0.40.14/x/ollama; O=/opt/homebrew/bin/ollama
G=./guarded.sh; I=./instance.sh
P="load_probe.py embed,gen,rr --conc 8"

# 1. Per-request overhead, 0.32.5 against 0.40.14 (client-side factors on each).
$G ovh-r1-0.32 $I $O ovh-r1-0.32 "ollama_overhead.py 12 embed,gen,rr"
$G ovh-r1-0.40 $I $N ovh-r1-0.40 "ollama_overhead.py 12 embed,gen,rr"
$G ovh-r2-0.40 $I $N ovh-r2-0.40 "ollama_overhead.py 12 embed,gen,rr"
$G ovh-r2-0.32 $I $O ovh-r2-0.32 "ollama_overhead.py 12 embed,gen,rr"

# 2. Settings matrix: latency, 8-way concurrency, resident size, per role.
for r in 1 2; do
  $G mx-r$r-0.32-default        $I $O mx-r$r-0.32-default "$P"
  $G mx-r$r-0.40-default        $I $N mx-r$r-0.40-default "$P"
  $G mx-r$r-0.40-ctx4k          $I $N mx-r$r-0.40-ctx4k "$P --num-ctx 4096"
  $G mx-r$r-0.40-np4-ctx4k      $I $N mx-r$r-0.40-np4-ctx4k "$P --num-ctx 4096" OLLAMA_NUM_PARALLEL=4
  $G mx-r$r-0.40-fa-q8-ctx4k    $I $N mx-r$r-0.40-fa-q8-ctx4k "$P --num-ctx 4096" OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0
done
$G mx-r1-0.40-envctx8k $I $N mx-r1-0.40-envctx8k "$P" OLLAMA_CONTEXT_LENGTH=8192
$G q8-parity ./q8_parity.sh

# 3. Runtime alternatives per role (parity comes along: deterministic, re-checked here).
$G cmp-embed  ./runtime_compare.sh embed
$G cmp-rerank ./rerank_runs.sh
$G cmp-hyde   ./hyde_runs.sh

# 4. Full searches under load.
$G load ./load_runs.sh
echo "## ALL DONE"
