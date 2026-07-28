from __future__ import annotations

import hashlib
import json
import platform
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from . import SCHEMA_VERSION, __version__


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    return str(uuid.uuid4())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def environment_record(platform_id: str) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "platform": platform_id,
        "machine": platform.machine(),
        "processor": platform.processor(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "runner": __version__,
    }
    if sys.platform == "darwin":
        record["hardware_model"] = _command_output(["sysctl", "-n", "hw.model"])
        record["physical_memory"] = _command_output(["sysctl", "-n", "hw.memsize"])
        record["os_build"] = _command_output(["sw_vers", "-buildVersion"])
    elif sys.platform.startswith("linux"):
        record["hardware_model"] = _command_output(["sh", "-c", "cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || true"])
        record["os_build"] = _command_output(["uname", "-v"])

    identity = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record["id"] = f"{platform_id}-{hashlib.sha256(identity).hexdigest()[:12]}"
    return record


def result_envelope(
    *,
    experiment_id: str,
    platform_id: str,
    environment: Dict[str, Any],
    commit: Dict[str, Any],
    compiler_status: str,
    unavailable_reason: Optional[str],
    results: Iterable[Dict[str, Any]],
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "created_at": utc_now(),
        "experiment_id": experiment_id,
        "platform": platform_id,
        "environment": environment,
        "aihc_commit": commit,
        "compiler_status": compiler_status,
        "unavailable_reason": unavailable_reason,
        "results": list(results),
    }


def _command_output(command: Iterable[str]) -> str:
    try:
        return subprocess.check_output(list(command), stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ""
