from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import SCHEMA_VERSION
from .schema import utc_now


def build_bundle(
    envelopes: Iterable[Dict[str, Any]],
    destination: Path,
    public_base_url: Optional[str] = None,
    object_prefix: str = "",
) -> Tuple[Dict[str, Any], List[Tuple[Path, str, str]]]:
    destination.mkdir(parents=True, exist_ok=True)
    base_url = (public_base_url or destination.resolve().as_uri()).rstrip("/")
    prefix = object_prefix.strip("/")
    uploads: List[Tuple[Path, str, str]] = []
    envelope_list = list(envelopes)

    grouped: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    revisions: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    experiments = sorted({envelope["experiment_id"] for envelope in envelope_list})

    for envelope in envelope_list:
        experiment = envelope["experiment_id"]
        platform_id = envelope["platform"]
        commit = envelope["aihc_commit"]
        revisions[(experiment, platform_id)].append(
            {
                "run_id": envelope["run_id"],
                "created_at": envelope["created_at"],
                "commit": commit,
                "compiler_status": envelope["compiler_status"],
                "unavailable_reason": envelope.get("unavailable_reason"),
                "environment": envelope["environment"],
                "outcomes": [
                    {
                        "benchmark": result["benchmark"],
                        "configuration": result["configuration"],
                        "compiler_family": result["compiler_family"],
                        "compiler_version": result["compiler_version"],
                        "compiler_variant": result.get(
                            "compiler_variant", "gmp" if result["compiler_family"] == "ghc" else "default"
                        ),
                        "backend": result["backend"],
                        "gc": result["gc"],
                        "compile_status": result.get("compile", {}).get("status"),
                        "measurement_status": result.get("measurement", {}).get("status"),
                    }
                    for result in envelope["results"]
                ],
            }
        )
        for result in envelope["results"]:
            for metric in result.get("measurement", {}).get("metrics", []):
                grouped[(experiment, platform_id, result["benchmark"], metric["metric"])].append(
                    {
                        "run_id": envelope["run_id"],
                        "created_at": envelope["created_at"],
                        "commit": commit,
                        "environment_id": envelope["environment"]["id"],
                        "configuration": result["configuration"],
                        "compiler_family": result["compiler_family"],
                        "compiler_version": result["compiler_version"],
                        "compiler_variant": result.get(
                            "compiler_variant", "gmp" if result["compiler_family"] == "ghc" else "default"
                        ),
                        "backend": result["backend"],
                        "gc": result["gc"],
                        "optimization": result["optimization"],
                        "status": result["measurement"]["status"],
                        "unit": metric["unit"],
                        "estimate": metric["estimate"],
                    }
                )

    series_entries: List[Dict[str, Any]] = []
    revision_entries: List[Dict[str, Any]] = []
    for (experiment, platform_id, benchmark, metric), points in sorted(grouped.items()):
        points.sort(key=lambda point: (point["commit"]["ordinal"], point["configuration"]))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment,
            "platform": platform_id,
            "benchmark": benchmark,
            "metric": metric,
            "points": points,
        }
        local, key, url = _write_content_object(destination, base_url, prefix, "views", payload)
        uploads.append((local, key, "application/json"))
        series_entries.append(
            {
                "experiment_id": experiment,
                "platform": platform_id,
                "benchmark": benchmark,
                "metric": metric,
                "url": url,
            }
        )

    for (experiment, platform_id), values in sorted(revisions.items()):
        values.sort(key=lambda value: value["commit"]["ordinal"])
        payload = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment,
            "platform": platform_id,
            "revisions": values,
        }
        local, key, url = _write_content_object(destination, base_url, prefix, "revisions", payload)
        uploads.append((local, key, "application/json"))
        revision_entries.append({"experiment_id": experiment, "platform": platform_id, "url": url})

    for envelope in envelope_list:
        raw_key = "/".join(
            part
            for part in [
                prefix,
                "raw",
                "v1",
                envelope["experiment_id"],
                envelope["platform"],
                envelope["aihc_commit"]["sha"],
                f"{envelope['run_id']}.json.gz",
            ]
            if part
        )
        raw_path = destination / raw_key
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(raw_path, "wt", encoding="utf-8", compresslevel=9) as output:
            json.dump(envelope, output, sort_keys=True, separators=(",", ":"))
        uploads.append((raw_path, raw_key, "application/gzip"))

    return (
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "active_experiment": experiments[-1] if experiments else None,
            "experiments": experiments,
            "series": series_entries,
            "revision_indexes": revision_entries,
        },
        uploads,
    )


def merge_catalog(base: Optional[Dict[str, Any]], update: Dict[str, Any]) -> Dict[str, Any]:
    if not base:
        return update
    merged = dict(base)
    merged["schema_version"] = SCHEMA_VERSION
    merged["generated_at"] = update["generated_at"]
    merged["active_experiment"] = update.get("active_experiment") or base.get("active_experiment")
    merged["experiments"] = sorted(set(base.get("experiments", [])) | set(update.get("experiments", [])))
    for field, identity in [
        ("series", lambda value: (value["experiment_id"], value["platform"], value["benchmark"], value["metric"])),
        ("revision_indexes", lambda value: (value["experiment_id"], value["platform"])),
    ]:
        values = {identity(value): value for value in base.get(field, [])}
        values.update({identity(value): value for value in update.get(field, [])})
        merged[field] = [values[key] for key in sorted(values)]
    return merged


def write_catalog(catalog: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_content_object(
    destination: Path,
    base_url: str,
    prefix: str,
    kind: str,
    payload: Dict[str, Any],
) -> Tuple[Path, str, str]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    key = "/".join(part for part in [prefix, kind, "v1", f"{digest}.json"] if part)
    local = destination / key
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(encoded + b"\n")
    return local, key, f"{base_url}/{key}"
