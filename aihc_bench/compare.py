"""Ad-hoc comparison of two AIHC builds.

``compare`` builds two compilers, compiles the selected benchmarks with both,
and then measures them in interleaved rounds (A, B, A, B, ...) so slow drift of
the machine, such as thermal throttling, affects both sides equally. The
report gives the median of each side, the ratio B/A, and a bootstrap
confidence interval on that ratio. Results are stored locally in
``adhoc_runs`` and are never uploaded.
"""

from __future__ import annotations

import json
import random
import statistics
import subprocess
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .git_history import GitError, create_worktree, remove_worktree
from .measurement import INVOCATION_METRICS, _sample_record
from .process import ProcessMeasurement, run_measured
from .runner import Cell, build_cells, build_compiler, compile_cells, _prepare_aihc_store
from .schema import new_run_id, utc_now

BOOTSTRAP_ITERATIONS = 1000
CONFIDENCE = 0.95


class CompareError(RuntimeError):
    pass


@dataclass
class Side:
    label: str
    sha: Optional[str]
    worktree: Path
    owned: bool


def resolve_side(repository: Path, ref: str, cache: Path) -> Side:
    """Resolve a Git ref to a detached worktree under ``cache``."""
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise CompareError(f"cannot resolve {ref!r}: {error.output.strip()}") from error
    return Side(label=ref if ref != sha else sha[:12], sha=sha, worktree=cache / sha[:12], owned=True)


def worktree_side(path: Path) -> Side:
    path = path.expanduser().resolve()
    if not (path / "flake.nix").is_file():
        raise CompareError(f"{path} does not look like an AIHC checkout")
    return Side(label="worktree", sha=None, worktree=path, owned=False)


def select_configuration(
    config: Dict[str, Any],
    *,
    benchmarks: Iterable[str] = (),
    configurations: Iterable[str] = (),
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Narrow the suite to what a comparison should compile and run.

    Without an explicit configuration list only AIHC configurations are kept,
    since GHC does not change between two AIHC commits.
    """
    wanted_benchmarks = set(benchmarks)
    wanted_configurations = set(configurations)
    selected = dict(config)
    selected["benchmarks"] = [item for item in config["benchmarks"] if not wanted_benchmarks or item["id"] in wanted_benchmarks]
    selected["configurations"] = [
        item
        for item in config["configurations"]
        if (item["id"] in wanted_configurations if wanted_configurations else item["compiler_family"] == "aihc")
        and (profile is None or item["optimization"] == profile)
    ]
    missing = wanted_benchmarks - {item["id"] for item in selected["benchmarks"]}
    if missing:
        raise CompareError(f"unknown benchmarks: {', '.join(sorted(missing))}")
    missing = wanted_configurations - {item["id"] for item in selected["configurations"]}
    if missing:
        raise CompareError(f"unknown configurations: {', '.join(sorted(missing))}")
    if not selected["benchmarks"] or not selected["configurations"]:
        raise CompareError("nothing to compare after filtering")
    return selected


def prepare_side(
    side: Side,
    config: Dict[str, Any],
    platform_id: str,
    root: Path,
    aihc_repository: Path,
    log: Callable[[str], None],
) -> List[Tuple[Cell, Dict[str, Any]]]:
    """Build the compiler for one side and compile every selected cell."""
    timeout = float(config["measurement"]["compile_timeout_seconds"])
    if side.owned and side.sha:
        create_worktree(aihc_repository, side.worktree, side.sha)
    log(f"[{side.label}] building the compiler")
    error = build_compiler(side.worktree, root, timeout)
    if error is not None:
        raise CompareError(f"[{side.label}] the compiler does not build:\n{error[-2000:]}")
    store = root / ".cache" / "compare-stores" / _side_key(side)
    setup_errors = _prepare_aihc_store(config, platform_id, side.worktree, root, store, timeout)
    for target, detail in setup_errors.items():
        log(f"[{side.label}] runtime for {target} unavailable: {detail.splitlines()[0]}")
    cells = build_cells(
        config,
        platform_id,
        {"sha": _side_key(side)},
        side.worktree,
        root,
        {benchmark["id"]: "compare" for benchmark in config["benchmarks"]},
        aihc_store=store,
        aihc_setup_errors=setup_errors,
    )
    log(f"[{side.label}] compiling {len(cells)} cells")
    compiled = compile_cells(cells, root, timeout)
    for cell, outcome in compiled:
        if outcome["status"] != "compiled":
            log(f"[{side.label}] {cell.benchmark['id']} / {cell.configuration['id']}: {outcome['status']} {outcome.get('reason', '')}".rstrip())
    return compiled


def _side_key(side: Side) -> str:
    if side.sha:
        return side.sha
    return "worktree-" + str(abs(hash(str(side.worktree))) % 10**8)


def measure_interleaved(
    sides: Tuple[List[Tuple[Cell, Dict[str, Any]]], List[Tuple[Cell, Dict[str, Any]]]],
    config: Dict[str, Any],
    root: Path,
    rounds: int,
    invoke: Callable[..., ProcessMeasurement] = run_measured,
    log: Callable[[str], None] = print,
) -> List[Dict[str, Any]]:
    """Run every cell of both sides ``rounds`` times, alternating sides per cell."""
    timeout = float(config["measurement"]["process_timeout_seconds"])
    keyed: List[Dict[Tuple[str, str], Tuple[Cell, Dict[str, Any]]]] = [
        {(cell.benchmark["id"], cell.configuration["id"]): (cell, outcome) for cell, outcome in compiled} for compiled in sides
    ]
    keys = sorted(set(keyed[0]) | set(keyed[1]))
    results: List[Dict[str, Any]] = []
    for key in keys:
        pair = [keyed[index].get(key) for index in range(2)]
        entry: Dict[str, Any] = {
            "benchmark": key[0],
            "configuration": key[1],
            "sides": [{"status": "missing", "samples": [], "compile": None} for _ in range(2)],
        }
        for index, item in enumerate(pair):
            if item is None:
                continue
            cell, outcome = item
            entry.setdefault("compiler_family", cell.configuration["compiler_family"])
            entry.setdefault("backend", cell.configuration["backend"])
            entry.setdefault("optimization", cell.configuration["optimization"])
            entry["sides"][index]["compile"] = outcome
            entry["sides"][index]["status"] = "ok" if outcome["status"] == "compiled" else outcome.get("reason", outcome["status"])
        results.append(entry)

    runnable = [entry for entry in results if all(side["status"] == "ok" for side in entry["sides"])]
    # Round 0 is a warm-up whose samples are discarded: the first invocation of
    # a fresh binary pays for page-cache and code-signing work that later ones
    # do not, and it would otherwise dominate a short comparison.
    for round_number in range(-1, rounds):
        for entry in runnable:
            for index in range(2):
                cell, _ = keyed[index][(entry["benchmark"], entry["configuration"])]
                sample = invoke(
                    cell.run_command or [],
                    root,
                    timeout,
                    environment_overrides=cell.run_environment,
                    stats_file=cell.stats_file,
                    stats_format=cell.stats_format,
                )
                side = entry["sides"][index]
                if sample.timed_out or sample.exit_code != 0 or sample.stdout != cell.benchmark["expected_stdout"].encode("utf-8"):
                    side["status"] = "timed_out" if sample.timed_out else ("run_failed" if sample.exit_code != 0 else "validation_failed")
                    side["stderr"] = sample.stderr[-2000:].decode("utf-8", errors="replace")
                    continue
                if round_number >= 0:
                    side["samples"].append(_sample_record(sample))
        log("warm-up complete" if round_number < 0 else f"round {round_number + 1}/{rounds} complete")
    for entry in results:
        entry["metrics"] = summarize(entry["sides"][0]["samples"], entry["sides"][1]["samples"])
    return results


def summarize(a_samples: List[Dict[str, Any]], b_samples: List[Dict[str, Any]], seed: int = 0) -> List[Dict[str, Any]]:
    """Median per side, the ratio B/A of medians, and a bootstrap interval on that ratio."""
    metrics = []
    for name, unit, field, _deterministic in INVOCATION_METRICS:
        a_values = [sample[field] for sample in a_samples if sample.get(field) is not None]
        b_values = [sample[field] for sample in b_samples if sample.get(field) is not None]
        if not a_values or not b_values:
            metrics.append({"metric": name, "unit": unit, "a": None, "b": None, "ratio": None, "ci": None, "significant": False})
            continue
        a_median = statistics.median(a_values)
        b_median = statistics.median(b_values)
        ratio = b_median / a_median if a_median else None
        interval = bootstrap_ratio(a_values, b_values, seed=seed) if ratio is not None else None
        metrics.append(
            {
                "metric": name,
                "unit": unit,
                "a": a_median,
                "b": b_median,
                "ratio": ratio,
                "ci": interval,
                "significant": bool(interval and (interval[0] > 1.0 or interval[1] < 1.0)),
                "samples": [len(a_values), len(b_values)],
            }
        )
    return metrics


def bootstrap_ratio(a_values: List[float], b_values: List[float], iterations: int = BOOTSTRAP_ITERATIONS, seed: int = 0) -> Optional[Tuple[float, float]]:
    generator = random.Random(seed)
    ratios = []
    for _ in range(iterations):
        a_sample = statistics.median(generator.choices(a_values, k=len(a_values)))
        b_sample = statistics.median(generator.choices(b_values, k=len(b_values)))
        if a_sample:
            ratios.append(b_sample / a_sample)
    if not ratios:
        return None
    ratios.sort()
    lower = ratios[int((1 - CONFIDENCE) / 2 * (len(ratios) - 1))]
    upper = ratios[int((1 + CONFIDENCE) / 2 * (len(ratios) - 1))]
    return (lower, upper)


def run_compare(
    *,
    config: Dict[str, Any],
    platform_id: str,
    root: Path,
    aihc_repository: Path,
    sides: Tuple[Side, Side],
    rounds: int,
    invoke: Callable[..., ProcessMeasurement] = run_measured,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    compiled: List[List[Tuple[Cell, Dict[str, Any]]]] = []
    try:
        for side in sides:
            compiled.append(prepare_side(side, config, platform_id, root, aihc_repository, log))
        results = measure_interleaved((compiled[0], compiled[1]), config, root, rounds, invoke=invoke, log=log)
    finally:
        for side in sides:
            if side.owned:
                remove_worktree(aihc_repository, side.worktree)
    return {
        "id": new_run_id(),
        "created_at": utc_now(),
        "platform": platform_id,
        "rounds": rounds,
        "sides": [{"label": side.label, "sha": side.sha, "worktree": str(side.worktree)} for side in sides],
        "results": results,
    }


# ---------------------------------------------------------------------------
# Formatting

def _format_metric_value(value: Optional[float], unit: str) -> str:
    if value is None:
        return "—"
    if unit == "ns":
        if value >= 1e9:
            return f"{value / 1e9:.3f} s"
        if value >= 1e6:
            return f"{value / 1e6:.2f} ms"
        return f"{value / 1e3:.1f} µs"
    if unit == "byte":
        if value >= 1024 ** 2:
            return f"{value / 1024 ** 2:.1f} MiB"
        return f"{value / 1024:.1f} KiB"
    return f"{value:,.0f}"


def _format_change(ratio: Optional[float]) -> str:
    if ratio is None:
        return "—"
    return f"{(ratio - 1) * 100:+.1f}%"


def _format_interval(interval: Optional[Tuple[float, float]]) -> str:
    if not interval:
        return "—"
    return f"[{(interval[0] - 1) * 100:+.1f}%, {(interval[1] - 1) * 100:+.1f}%]"


def format_report(report: Dict[str, Any], markdown: bool = False, metrics: Iterable[str] = ("wall_time", "cpu_time", "peak_rss", "peak_heap", "allocated_bytes")) -> str:
    a_label, b_label = (side["label"] for side in report["sides"])
    wanted = list(metrics)
    lines: List[str] = []
    header = f"A = {a_label}{_sha_suffix(report['sides'][0])}, B = {b_label}{_sha_suffix(report['sides'][1])}; {report['rounds']} interleaved rounds; change is B relative to A, lower is better; * marks a 95% interval that excludes zero."
    if markdown:
        lines.append(header)
        lines.append("")
        lines.append("| Benchmark | Configuration | Metric | A | B | Change | 95% CI |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    else:
        lines.append(header)
    for entry in report["results"]:
        problems = [f"{label}: {side['status']}" for label, side in zip((a_label, b_label), entry["sides"]) if side["status"] != "ok"]
        if not markdown:
            lines.append("")
            lines.append(f"{entry['benchmark']} / {entry['configuration']}" + (f"  ({'; '.join(problems)})" if problems else ""))
            if not problems:
                lines.append(f"  {'metric':16} {'A':>12} {'B':>12} {'change':>9}  95% CI")
        for metric in entry["metrics"]:
            if metric["metric"] not in wanted or metric["a"] is None:
                continue
            change = _format_change(metric["ratio"]) + ("*" if metric["significant"] else "")
            if markdown:
                lines.append(
                    f"| {entry['benchmark']} | `{entry['configuration']}` | {metric['metric']} | {_format_metric_value(metric['a'], metric['unit'])} | "
                    f"{_format_metric_value(metric['b'], metric['unit'])} | {change} | {_format_interval(metric['ci'])} |"
                )
            else:
                lines.append(
                    f"  {metric['metric']:16} {_format_metric_value(metric['a'], metric['unit']):>12} {_format_metric_value(metric['b'], metric['unit']):>12} "
                    f"{change:>9}  {_format_interval(metric['ci'])}"
                )
        if markdown and problems:
            lines.append(f"| {entry['benchmark']} | `{entry['configuration']}` | — | — | — | — | {'; '.join(problems)} |")
    return "\n".join(lines)


def _sha_suffix(side: Dict[str, Any]) -> str:
    return f" ({side['sha'][:12]})" if side.get("sha") and not side["label"].startswith(side["sha"][:12]) else ""
