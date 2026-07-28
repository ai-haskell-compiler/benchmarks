from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

from .process import ProcessMeasurement, run_measured
from .schema import sha256_bytes


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
    buckets: List[List[Dict[str, int]]] = []
    bucket_size = 1
    output_hash = sha256_bytes(expected_stdout)

    while bucket_size <= maximum_bucket_size:
        bucket: List[Dict[str, int]] = []
        for _ in range(bucket_size):
            sample = invoke(command, cwd, timeout_seconds)
            if sample.timed_out:
                return _failure("timed_out", buckets, bucket, sample)
            if sample.exit_code != 0:
                return _failure("run_failed", buckets, bucket, sample)
            if sample.stdout != expected_stdout:
                return _failure("validation_failed", buckets, bucket, sample)
            bucket.append(
                {
                    "wall_time_ns": sample.wall_time_ns,
                    "peak_rss_bytes": sample.peak_rss_bytes,
                }
            )
        buckets.append(bucket)

        if len(buckets) >= 2:
            previous_mean = statistics.fmean(item["wall_time_ns"] for item in buckets[-2])
            current_mean = statistics.fmean(item["wall_time_ns"] for item in buckets[-1])
            difference = relative_difference(previous_mean, current_mean)
            if difference <= relative_threshold:
                return _success("converged", buckets, output_hash, difference)

        bucket_size *= 2

    return _success("nonconverged", buckets, output_hash, None)


def _success(status: str, buckets: List[List[Dict[str, int]]], output_hash: str, difference: Any) -> Dict[str, Any]:
    stable = [item for bucket in buckets[-2:] for item in bucket]
    wall_samples = [item["wall_time_ns"] for item in stable]
    rss_samples = [item["peak_rss_bytes"] for item in stable]
    return {
        "status": status,
        "output_sha256": output_hash,
        "bucket_sizes": [len(bucket) for bucket in buckets],
        "relative_difference": difference,
        "samples": [item for bucket in buckets for item in bucket],
        "metrics": [
            {
                "metric": "wall_time",
                "unit": "ns",
                "estimate": round(statistics.fmean(wall_samples)),
                "samples": [item["wall_time_ns"] for bucket in buckets for item in bucket],
            },
            {
                "metric": "peak_rss",
                "unit": "byte",
                "estimate": round(statistics.median(rss_samples)),
                "samples": [item["peak_rss_bytes"] for bucket in buckets for item in bucket],
            },
        ],
    }


def _failure(
    status: str,
    completed_buckets: List[List[Dict[str, int]]],
    current_bucket: List[Dict[str, int]],
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
