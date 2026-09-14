from __future__ import annotations

import hashlib
import json
import platform as host_platform
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import __version__
from .git_history import DEFAULT_TREE_PATHS, GitError, parse_cutoff
from .stats import STATS_FORMATS

CONFIG_SCHEMA_VERSION = 2
OPTIMIZATION_PROFILES = ("O0", "O1", "O2", "Os")


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
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema version: {config['schema_version']}")
    _validate_unique(config["benchmarks"], "benchmark")
    _validate_unique(config["configurations"], "configuration")
    for configuration in config["configurations"]:
        _validate_configuration(configuration)
    ghc_boot_libraries = config.setdefault("ghc_boot_libraries", [])
    if not isinstance(ghc_boot_libraries, list) or not all(isinstance(name, str) and name for name in ghc_boot_libraries):
        raise ConfigError("ghc_boot_libraries must be a list of package names")
    for benchmark in config["benchmarks"]:
        if not benchmark.get("package"):
            raise ConfigError(f"benchmark {benchmark.get('id')} lacks a package (its cabal executable name)")
        source = (path.parent / benchmark["source"]).resolve()
        if not source.is_dir():
            raise ConfigError(f"benchmark source is not a directory: {source}")
        if not list(source.glob("*.cabal")):
            raise ConfigError(f"benchmark source has no .cabal file: {source}")
        benchmark["source_sha256"] = _hash_directory(source)
    toolchain_hasher = hashlib.sha256()
    for toolchain_file in (path.parent / "flake.nix", path.parent / "flake.lock"):
        if toolchain_file.is_file():
            toolchain_hasher.update(toolchain_file.name.encode("utf-8"))
            toolchain_hasher.update(toolchain_file.read_bytes())
    config.setdefault("aihc_tree_paths", list(DEFAULT_TREE_PATHS))
    publishing = config.setdefault("publishing", {})
    publishing.setdefault("server_url", "https://perf.aihc.app")
    publishing.setdefault("wrangler_config", "web/wrangler.jsonc")
    publishing.setdefault("bucket", "aihc-benchmarks")
    publishing.setdefault("database", "aihc-benchmarks")
    if not isinstance(config["aihc_tree_paths"], list) or not all(isinstance(item, str) and item for item in config["aihc_tree_paths"]):
        raise ConfigError("aihc_tree_paths must be a list of repository paths")
    since = config.get("aihc_since")
    if since is not None:
        if not isinstance(since, str):
            raise ConfigError("aihc_since must be an ISO 8601 timestamp string")
        try:
            parse_cutoff(since)
        except GitError as error:
            raise ConfigError(f"aihc_since: {error}") from error
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


def _hash_directory(source: Path) -> str:
    """Hash every file in a benchmark package directory, deterministically.

    Benchmarks are now self-contained Cabal packages rather than a single
    ``Main.hs``, so the experiment identity (see ``benchmark_experiment_id``) needs to
    change whenever any file in the package changes, including its
    ``cabal.project.freeze`` pins.
    """
    hasher = hashlib.sha256()
    for file_path in sorted(p for p in source.rglob("*") if p.is_file()):
        hasher.update(file_path.relative_to(source).as_posix().encode("utf-8"))
        hasher.update(file_path.read_bytes())
    return hasher.hexdigest()


def _validate_configuration(configuration: Dict[str, Any]) -> None:
    identifier = configuration["id"]
    for key in ("compiler_family", "compiler_version", "backend", "gc", "optimization", "compile", "run"):
        if key not in configuration:
            raise ConfigError(f"configuration {identifier} lacks {key}")
    if configuration["optimization"] not in OPTIMIZATION_PROFILES:
        raise ConfigError(f"configuration {identifier} has an unknown optimization profile")
    stats_format = configuration.get("runtime_stats")
    if stats_format is not None and stats_format not in STATS_FORMATS:
        raise ConfigError(f"configuration {identifier} has an unknown runtime_stats format")


def _validate_unique(items: Iterable[Dict[str, Any]], kind: str) -> None:
    identifiers: List[str] = [str(item.get("id", "")) for item in items]
    if any(not identifier for identifier in identifiers):
        raise ConfigError(f"every {kind} needs a non-empty id")
    duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
    if duplicates:
        raise ConfigError(f"duplicate {kind} ids: {', '.join(duplicates)}")


def benchmark_experiment_id(config: Dict[str, Any], benchmark: Dict[str, Any]) -> str:
    """The experiment a benchmark's results belong to.

    Identity is per benchmark, so adding a benchmark leaves every other
    benchmark's history valid. The hash covers everything that changes what
    a measurement means: the benchmark itself (including its package
    contents), the configurations, the measurement settings, the tree paths
    that decide result inheritance, the pinned toolchain and the runner
    version. Local paths and publishing locations do not take part.
    """
    semantic = {
        "schema_version": config["schema_version"],
        "measurement": config["measurement"],
        "tree_paths": config.get("aihc_tree_paths"),
        "benchmark": benchmark,
        "configurations": config["configurations"],
        "toolchain_sha256": config.get("_toolchain_sha256"),
        "runner_version": config.get("_runner_version"),
    }
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{benchmark['id']}-{hashlib.sha256(encoded).hexdigest()[:12]}"


def experiment_ids(config: Dict[str, Any]) -> Dict[str, str]:
    """Benchmark id to experiment id, in configuration order."""
    return {benchmark["id"]: benchmark_experiment_id(config, benchmark) for benchmark in config["benchmarks"]}


def suite_key(config: Dict[str, Any]) -> str:
    """Identity of the whole suite: the sorted set of its benchmark experiments.

    The site shows one suite at a time and the uploader publishes this key
    with the benchmark to experiment mapping, so the Worker knows which
    experiments make up the current suite without re-deriving the hashes.
    """
    encoded = json.dumps(sorted(experiment_ids(config).values()), separators=(",", ":")).encode("utf-8")
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
