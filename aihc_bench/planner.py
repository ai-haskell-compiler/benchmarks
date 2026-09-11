"""Commit selection for the overnight runner.

Selection proceeds in three stages over the first-parent history:

1. An unmeasured HEAD is always first.
2. While any of the newest ``warmup`` commits is unmeasured, the newest of
   those is selected, so recent history fills in densely before anything else.
3. Every maximal run of unmeasured commits between measured neighbours is a
   gap. Gaps are scored by width, by the change observed between their
   measured endpoints, and by recency; the midpoint of the best gap is
   selected. Even spacing gives coverage, and the signal term localizes
   regressions to the commit that caused them.

Commits that inherit a result from a same-tree neighbour count as measured
and are never selected.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

SIGNAL_METRICS = ("wall_time", "allocated_bytes")
SIGNAL_WEIGHT = 8.0
DEFAULT_WARMUP = 20


def select_next(
    commits: List[Dict[str, Any]],
    terminal_attempts: Iterable[Dict[str, Any]],
    warmup: int = DEFAULT_WARMUP,
) -> Optional[Dict[str, Any]]:
    plan = build_plan(commits, terminal_attempts, warmup)
    return plan["next"]


def build_plan(
    commits: List[Dict[str, Any]],
    terminal_attempts: Iterable[Dict[str, Any]],
    warmup: int = DEFAULT_WARMUP,
) -> Dict[str, Any]:
    """Return the next commit, the stage that chose it, and the ranked gaps."""
    attempts = list(terminal_attempts)
    measured = {attempt["commit_sha"]: attempt for attempt in attempts}
    plan: Dict[str, Any] = {"next": None, "stage": None, "gaps": []}
    if not commits:
        return plan
    unmeasured = [commit for commit in commits if commit["sha"] not in measured]
    if not unmeasured:
        return plan

    head = commits[-1]
    if head["sha"] not in measured:
        plan.update(next=head, stage="head")
        return plan

    recent = [commit for commit in commits[-warmup:] if commit["sha"] not in measured]
    if recent:
        plan.update(next=recent[-1], stage="warmup")
        return plan

    gaps = rank_gaps(commits, measured)
    plan["gaps"] = gaps
    if gaps:
        plan.update(next=gaps[0]["pick"], stage="gap")
    return plan


def rank_gaps(commits: List[Dict[str, Any]], measured: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    head_ordinal = max(commits[-1]["ordinal"], 1)
    signals = {sha: attempt_signals(attempt) for sha, attempt in measured.items()}
    gaps: List[Dict[str, Any]] = []
    run: List[Dict[str, Any]] = []
    left: Optional[Dict[str, Any]] = None

    def close(right: Optional[Dict[str, Any]]) -> None:
        if not run:
            return
        signal = 0.0
        if left is not None and right is not None:
            signal = signal_between(signals[left["sha"]], signals[right["sha"]])
        width = len(run)
        midpoint = run[len(run) // 2]
        recency = midpoint["ordinal"] / head_ordinal
        score = width * (1.0 + SIGNAL_WEIGHT * signal) * (1.0 + recency)
        gaps.append(
            {
                "start": run[0],
                "end": run[-1],
                "left": left,
                "right": right,
                "width": width,
                "signal": signal,
                "recency": recency,
                "score": score,
                "pick": midpoint,
            }
        )

    for commit in commits:
        if commit["sha"] in measured:
            close(commit)
            run = []
            left = commit
        else:
            run.append(commit)
    close(None)
    gaps.sort(key=lambda gap: (gap["score"], gap["pick"]["ordinal"]), reverse=True)
    return gaps


def attempt_signals(attempt: Dict[str, Any]) -> Dict[Tuple[str, str, str], float]:
    """Estimates of the signal metrics keyed by benchmark, configuration and metric."""
    if attempt.get("status") not in {"complete", "inherited"}:
        return {}
    envelope = attempt.get("result")
    if envelope is None:
        raw = attempt.get("result_json")
        if not raw:
            return {}
        try:
            envelope = json.loads(raw)
        except ValueError:
            return {}
    if envelope.get("compiler_status") != "available":
        return {}
    values: Dict[Tuple[str, str, str], float] = {}
    for result in envelope.get("results", []):
        for metric in result.get("measurement", {}).get("metrics", []):
            if metric["metric"] in SIGNAL_METRICS and metric.get("estimate"):
                values[(result["benchmark"], result["configuration"], metric["metric"])] = float(metric["estimate"])
    return values


def signal_between(left: Dict[Tuple[str, str, str], float], right: Dict[Tuple[str, str, str], float]) -> float:
    strongest = 0.0
    for key, value in left.items():
        other = right.get(key)
        if other and value > 0:
            strongest = max(strongest, abs(math.log(other / value)))
    return strongest
