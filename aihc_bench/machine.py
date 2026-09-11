"""Stable machine identity derived from the CPU model and a hardware identifier."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from .schema import utc_now

MACHINE_FILE = "machine.json"
DROPPED_TOKENS = {"processor", "cpu", "(r)", "(tm)", "core", "with", "radeon", "graphics", "@"}
_CORE_COUNT = re.compile(r"^\d+-core$")
_FREQUENCY = re.compile(r"^\d+(\.\d+)?ghz$")
_UNSAFE = re.compile(r"[^a-z0-9]+")


def cpu_brand() -> str:
    if sys.platform == "darwin":
        return _command_output(["sysctl", "-n", "machdep.cpu.brand_string"])
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            return ""
    return ""


def hardware_identifier() -> Tuple[str, str]:
    """Return (identifier, source). The identifier never leaves the machine unhashed."""
    if sys.platform == "darwin":
        output = _command_output(["ioreg", "-d2", "-c", "IOPlatformExpertDevice"])
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
        if match:
            return match.group(1), "ioplatform-uuid"
    elif sys.platform.startswith("linux"):
        for path, source in (("/sys/class/dmi/id/product_uuid", "dmi-product-uuid"), ("/etc/machine-id", "machine-id")):
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                return value, source
    return socket.gethostname(), "hostname"


def slug(brand: str) -> str:
    tokens = []
    for raw in brand.lower().replace("(r)", " ").replace("(tm)", " ").split():
        if raw in DROPPED_TOKENS or _CORE_COUNT.match(raw) or _FREQUENCY.match(raw):
            continue
        token = _UNSAFE.sub("-", raw).strip("-")
        if not token:
            continue
        tokens.append(token)
        if len(tokens) == 3:
            break
    return "-".join(tokens) or "unknown-cpu"


def derive_machine_id(brand: str, identifier: str) -> str:
    return f"{slug(brand)}-{hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:6]}"


def load_machine(state_dir: Path, override: Optional[str] = None) -> Dict[str, Any]:
    """Load the frozen machine record, deriving and freezing it on first use."""
    path = state_dir / MACHINE_FILE
    if override is None and path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("machine_id"):
                return record
        except (OSError, ValueError):
            pass

    brand = cpu_brand()
    identifier, source = hardware_identifier()
    record = {
        "machine_id": override or derive_machine_id(brand, identifier),
        "derivation": {
            "cpu_brand": brand,
            "slug": slug(brand),
            "identifier_source": source,
            "overridden": override is not None,
        },
        "created_at": utc_now(),
    }
    if override is not None and not _valid_machine_id(override):
        raise ValueError("machine id must be lowercase letters, digits and hyphens")
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def _valid_machine_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value))


def _command_output(command: Iterable[str]) -> str:
    try:
        return subprocess.check_output(list(command), stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cpu_count() -> int:
    return os.cpu_count() or 0
