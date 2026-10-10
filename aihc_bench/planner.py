"""Bisect first-parent history where benchmark values change the most.

An unmeasured HEAD is selected first, followed by the first eligible commit.
Then the midpoint of the gap with the largest relative change between its
measured endpoints is selected. Gap width and recency only break ties.
Commits with terminal or inherited results are never selected again.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

SIGNAL_METRICS = ("wall_time", "allocated_bytes")


def select_next(
    commits: List[Dict[str, Any]],
    terminal_attempts: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    plan = build_plan(commits, terminal_attempts)
    return plan["next"]


def build_plan(
    commits: List[Dict[str, Any]],
    terminal_attempts: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return the next commit, its selection stage, and the ranked gaps.

    Stages are ``head``, ``first`` and ``signal``. Commits are oldest first.
    """
    measured = {attempt["commit_sha"]: attempt for attempt in terminal_attempts}
    plan: Dict[str, Any] = {"next": None, "stage": None, "gaps": []}
    if not commits:
        return plan

    head = commits[-1]
    if head["sha"] not in measured:
        plan.update(next=head, stage="head")
        return plan

    first = commits[0]
    if first["sha"] not in measured:
        plan.update(next=first, stage="first")
        return plan

    gaps = rank_gaps(commits, measured)
    plan["gaps"] = gaps
    if gaps:
        plan.update(next=gaps[0]["pick"], stage="signal")
    return plan


def rank_gaps(
    commits: List[Dict[str, Any]],
    measured: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank unmeasured runs by endpoint difference, then width, then recency.

    Difference is the strongest absolute log ratio among shared signal
    metrics. Missing or unavailable endpoints carry zero signal. Equal or
    unknown signals fall back to bisection of the widest gap so the planner
    can discover changes even without an initial signal.
    """
    signals = {sha: attempt_signals(attempt) for sha, attempt in measured.items()}
    gaps: List[Dict[str, Any]] = []
    run: List[Dict[str, Any]] = []
    left: Optional[Dict[str, Any]] = None

    def close(right: Optional[Dict[str, Any]]) -> None:
        if not run:
            return
        signal, change = 0.0, None
        if left is not None and right is not None:
            signal, change = strongest_change(signals[left["sha"]], signals[right["sha"]])
        gaps.append(
            {
                "start": run[0],
                "end": run[-1],
                "left": left,
                "right": right,
                "width": len(run),
                "signal": signal,
                "change": change,
                "pick": run[len(run) // 2],
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
    gaps.sort(key=lambda gap: (gap["signal"], gap["width"], gap["pick"]["ordinal"]), reverse=True)
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
    return strongest_change(left, right)[0]


def strongest_change(
    left: Dict[Tuple[str, str, str], float], right: Dict[Tuple[str, str, str], float]
) -> Tuple[float, Optional[Dict[str, Any]]]:
    """The strongest absolute log ratio between two commits, and what it was measured on.

    The second value names the benchmark, configuration and metric and gives
    both estimates, so a pick can be explained; it is None without a change.
    """
    strongest, change = 0.0, None
    for key, value in left.items():
        other = right.get(key)
        if other and value > 0:
            signal = abs(math.log(other / value))
            if signal > strongest:
                strongest = signal
                benchmark, configuration, metric = key
                change = {"benchmark": benchmark, "configuration": configuration, "metric": metric, "left": value, "right": other}
    return strongest, change


def merge_terminal_attempts(attempts_by_experiment: Dict[str, Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Fold per-benchmark attempts into one attempt per fully covered commit.

    Experiments are per benchmark, but the planner reasons about commits: a
    commit counts as measured only when every experiment has a terminal
    attempt for it, so a newly added benchmark makes the whole history
    eligible again and fills in HEAD, then the first commit, then gaps ranked
    by endpoint difference. The merged attempt carries the concatenated
    results under ``result`` so gap signals see every benchmark.
    """
    experiments = dict(attempts_by_experiment)
    if not experiments:
        return []
    per_commit: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for experiment, attempts in experiments.items():
        for attempt in attempts:
            per_commit.setdefault(attempt["commit_sha"], {})[experiment] = attempt
    merged: List[Dict[str, Any]] = []
    for sha, attempts in per_commit.items():
        if len(attempts) != len(experiments):
            continue
        parts = [attempts[experiment] for experiment in experiments]
        envelopes = [_attempt_envelope(part) for part in parts]
        statuses = [part.get("status") for part in parts]
        inherited = [part.get("inherited_from") for part in parts]
        if all(inherited):
            status = "inherited"
        elif any(status in {"complete", "inherited"} for status in statuses):
            status = "complete"
        else:
            status = statuses[0]
        available = any(envelope.get("compiler_status") == "available" for envelope in envelopes)
        merged.append(
            {
                "commit_sha": sha,
                "ordinal": parts[0].get("ordinal"),
                "status": status,
                "inherited_from": inherited[0] if all(inherited) else None,
                "result": {
                    "compiler_status": "available" if available else "unavailable",
                    "results": [result for envelope in envelopes for result in envelope.get("results", [])],
                },
            }
        )
    merged.sort(key=lambda item: (item["ordinal"] is None, item["ordinal"]))
    return merged


def _attempt_envelope(attempt: Dict[str, Any]) -> Dict[str, Any]:
    envelope = attempt.get("result")
    if envelope is None and attempt.get("result_json"):
        try:
            envelope = json.loads(attempt["result_json"])
        except ValueError:
            envelope = None
    return envelope or {}
