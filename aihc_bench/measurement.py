from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .process import ProcessMeasurement, run_measured
from .schema import sha256_bytes
from .stats import STATS_FIELDS

# Metrics measured from every invocation, in publication order. Each entry is
# (metric name, unit, sample field, deterministic). Deterministic metrics are
# expected to be identical across invocations; disagreement is recorded.
INVOCATION_METRICS = (
    ("wall_time", "ns", "wall_time_ns", False),
    ("cpu_time", "ns", "cpu_time_ns", False),
    ("peak_rss", "byte", "peak_rss_bytes", False),
    ("peak_heap", "byte", "peak_heap_bytes", False),
    ("allocated_bytes", "byte", "allocated_bytes", True),
    ("gc_count", "count", "gc_count", True),
    ("gc_time", "ns", "gc_time_ns", False),
)
RUNTIME_STATS_METRICS = {"peak_heap", "allocated_bytes", "gc_count", "gc_time"}


def relative_difference(left: float, right: float) -> float:
    midpoint = (left + right) / 2.0
    if midpoint == 0:
        return 0.0 if left == right else float("inf")
    return abs(left - right) / midpoint


def measure_adaptively(
    command: Iterable[str],
    cwd: Path,
    expected_stdout: bytes,
    timeout_seconds: float,
    relative_threshold: float,
    maximum_bucket_size: int,
    invoke: Callable[[Iterable[str], Path, float], ProcessMeasurement] = run_measured,
) -> Dict[str, Any]:
    buckets: List[List[Dict[str, Any]]] = []
    bucket_size = 1
    output_hash = sha256_bytes(expected_stdout)
    stats_error: Optional[str] = None

    while bucket_size <= maximum_bucket_size:
        bucket: List[Dict[str, Any]] = []
        for _ in range(bucket_size):
            sample = invoke(command, cwd, timeout_seconds)
            if sample.timed_out:
                return _failure("timed_out", buckets, bucket, sample)
            if sample.exit_code != 0:
                return _failure("run_failed", buckets, bucket, sample)
            if sample.stdout != expected_stdout:
                return _failure("validation_failed", buckets, bucket, sample)
            stats_error = stats_error or sample.stats_error
            bucket.append(_sample_record(sample))
        buckets.append(bucket)

        if len(buckets) >= 2:
            previous_mean = statistics.fmean(item["wall_time_ns"] for item in buckets[-2])
            current_mean = statistics.fmean(item["wall_time_ns"] for item in buckets[-1])
            difference = relative_difference(previous_mean, current_mean)
            if difference <= relative_threshold:
                return _success("converged", buckets, output_hash, difference, stats_error)

        bucket_size *= 2

    return _success("nonconverged", buckets, output_hash, None, stats_error)


def _sample_record(sample: ProcessMeasurement) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "wall_time_ns": sample.wall_time_ns,
        "cpu_time_ns": sample.cpu_time_ns,
        "peak_rss_bytes": sample.peak_rss_bytes,
    }
    for field in STATS_FIELDS:
        record[field] = sample.runtime_stats.get(field) if sample.runtime_stats else None
    return record


def _success(
    status: str,
    buckets: List[List[Dict[str, Any]]],
    output_hash: str,
    difference: Any,
    stats_error: Optional[str],
) -> Dict[str, Any]:
    stable = [item for bucket in buckets[-2:] for item in bucket]
    every = [item for bucket in buckets for item in bucket]
    result: Dict[str, Any] = {
        "status": status,
        "output_sha256": output_hash,
        "bucket_sizes": [len(bucket) for bucket in buckets],
        "relative_difference": difference,
        "samples": every,
        "metrics": [_metric(name, unit, field, deterministic, stable, every) for name, unit, field, deterministic in INVOCATION_METRICS],
    }
    if stats_error:
        result["stats_error"] = stats_error
    return result


def _metric(
    name: str,
    unit: str,
    field: str,
    deterministic: bool,
    stable: List[Dict[str, Any]],
    every: List[Dict[str, Any]],
) -> Dict[str, Any]:
    samples = [item[field] for item in every]
    stable_samples = [item[field] for item in stable]
    if any(value is None for value in samples):
        return {"metric": name, "unit": unit, "status": "unavailable", "estimate": None, "samples": []}
    metric: Dict[str, Any] = {
        "metric": name,
        "unit": unit,
        "status": "ok",
        "estimate": round(statistics.median(stable_samples)),
        "samples": samples,
    }
    if deterministic and len(set(samples)) > 1:
        metric["status"] = "nondeterministic"
    return metric


def _failure(
    status: str,
    completed_buckets: List[List[Dict[str, Any]]],
    current_bucket: List[Dict[str, Any]],
    sample: ProcessMeasurement,
) -> Dict[str, Any]:
    return {
        "status": status,
        "bucket_sizes": [len(bucket) for bucket in completed_buckets] + ([len(current_bucket)] if current_bucket else []),
        "samples": [item for bucket in completed_buckets for item in bucket] + current_bucket,
        "exit_code": sample.exit_code,
        "actual_stdout_sha256": sha256_bytes(sample.stdout),
        "stderr": sample.stderr[-8192:].decode("utf-8", errors="replace"),
    }


def compile_metrics(compile_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Metrics describing the compilation, appended to the measurement metrics."""
    wall_time = compile_result.get("wall_time_ns")
    artifact_bytes = compile_result.get("artifact_bytes")
    return [
        {
            "metric": "compile_time",
            "unit": "ns",
            "status": "ok" if wall_time is not None else "unavailable",
            "estimate": wall_time,
            "samples": [wall_time] if wall_time is not None else [],
        },
        {
            "metric": "artifact_size",
            "unit": "byte",
            "status": "ok" if artifact_bytes is not None else "unavailable",
            "estimate": artifact_bytes,
            "samples": [artifact_bytes] if artifact_bytes is not None else [],
        },
    ]
