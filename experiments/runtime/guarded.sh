#!/bin/bash
# Run one timing block only on a quiet machine, and throw it away if a build/test burst starts
# mid-block.  guarded.sh <label> <command...>
# Quiet = no pytest / swift-build / swift-test / xctest / xcodebuild, and 1-min load < 3.
set -u
LABEL=$1; shift
BURST='pytest|swift-build|swift-test|xctest|xcodebuild'
load1() { sysctl -n vm.loadavg | awk '{print $2}'; }
busy() { pgrep -f "$BURST" >/dev/null || awk -v l="$(load1)" 'BEGIN{exit !(l>=3)}'; }
cond() { echo "   [$1] $(uptime | sed 's/.*up/up/') | $(memory_pressure | tail -1 | sed 's/System-wide memory //') | swap $(sysctl -n vm.swapusage | sed 's/.*used = //;s/ free.*//')"; }
for attempt in 1 2 3; do
  waited=0
  while busy; do sleep 5; waited=$((waited + 5)); [ $waited -ge 1800 ] && { echo "## $LABEL: never quiet in 30 min, skipped"; exit 1; }; done
  echo "## $LABEL (attempt $attempt, waited ${waited}s)"; cond start
  FLAG=$(mktemp); ( while [ -f "$FLAG" ]; do pgrep -f "$BURST" >/dev/null && echo burst >"$FLAG.hit"; sleep 2; done ) &
  MON=$!
  "$@"; rc=$?
  rm -f "$FLAG"; wait $MON 2>/dev/null
  cond end
  if [ -f "$FLAG.hit" ]; then rm -f "$FLAG.hit"; echo "## $LABEL: DISCARDED, a build/test burst started mid-block"; continue; fi
  echo "## $LABEL: kept (rc=$rc)"; exit 0
done
echo "## $LABEL: discarded three times, no clean block"; exit 1
