#!/usr/bin/env bash
# Benchmark continuously, picking up new commits as they land.
#
# Nothing else runs on this machine, so there is no window to respect.
#
# One commit is measured per iteration rather than `run --all`, so the suite
# is brought up to date between commits. `run --all` only returns once every
# commit has a terminal result, and at two hours a commit against a history
# that grows faster than that, it never did: worker-nuc ran one sweep for
# days on a checkout that predated the change it was meant to publish.
set -uo pipefail

REPO=${AIHC_BENCH_REPO:-/mnt/data/ai-haskell-compiler/benchmarks}
LOG=/tmp/aihc-bench-continuous.log
TZONE=Europe/Copenhagen

# How long to wait, with everything measured, before looking for new commits.
IDLE_SLEEP=900

# What `run` prints when it found nothing left to measure.
CAUGHT_UP='^all commits have terminal results$'

log() { echo "[$(TZ="$TZONE" date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG"; }

# shellcheck source=/dev/null
. "$(dirname "$0")/aihc-bench-lib.sh"

cd "$REPO" || { log "repository missing: $REPO"; exit 1; }
log "started; continuous"

while true; do
  update_suite
  offset=$(wc -c < "$LOG")
  PYTHONUNBUFFERED=1 nix run . -- run --fetch --upload >> "$LOG" 2>&1
  status=$?
  if [ "$status" -ne 0 ]; then
    log "run exited $status"
    sleep 60
  elif tail -c +"$((offset + 1))" "$LOG" | grep -q "$CAUGHT_UP"; then
    log "caught up; waiting ${IDLE_SLEEP}s for new commits"
    sleep "$IDLE_SLEEP"
  fi
done
