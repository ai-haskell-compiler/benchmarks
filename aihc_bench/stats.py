"""Parsers for runtime statistics written by benchmark processes."""

from __future__ import annotations

import ast
import json
from typing import Any, Dict, Optional

STATS_FORMATS = ("ghc", "aihc")
STATS_FIELDS = ("peak_heap_bytes", "allocated_bytes", "gc_count", "gc_time_ns")


class StatsError(ValueError):
    pass


def parse_runtime_stats(text: str, stats_format: str) -> Dict[str, int]:
    if stats_format == "ghc":
        return parse_ghc_machine_readable(text)
    if stats_format == "aihc":
        return parse_aihc_json(text)
    raise StatsError(f"unknown runtime statistics format: {stats_format}")


def parse_ghc_machine_readable(text: str) -> Dict[str, int]:
    """Parse the output of ``+RTS -t<file> --machine-readable``.

    The file starts with the echoed command line, followed by a Haskell list of
    string pairs. ``max_live_bytes`` is comparable to the AIHC peak heap, since
    both count live data after collection.
    """
    start = text.find("[(")
    if start < 0:
        raise StatsError("GHC statistics file does not contain a pair list")
    try:
        pairs = ast.literal_eval(text[start:].strip())
        values = {str(key): str(value) for key, value in pairs}
    except (ValueError, SyntaxError, TypeError) as error:
        raise StatsError(f"GHC statistics are not a pair list: {error}") from error
    try:
        return {
            "peak_heap_bytes": int(values["max_live_bytes"]),
            "allocated_bytes": int(values["allocated_bytes"]),
            "gc_count": int(values["num_GCs"]),
            "gc_time_ns": round(float(values["GC_cpu_seconds"]) * 1_000_000_000),
        }
    except KeyError as error:
        raise StatsError(f"GHC statistics lack {error.args[0]}") from error


def parse_aihc_json(text: str) -> Dict[str, int]:
    """Parse the JSON object written by the AIHC runtime through ``AIHC_RTS_STATS``."""
    try:
        record: Any = json.loads(text)
    except ValueError as error:
        raise StatsError(f"AIHC statistics are not JSON: {error}") from error
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise StatsError("AIHC statistics must be a schema 1 object")
    try:
        return {field: int(record[field]) for field in STATS_FIELDS}
    except (KeyError, TypeError, ValueError) as error:
        raise StatsError(f"AIHC statistics lack a valid field: {error}") from error


def read_stats_file(path: Optional[str], stats_format: Optional[str]) -> Optional[Dict[str, int]]:
    """Return parsed statistics, or None when the process did not write any."""
    if not path or not stats_format:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    if not text.strip():
        return None
    return parse_runtime_stats(text, stats_format)
