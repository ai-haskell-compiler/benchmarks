from __future__ import annotations

import hashlib
import json
import platform as host_platform
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    corpus_hashes: Dict[str, Optional[str]] = {}
    for benchmark in config["benchmarks"]:
        if not benchmark.get("package"):
            raise ConfigError(f"benchmark {benchmark.get('id')} lacks a package (its cabal executable name)")
        source = (path.parent / benchmark["source"]).resolve()
        if not source.is_dir():
            raise ConfigError(f"benchmark source is not a directory: {source}")
        if not list(source.glob("*.cabal")):
            raise ConfigError(f"benchmark source has no .cabal file: {source}")
        benchmark["source_sha256"] = _hash_directory(source)
        timeout = benchmark.get("process_timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ConfigError(f"benchmark {benchmark['id']}: process_timeout_seconds must be a positive number")
        corpus_env = benchmark.get("corpus_env")
        if corpus_env is not None and (not isinstance(corpus_env, str) or not corpus_env):
            raise ConfigError(f"benchmark {benchmark['id']}: corpus_env must name an environment variable")
        corpus_sources = benchmark.get("corpus_sources")
        if corpus_sources is not None:
            if not corpus_env:
                raise ConfigError(f"benchmark {benchmark['id']}: corpus_sources needs corpus_env")
            if not isinstance(corpus_sources, list) or not corpus_sources or not all(isinstance(item, str) and item for item in corpus_sources):
                raise ConfigError(f"benchmark {benchmark['id']}: corpus_sources must list the directories the corpus is built from")
            for item in corpus_sources:
                if not (path.parent / item).is_dir():
                    raise ConfigError(f"benchmark {benchmark['id']}: corpus source is not a directory: {path.parent / item}")
        if corpus_env:
            corpus_hashes[benchmark["id"]] = _hash_directories(path.parent, corpus_sources or ["corpus"])
    config["_corpus_sha256"] = corpus_hashes
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
    """Hash every file in a directory, deterministically.

    Benchmarks are self-contained Cabal packages rather than a single
    ``Main.hs``, so the experiment identity (see ``benchmark_experiment_id``)
    needs to change whenever any file in the package changes, including its
    ``cabal.project.freeze`` pins. The corpus sources under ``corpus/`` are
    hashed the same way for the benchmarks that read a corpus.
    """
    hasher = hashlib.sha256()
    for file_path in sorted(p for p in source.rglob("*") if p.is_file()):
        hasher.update(file_path.relative_to(source).as_posix().encode("utf-8"))
        hasher.update(file_path.read_bytes())
    return hasher.hexdigest()


def _hash_directories(root: Path, sources: Iterable[str]) -> Optional[str]:
    """Hash the files under several directories of the repository, by path.

    A corpus is assembled from its own directory and the shared snapshot pin,
    so a corpus benchmark names the directories it is built from and hashes
    only those: a change to one corpus restarts its benchmark's history and
    no other's. Paths are taken relative to the repository root, so moving a
    directory changes the digest as it changes the corpus. ``None`` when
    none of the directories exists.
    """
    hasher = hashlib.sha256()
    present = False
    for source in sources:
        directory = root / source
        if not directory.is_dir():
            continue
        present = True
        for file_path in sorted(p for p in directory.rglob("*") if p.is_file()):
            hasher.update(file_path.relative_to(root).as_posix().encode("utf-8"))
            hasher.update(file_path.read_bytes())
    return hasher.hexdigest() if present else None


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
    corpus_options = configuration.get("corpus_options", [])
    if not isinstance(corpus_options, list) or not all(isinstance(part, str) for part in corpus_options):
        raise ConfigError(f"configuration {identifier}: corpus_options must be a list of command template parts")


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
        # A benchmark that reads a corpus measures that corpus, so the files
        # it is built from are part of its identity; other benchmarks, and
        # the benchmarks of other corpora, are not restarted by a change.
        "corpus_sha256": config.get("_corpus_sha256", {}).get(benchmark["id"]),
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
