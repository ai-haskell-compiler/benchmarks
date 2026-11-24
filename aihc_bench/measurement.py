from __future__ import annotations

import statistics
from dataclasses import dataclass
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
    ("gc_max_pause", "ns", "gc_max_pause_ns", False),
    ("instructions", "count", "instructions", False),
    ("cycles", "count", "cycles", False),
)
INVOCATION_METRIC_NAMES = frozenset(name for name, _unit, _field, _deterministic in INVOCATION_METRICS)
RUNTIME_STATS_METRICS = {"peak_heap", "allocated_bytes", "gc_count", "gc_time", "gc_max_pause"}


#: Metrics whose estimate is the lowest sample rather than the median. The
#: programs are deterministic and measured one at a time, so noise in these
#: only ever adds: another process, a cold cache or a lowered clock makes a
#: run slower, never faster, and the quickest run is the closest to what the
#: program itself costs. Peak memory and the longest GC pause are not costs
#: that interference inflates the same way, so they keep the median.
FASTEST_METRICS = frozenset({"wall_time", "cpu_time", "gc_time", "instructions", "cycles"})

#: Statuses a measurement that ran to completion can carry. Every run now
#: ends ``measured``; the others name how the adaptive bucket escalation
#: that preceded it ended, and are kept so envelopes recorded before the
#: change still read as measured.
MEASURED_STATUSES = frozenset({"measured", "converged", "nonconverged", "budget"})


@dataclass(frozen=True)
class OutputFile:
    """A file the program writes, which every run must reproduce exactly.

    Most programs print their result, and ``expected_stdout`` checks it. A
    program measured as it is released, such as ``mhs``, writes its result to
    a file named on its command line instead. The runner deletes the file
    before each run and compares the SHA-256 of what the run wrote.
    """

    path: Path
    sha256: str

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def digest(self) -> Optional[str]:
        """The SHA-256 of the file, or ``None`` when the run did not write it."""
        try:
            return sha256_bytes(self.path.read_bytes())
        except FileNotFoundError:
            return None


def run_status(sample: ProcessMeasurement, expected_stdout: bytes, output_file: Optional[OutputFile] = None) -> Optional[str]:
    """Why a run does not count, or ``None`` when it does."""
    if sample.timed_out:
        return "timed_out"
    if sample.exit_code != 0:
        return "run_failed"
    if sample.stdout != expected_stdout:
        return "validation_failed"
    if output_file is not None and output_file.digest() != output_file.sha256:
        return "validation_failed"
    return None


def measure(
    command: Iterable[str],
    cwd: Path,
    expected_stdout: bytes,
    timeout_seconds: float,
    runs: int,
    invoke: Callable[[Iterable[str], Path, float], ProcessMeasurement] = run_measured,
    output_file: Optional[OutputFile] = None,
) -> Dict[str, Any]:
    """Run the program ``runs`` times and keep every sample.

    A fixed count makes a cell cost a known multiple of one invocation, so
    the time a commit takes is known before it starts. The estimate for the
    time-like metrics is the quickest run (see ``FASTEST_METRICS``), which
    needs no convergence test to be stable.
    """
    samples: List[Dict[str, Any]] = []
    output_hash = sha256_bytes(expected_stdout)
    stats_error: Optional[str] = None

    for _ in range(runs):
        if output_file is not None:
            output_file.clear()
        sample = invoke(command, cwd, timeout_seconds)
        status = run_status(sample, expected_stdout, output_file)
        if status:
            failure = _failure(status, samples, sample)
            if output_file is not None:
                failure["actual_output_file_sha256"] = output_file.digest()
            return failure
        stats_error = stats_error or sample.stats_error
        samples.append(_sample_record(sample))

    result: Dict[str, Any] = {
        "status": "measured",
        "output_sha256": output_hash,
        **({"output_file_sha256": output_file.sha256} if output_file is not None else {}),
        "samples": samples,
        "metrics": [_metric(name, unit, field, deterministic, samples) for name, unit, field, deterministic in INVOCATION_METRICS],
    }
    if stats_error:
        result["stats_error"] = stats_error
    return result


def _sample_record(sample: ProcessMeasurement) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "wall_time_ns": sample.wall_time_ns,
        "cpu_time_ns": sample.cpu_time_ns,
        "peak_rss_bytes": sample.peak_rss_bytes,
    }
    for field in STATS_FIELDS:
        record[field] = sample.runtime_stats.get(field) if sample.runtime_stats else None
    record["instructions"] = sample.instructions
    record["cycles"] = sample.cycles
    return record


def _metric(name: str, unit: str, field: str, deterministic: bool, every: List[Dict[str, Any]]) -> Dict[str, Any]:
    samples = [item[field] for item in every]
    if any(value is None for value in samples):
        return {"metric": name, "unit": unit, "status": "unavailable", "estimate": None, "samples": []}
    estimate = min(samples) if name in FASTEST_METRICS else round(statistics.median(samples))
    metric: Dict[str, Any] = {
        "metric": name,
        "unit": unit,
        "status": "ok",
        "estimate": estimate,
        "samples": samples,
    }
    if deterministic and len(set(samples)) > 1:
        metric["status"] = "nondeterministic"
    return metric


def _failure(status: str, samples: List[Dict[str, Any]], sample: ProcessMeasurement) -> Dict[str, Any]:
    return {
        "status": status,
        "samples": samples,
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
