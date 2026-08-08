from __future__ import annotations

import json
import math
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


START = "<!-- AUTO-GENERATED: START benchmark-summary -->"
END = "<!-- AUTO-GENERATED: END benchmark-summary -->"
BACKEND_BASELINES = {"native": "native", "llvm": "llvm"}


def load_json(location: str) -> Dict[str, Any]:
    if location.startswith(("https://", "http://", "file://")):
        with urllib.request.urlopen(location, timeout=30) as response:
            return json.loads(response.read())
    return json.loads(Path(location).read_text(encoding="utf-8"))


def generate_summary(catalog: Dict[str, Any], platform_id: str = "aarch64-darwin") -> str:
    experiment = catalog.get("active_experiment") or (catalog.get("experiments", [None])[-1] if catalog.get("experiments") else None)
    if not experiment:
        return "_No benchmark results have been published yet._"
    revision_entry = next(
        (
            entry
            for entry in catalog.get("revision_indexes", [])
            if entry["experiment_id"] == experiment and entry["platform"] == platform_id
        ),
        None,
    )
    if not revision_entry:
        return f"_No results are available for `{platform_id}`._"
    revision_index = load_json(revision_entry["url"])
    if not revision_index.get("revisions"):
        return f"_No results are available for `{platform_id}`._"
    latest = max(revision_index["revisions"], key=lambda value: value["commit"]["ordinal"])
    commit = latest["commit"]
    lines = [
        f"Latest measured `aihc/main`: [`{commit['sha'][:12]}`](https://github.com/ai-haskell-compiler/aihc/commit/{commit['sha']}) "
        f"({commit['committed_at'][:10]}) on `{platform_id}`.",
        "",
    ]
    if latest["compiler_status"] != "available":
        reason = latest.get("unavailable_reason") or "unknown"
        lines.append(f"**Compiler unavailable:** `{reason}`. No runtime results exist for this revision.")
        return "\n".join(lines)

    views = _wall_views(catalog, experiment, platform_id)
    benchmark_rows: List[Tuple[str, Dict[str, Optional[float]]]] = []
    ghc_version = _latest_ghc_914(views, commit["sha"])
    for benchmark, payload in sorted(views.items()):
        points = [point for point in payload["points"] if point["commit"]["sha"] == commit["sha"]]
        values: Dict[str, Optional[float]] = {}
        for backend in ("native", "wasm", "llvm"):
            aihc = next(
                (
                    point
                    for point in points
                    if point["compiler_family"] == "aihc" and point["backend"] == backend and point["gc"] == "semispace"
                ),
                None,
            )
            ghc_backend = BACKEND_BASELINES.get(backend)
            ghc = next(
                (
                    point
                    for point in points
                    if ghc_backend
                    and point["compiler_family"] == "ghc"
                    and point["compiler_version"] == ghc_version
                    and point.get("compiler_variant", "gmp") == "gmp"
                    and point["backend"] == ghc_backend
                ),
                None,
            )
            values[backend] = (aihc["estimate"] / ghc["estimate"]) if aihc and ghc and ghc["estimate"] else None
            values[f"{backend}_time"] = aihc["estimate"] if aihc else None
        benchmark_rows.append((benchmark, values))

    lines.extend(
        [
            f"Baseline: GHC `{ghc_version or '9.14 unavailable'}`, `-O2`. Lower is better; ratios are AIHC/GHC.",
            "",
            "| Benchmark | Native vs GHC | AIHC Wasm | LLVM vs GHC |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for benchmark, values in benchmark_rows:
        lines.append(
            f"| `{benchmark}` | {_format_cell(values['native_time'], values['native'])} | "
            f"{_format_time(values['wasm_time'])} | {_format_cell(values['llvm_time'], values['llvm'])} |"
        )
    means = {backend: _geometric_mean([values[backend] for _, values in benchmark_rows]) for backend in ("native", "wasm", "llvm")}
    lines.append(
        f"| **Geometric mean** | {_format_ratio(means['native'])} | — no GHC baseline | {_format_ratio(means['llvm'])} |"
    )
    return "\n".join(lines)


def update_readme(readme: Path, summary: str) -> None:
    content = readme.read_text(encoding="utf-8")
    replacement = f"{START}\n{summary}\n{END}"
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(content):
        raise ValueError(f"{readme} does not contain generated summary markers")
    readme.write_text(pattern.sub(replacement, content), encoding="utf-8")


def _wall_views(catalog: Dict[str, Any], experiment: str, platform_id: str) -> Dict[str, Dict[str, Any]]:
    views: Dict[str, Dict[str, Any]] = {}
    for entry in catalog.get("series", []):
        if (
            entry["experiment_id"] == experiment
            and entry["platform"] == platform_id
            and entry["metric"] == "wall_time"
        ):
            views[entry["benchmark"]] = load_json(entry["url"])
    return views


def _latest_ghc_914(views: Dict[str, Dict[str, Any]], commit_sha: str) -> Optional[str]:
    versions = {
        point["compiler_version"]
        for view in views.values()
        for point in view["points"]
        if point["commit"]["sha"] == commit_sha
        and point["compiler_family"] == "ghc"
        and point["compiler_version"].startswith("9.14.")
    }
    return max(versions, key=_version_tuple) if versions else None


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _format_cell(nanoseconds: Optional[float], ratio: Optional[float]) -> str:
    if nanoseconds is None or ratio is None:
        return "— unavailable"
    return f"{nanoseconds / 1_000_000:.2f} ms · {ratio:.3f}×"


def _format_time(nanoseconds: Optional[float]) -> str:
    if nanoseconds is None:
        return "— unavailable"
    return f"{nanoseconds / 1_000_000:.2f} ms"


def _format_ratio(ratio: Optional[float]) -> str:
    return f"{ratio:.3f}×" if ratio is not None else "— unavailable"


def _geometric_mean(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None and value > 0]
    if not present:
        return None
    return math.exp(sum(math.log(value) for value in present) / len(present))
