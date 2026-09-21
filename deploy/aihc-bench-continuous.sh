#!/usr/bin/env bash
# Benchmark continuously, picking up new commits as they land.
#
# Nothing else runs on this machine, so there is no window to respect.
# `run --all --fetch` measures until every commit has a terminal result and
# then exits; the sleep-and-repeat is what turns that into "keep going as new
# commits arrive".
set -uo pipefail

REPO=${AIHC_BENCH_REPO:-/mnt/data/ai-haskell-compiler/benchmarks}
LOG=/tmp/aihc-bench-continuous.log
TZONE=Europe/Copenhagen

# How long to wait, with everything measured, before looking for new commits.
IDLE_SLEEP=900

log() { echo "[$(TZ="$TZONE" date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG"; }

# shellcheck source=/dev/null
. "$(dirname "$0")/aihc-bench-lib.sh"

cd "$REPO" || { log "repository missing: $REPO"; exit 1; }
log "started; continuous"

while true; do
  update_suite
  PYTHONUNBUFFERED=1 nix run . -- run --all --fetch --upload >> "$LOG" 2>&1
  status=$?
  [ "$status" -ne 0 ] && log "run exited $status"
  log "caught up; waiting ${IDLE_SLEEP}s for new commits"
  sleep "$IDLE_SLEEP"
done
