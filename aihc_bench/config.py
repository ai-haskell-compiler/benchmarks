from __future__ import annotations

import hashlib
import json
import platform as host_platform
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import __version__


class ConfigError(ValueError):
    pass


def load_config(path: Path) -> Dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"could not load {path}: {error}") from error

    required = {"schema_version", "suite_id", "measurement", "platforms", "benchmarks", "configurations"}
    missing = sorted(required - set(config))
    if missing:
        raise ConfigError(f"missing configuration keys: {', '.join(missing)}")
    if config["schema_version"] != 1:
        raise ConfigError(f"unsupported schema version: {config['schema_version']}")
    _validate_unique(config["benchmarks"], "benchmark")
    _validate_unique(config["configurations"], "configuration")
    for benchmark in config["benchmarks"]:
        source = (path.parent / benchmark["source"]).resolve()
        if not source.is_file():
            raise ConfigError(f"benchmark source does not exist: {source}")
        benchmark["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    toolchain_hasher = hashlib.sha256()
    for toolchain_file in (path.parent / "flake.nix", path.parent / "flake.lock"):
        if toolchain_file.is_file():
            toolchain_hasher.update(toolchain_file.name.encode("utf-8"))
            toolchain_hasher.update(toolchain_file.read_bytes())
    config["_toolchain_sha256"] = toolchain_hasher.hexdigest()
    config["_runner_version"] = __version__

    measurement = config["measurement"]
    maximum = int(measurement["maximum_bucket_size"])
    if maximum < 2 or maximum & (maximum - 1):
        raise ConfigError("maximum_bucket_size must be a power of two of at least 2")
    threshold = float(measurement["relative_threshold"])
    if not 0 < threshold < 1:
        raise ConfigError("relative_threshold must be between zero and one")
    return config


def _validate_unique(items: Iterable[Dict[str, Any]], kind: str) -> None:
    identifiers: List[str] = [str(item.get("id", "")) for item in items]
    if any(not identifier for identifier in identifiers):
        raise ConfigError(f"every {kind} needs a non-empty id")
    duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
    if duplicates:
        raise ConfigError(f"duplicate {kind} ids: {', '.join(duplicates)}")


def experiment_id(config: Dict[str, Any]) -> str:
    semantic = {
        "schema_version": config["schema_version"],
        "suite_id": config["suite_id"],
        "optimization": config.get("optimization"),
        "measurement": config["measurement"],
        "benchmarks": config["benchmarks"],
        "configurations": config["configurations"],
        "toolchain_sha256": config.get("_toolchain_sha256"),
        "runner_version": config.get("_runner_version"),
    }
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{config['suite_id']}-{hashlib.sha256(encoded).hexdigest()[:12]}"


def detect_platform() -> str:
    machine = host_platform.machine().lower()
    system = host_platform.system().lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "aarch64-darwin"
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "x86_64-linux"
    return f"{machine}-{system}"


def expand_command(template: Iterable[str], values: Dict[str, str]) -> List[str]:
    try:
        return [part.format_map(values) for part in template]
    except KeyError as error:
        raise ConfigError(f"unknown command placeholder: {error.args[0]}") from error
