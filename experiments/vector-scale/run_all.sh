#!/usr/bin/env bash
# Full sweep, one process per (library, size) so memory figures are not contaminated by the last run.
# Calibration first, then brute force (which also generates and caches the corpora and ground truth
# under data/), then every ANN library at every size, the larger graph at 1M, and the real scopes.
# About 3.5 hours on an M2 Max under load; most of it is building HNSW at 1M.
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
mkdir -p results/raw
: > results/raw/brute.jsonl
: > results/raw/ann.jsonl
: > results/raw/real.jsonl
$PY calibrate.py > results/calibration.txt
for n in 7206 10000 100000 250000 500000 1000000; do
  echo "$(date +%T) brute $n"
  $PY bench_brute.py "$n" > /dev/null
done
for n in 10000 100000 500000 1000000; do
  for lib in faiss usearch usearch_i8 hnswlib sqlite_vec; do
    echo "$(date +%T) $lib $n"
    $PY bench_ann.py "$lib" "$n" > /dev/null
  done
done
# The larger graph at 1M, because M = 16 cannot reach recall 0.95 there.
for lib in faiss usearch; do
  echo "$(date +%T) $lib 1000000 M32"
  $PY bench_ann.py "$lib" 1000000 --m 32 --efc 256 --ef-max 2048 > /dev/null
done
# usearch built single-threaded: its 12-thread build leaves rows unreachable.
for n in 10000 100000; do
  echo "$(date +%T) usearch $n serial build"
  $PY bench_ann.py usearch "$n" --build-threads 1 > /dev/null
done
for scope in vault-a vault-b scripta; do
  for lib in faiss usearch usearch_i8 hnswlib; do
    echo "$(date +%T) $lib $scope"
    $PY bench_ann.py "$lib" --real "$scope" > /dev/null
  done
  echo "$(date +%T) usearch $scope serial build"
  $PY bench_ann.py usearch --real "$scope" --build-threads 1 > /dev/null
done
$PY report.py > results/RESULTS.md
echo "$(date +%T) done"
