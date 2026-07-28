from __future__ import annotations

import json
import os
import subprocess
import urllib.error
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .catalog import build_bundle, merge_catalog, write_catalog
from .report import load_json


class PublishError(RuntimeError):
    pass


def publish(
    *,
    envelopes: Iterable[Dict[str, Any]],
    config: Dict[str, Any],
    destination: Path,
    base_catalog_location: Optional[str],
    dry_run: bool,
    trigger_workflow: bool,
    root: Path,
) -> Dict[str, Any]:
    publishing = config["publishing"]
    public_base_url = os.environ.get("R2_PUBLIC_BASE_URL", publishing["public_base_url"]).rstrip("/")
    if public_base_url.endswith(".invalid") and not dry_run:
        raise PublishError("set publishing.public_base_url in benchmark.json before publishing")

    base = _load_optional_catalog(base_catalog_location)
    update, uploads = build_bundle(
        envelopes,
        destination,
        public_base_url=None if dry_run else public_base_url,
    )
    catalog = merge_catalog(base, update)
    candidate_key = publishing["candidate_catalog_key"].lstrip("/")
    candidate_path = destination / candidate_key
    write_catalog(catalog, candidate_path)

    if dry_run:
        return catalog

    endpoint = _endpoint()
    bucket = os.environ.get("R2_BUCKET", publishing["bucket"])
    for local, key, content_type in uploads:
        if not _exists(bucket, key, endpoint):
            _upload(local, bucket, key, endpoint, content_type, "public, max-age=31536000, immutable")
    _upload(candidate_path, bucket, candidate_key, endpoint, "application/json", "no-cache")

    if trigger_workflow:
        catalog_url = f"{public_base_url}/{candidate_key}"
        process = subprocess.run(
            ["gh", "workflow", "run", "results-update.yml", "-f", f"catalog_url={catalog_url}"],
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise PublishError(f"uploaded results, but could not trigger results workflow: {process.stderr.strip()}")
    return catalog


def _load_optional_catalog(location: Optional[str]) -> Optional[Dict[str, Any]]:
    if not location:
        return None
    try:
        return load_json(location)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _endpoint() -> str:
    account_id = os.environ.get("R2_ACCOUNT_ID")
    if not account_id:
        raise PublishError("R2_ACCOUNT_ID is not set")
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        raise PublishError("AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set")
    return f"https://{account_id}.r2.cloudflarestorage.com"


def _upload(local: Path, bucket: str, key: str, endpoint: str, content_type: str, cache_control: str) -> None:
    process = subprocess.run(
        [
            "aws",
            "s3",
            "cp",
            str(local),
            f"s3://{bucket}/{key}",
            "--endpoint-url",
            endpoint,
            "--content-type",
            content_type,
            "--cache-control",
            cache_control,
            "--only-show-errors",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise PublishError(f"could not upload {key}: {process.stderr.strip()}")


def _exists(bucket: str, key: str, endpoint: str) -> bool:
    process = subprocess.run(
        [
            "aws",
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--endpoint-url",
            endpoint,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return process.returncode == 0
