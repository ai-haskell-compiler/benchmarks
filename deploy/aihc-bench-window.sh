#!/usr/bin/env bash
# Benchmark only while this machine's CI runners are idle.
#
# The machine hosts eight self-hosted GitHub Actions runners. A CI job landing
# mid-measurement competes for the whole machine, and compile time and wall
# time both move -- indistinguishable, afterwards, from a real regression. So
# measurement is confined to the window when CI is quiet.
#
# One commit is measured per iteration rather than `run --all`, so the window
# is re-checked between commits instead of once at the start.
set -uo pipefail

REPO=${AIHC_BENCH_REPO:-$HOME/coding/ai-haskell-compiler/benchmarks}
LOG=/tmp/aihc-bench-window.log
TZONE=Europe/Copenhagen

# The quiet window, in TZONE hours. Wraps midnight.
START_HOUR=22
END_HOUR=8

# Do not start a commit that would likely still be running when CI returns.
# What a commit costs is a property of the suite and the machine, and it moves
# whenever either does: it was 15 minutes when this script was written and 51
# on the same machine once two benchmarks were added. A fixed margin built
# from the first number lets the service start a commit it cannot finish,
# which is the contention the window exists to avoid -- so the margin is read
# back from what the commits here actually took.
MARGIN_FLOOR=25
MARGIN_DEFAULT=90
MARGIN_SAMPLES=5

# How long to wait before looking at the clock again, outside the window.
IDLE_SLEEP=600

log() { echo "[$(TZ="$TZONE" date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG"; }

# shellcheck source=/dev/null
. "$(dirname "$0")/aihc-bench-lib.sh"

# The longest of the last MARGIN_SAMPLES commits, plus a quarter -- the
# longest rather than the median because overrunning the window costs a whole
# commit's worth of contended numbers, while waiting costs one idle hour.
margin_minutes() {
  local longest
  longest=$(grep -oE '^commit took [0-9]+ min' "$LOG" 2>/dev/null |
    grep -oE '[0-9]+' | tail -n "$MARGIN_SAMPLES" | sort -n | tail -n 1)
  if [ -z "$longest" ]; then
    echo "$MARGIN_DEFAULT"
    return
  fi
  longest=$(( longest * 5 / 4 ))
  [ "$longest" -lt "$MARGIN_FLOOR" ] && longest=$MARGIN_FLOOR
  echo "$longest"
}

minutes_left() {
  local now_h now_m now start end
  now_h=$(TZ="$TZONE" date +%-H)
  now_m=$(TZ="$TZONE" date +%-M)
  now=$(( now_h * 60 + now_m ))
  start=$(( START_HOUR * 60 ))
  end=$(( END_HOUR * 60 ))
  if [ "$now" -ge "$start" ]; then
    echo $(( 24 * 60 - now + end ))   # after the start, before midnight
  elif [ "$now" -lt "$end" ]; then
    echo $(( end - now ))             # after midnight, before the end
  else
    echo 0                            # outside the window
  fi
}

cd "$REPO" || { log "repository missing: $REPO"; exit 1; }
log "started; window ${START_HOUR}:00-${END_HOUR}:00 $TZONE"

while true; do
  left=$(minutes_left)
  margin=$(margin_minutes)
  if [ "$left" -ge "$margin" ]; then
    log "measuring one commit (${left}m left in the window, ${margin}m needed)"
    update_suite
    PYTHONUNBUFFERED=1 nix run . -- run --fetch --upload >> "$LOG" 2>&1
    status=$?
    [ "$status" -ne 0 ] && log "run exited $status"
    sleep 5
  else
    if [ "$left" -eq 0 ]; then
      log "outside the window; sleeping"
    else
      log "only ${left}m left in the window, ${margin}m needed; not starting another commit"
    fi
    sleep "$IDLE_SLEEP"
  fi
done
