#!/usr/bin/env bash
# Benchmark only while this machine's CI runners are idle.
#
# The machine hosts eight self-hosted GitHub Actions runners. A CI job landing
# mid-measurement competes for the whole machine, and compile time and wall
# time both move -- indistinguishable, afterwards, from a real regression. So
# measurement is confined to the window when CI is quiet.
#
# One commit is measured per iteration rather than `run --all`, so the window
# is re-checked between commits instead of once at the start. A commit started
# just before the window closes is allowed to finish past it.
set -uo pipefail

REPO=${AIHC_BENCH_REPO:-$HOME/coding/ai-haskell-compiler/benchmarks}
LOG=/tmp/aihc-bench-window.log
TZONE=Europe/Copenhagen

# The quiet window, in TZONE hours. Wraps midnight.
START_HOUR=22
END_HOUR=6

# How long to wait before looking at the clock again, outside the window.
IDLE_SLEEP=600

log() { echo "[$(TZ="$TZONE" date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG"; }

# shellcheck source=/dev/null
. "$(dirname "$0")/aihc-bench-lib.sh"

in_window() {
  local hour
  hour=$(TZ="$TZONE" date +%-H)
  [ "$hour" -ge "$START_HOUR" ] || [ "$hour" -lt "$END_HOUR" ]
}

cd "$REPO" || { log "repository missing: $REPO"; exit 1; }
log "started; window ${START_HOUR}:00-${END_HOUR}:00 $TZONE"

while true; do
  if in_window; then
    log "measuring one commit"
    update_suite
    PYTHONUNBUFFERED=1 nix run . -- run --fetch --upload >> "$LOG" 2>&1
    status=$?
    [ "$status" -ne 0 ] && log "run exited $status"
    sleep 5
  else
    log "outside the window; sleeping"
    sleep "$IDLE_SLEEP"
  fi
done
