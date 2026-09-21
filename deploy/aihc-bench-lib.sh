# shellcheck shell=bash
# Shared by the continuous and window services. Both source this after
# defining REPO, LOG and log().

# Fast-forward the suite checkout to origin/main between commits.
#
# The suite is what defines the experiments, so a worker running an old
# checkout quietly publishes a different suite from its sibling, and nothing
# in the results says so: worker-nuc spent a day four commits behind while
# worker-desktop was current, and it took reading both machines to notice.
# A benchmark checkout only ever reads, so origin is https and this needs no
# credentials in the service's environment.
#
# --ff-only, never a merge: a checkout with local commits is a machine
# somebody is working on, and it is left alone and said so.
update_suite() {
  local before after
  before=$(git -C "$REPO" rev-parse HEAD 2>/dev/null) || return 0
  if ! git -C "$REPO" fetch --quiet origin main 2>>"$LOG"; then
    log "could not reach origin; measuring with the checkout as it is"
    return 0
  fi
  after=$(git -C "$REPO" rev-parse origin/main)
  [ "$before" = "$after" ] && return 0
  if git -C "$REPO" merge --ff-only --quiet origin/main 2>>"$LOG"; then
    log "suite updated ${before:0:12} -> ${after:0:12}"
  else
    log "cannot fast-forward to ${after:0:12}; leaving the checkout alone"
  fi
}
