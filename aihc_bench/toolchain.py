"""The programs a measurement runs: built, pinned and checked before it starts.

Every AIHC compile used to start with ``nix run <worktree>#aihc``, so Nix
evaluated the flake, found the compiler in the store -- or built it -- and
handed over, inside the timed step, once per cell. Two things followed. Each
compile time carried a second or two of flake evaluation that GHC's did not.
And the store is not this suite's: Determinate Nix collects garbage on its
own schedule, the compiler was no garbage collector root, and on worker-m1 a
collection deleted it partway through a commit. The next ``nix run`` rebuilt
it inside a timed cell (384 s of "compile time") and that rebuild came out
with one wrong page hash in its code signature. macOS kills a process when it
touches such a page, so every AIHC compile of the two stackage benchmarks died
with SIGKILL and no output, the small benchmarks never touched the page and
compiled fine, and 24 cells a commit were published as ``compile_failed`` for
two commits before anyone looked.

So the programs are fixed before anything is timed. ``build_compiler`` builds
the commit's ``aihc`` once, holds it with a garbage collector root and returns
the absolute path the cells execute; ``pin_runner_environment`` does the same
for the store paths this runner was started with (the GHC toolchains, the
corpora, the strip and Wasm tools); ``verify_closure`` checks both before a
commit is measured. Whatever goes wrong with them is a ``MachineFault``: the
commit is not recorded, stays unfinished and is measured again once the
machine is sound, rather than publishing the machine's problem as a property
of the compiler.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .process import run_command


class MachineFault(RuntimeError):
    """The machine is at fault, not the compiler under test.

    Never recorded as a result. The attempt stays unfinished, so the commit is
    measured again on the next run instead of carrying the fault in its
    history until someone runs ``forget``.
    """


class CompilerDoesNotBuild(Exception):
    """The commit's compiler fails to evaluate, build or start.

    A property of the commit, recorded as ``build_failed``. Only raised when
    nothing in the failure points at the machine; see ``MachineFault``.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Compiler:
    """A built, rooted and verified ``aihc``."""

    #: What the cells execute: the flake's ``aihc`` app, which runs the
    #: compiler with the options the flake gives it (``+RTS -M2G``).
    program: Path
    #: The store path ``program`` lives in, held by a garbage collector root.
    store_path: Path


STORE_DIR = "/nix/store"

#: A store path and nothing below it: ``/nix/store/<hash>-<name>``.
_STORE_PATH = re.compile(r"/nix/store/[0-9a-z]{32}-[^/\s:'\"]+")

#: Output that says the machine failed, not the derivation. Anything else from
#: a failed evaluation or build is taken to be the commit's, as it was before
#: this distinction existed; a pattern missing here costs a wrong
#: ``build_failed``, never a wrong measurement.
_MACHINE_FAILURE = re.compile(
    r"(unable to download|could not resolve host|couldn't resolve host|no space left on device"
    r"|cannot connect to socket|cannot open connection|connection (refused|reset|timed out)"
    r"|too many open files|database is locked|killed by signal|signal 9|out of memory"
    r"|unexpected end-of-file|HTTP error \d+|SSL (connect )?error|disk I/O error)",
    re.IGNORECASE,
)


def build_compiler(worktree: Path, platform_id: str, link: Path, timeout_seconds: float) -> Compiler:
    """Build the commit's ``aihc``, hold it at ``link`` and check it.

    The suite tracks the current AIHC command line only, so a commit whose
    ``aihc`` builds is assumed to offer ``build`` and ``install`` with the
    flags ``benchmark.json`` passes them. Commits older than ``aihc_since``
    predate that command line and are never planned.

    A build whose verification fails is deleted and built once more before
    the machine is blamed: the broken compiler on worker-m1 came from one bad
    build, and the next build of the same derivation was sound.
    """
    program, derivations = _evaluate_app(worktree, platform_id, timeout_seconds)
    for attempt in range(2):
        store_path = _build_rooted(derivations, program, link, timeout_seconds)
        if not Path(program).is_file():
            raise MachineFault(f"{program} is missing although {store_path} was just built")
        problems = verify_closure([store_path], timeout_seconds)
        if not problems:
            break
        if attempt == 0 and _delete_build(store_path, problems, link, timeout_seconds):
            continue
        raise MachineFault(
            "the compiler in the Nix store fails verification:\n"
            + describe_problems(problems)
            + "\ndelete it (nix store delete <path>) so the next run builds it again"
        )
    compiler = Compiler(program=Path(program), store_path=store_path)
    _probe(compiler, worktree, timeout_seconds)
    return compiler


def _evaluate_app(worktree: Path, platform_id: str, timeout_seconds: float) -> tuple:
    """The ``aihc`` app's program and the derivations that produce it.

    ``nix run`` would build and run the app in one go, but nothing it builds
    is rooted. The program's string context names the derivations behind it,
    which ``nix build --out-link`` can then build and root in one step, so the
    compiler is never in the store unrooted.
    """
    expression = (
        "app: { program = builtins.unsafeDiscardStringContext app.program; "
        "derivations = builtins.attrNames (builtins.getContext app.program); }"
    )
    command = ["nix", "eval", "--json", f"{worktree}#apps.{platform_id}.aihc", "--apply", expression]
    process = _nix(command, worktree, timeout_seconds, "evaluating the aihc app")
    try:
        evaluated = json.loads(process.stdout)
        program = str(evaluated["program"])
        derivations = [str(item) for item in evaluated["derivations"] if str(item).endswith(".drv")]
    except (ValueError, KeyError, TypeError) as error:
        raise MachineFault(f"nix eval returned something unexpected for the aihc app: {error}") from error
    if not derivations:
        raise CompilerDoesNotBuild(f"the aihc app's program {program} is not built by any derivation")
    return program, derivations


def _build_rooted(derivations: Sequence[str], program: str, link: Path, timeout_seconds: float) -> Path:
    """Build ``derivations`` and root their outputs at ``link``; return the store path holding ``program``."""
    link.parent.mkdir(parents=True, exist_ok=True)
    _remove_links(link)
    command = ["nix", "build", "--print-out-paths", "--out-link", str(link), *(f"{drv}^out" for drv in derivations)]
    process = _nix(command, link.parent, timeout_seconds, "building the compiler")
    outputs = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    holding = [output for output in outputs if program.startswith(output.rstrip("/") + "/")]
    if not holding:
        raise MachineFault(f"nix build printed {outputs or 'no output path'}, none of which holds {program}")
    return Path(holding[0])


def _delete_build(store_path: Path, problems: Dict[str, str], link: Path, timeout_seconds: float) -> bool:
    """Drop a build that failed verification so the next build replaces it.

    Only the paths that failed and the compiler that refers to them are
    deleted. Nix refuses while anything else still holds them -- another
    root, a running process -- and then the fault stands.
    """
    _remove_links(link)
    targets = sorted({str(store_path), *(_store_root(path) for path in problems)})
    try:
        process = run_command(["nix", "store", "delete", *targets], link.parent, timeout_seconds)
    except subprocess.TimeoutExpired:
        return False
    return process.returncode == 0


def _probe(compiler: Compiler, cwd: Path, timeout_seconds: float) -> None:
    """Start the compiler once, before a cell depends on it."""
    try:
        process = run_command([str(compiler.program), "--help"], cwd, timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MachineFault(f"aihc --help did not return: {error}") from error
    except OSError as error:
        raise MachineFault(f"cannot start {compiler.program}: {error}") from error
    if process.returncode < 0:
        raise MachineFault(f"aihc --help was killed by signal {-process.returncode}: {killed_by_hint(process.returncode)}")
    if process.returncode != 0:
        raise CompilerDoesNotBuild((process.stderr or process.stdout)[-8192:] or f"aihc --help exited with {process.returncode}")


def pin_runner_environment(root: Path, timeout_seconds: float) -> List[Path]:
    """Root and verify the store paths this runner was started with.

    The flake hands the runner its tools as store paths in ``PATH`` and in
    ``AIHC_*`` variables. The running process keeps them alive on Linux,
    where Nix reads every process's environment for roots, but not on macOS,
    where it only asks ``lsof`` for open files: a GHC toolchain that nothing
    has open is as collectable there as the compiler was.
    """
    paths = runner_store_paths()
    if not paths:
        return []
    directory = root / ".cache" / "gcroots" / "runner"
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    command = ["nix", "build", "--out-link", str(directory / "path"), *map(str, paths)]
    _nix(command, root, timeout_seconds, "rooting the runner's tools", machine_only=True)
    problems = verify_closure(paths, timeout_seconds)
    if problems:
        raise MachineFault(
            "the runner's tools fail verification:\n"
            + describe_problems(problems)
            + "\ndelete them (nix store delete <path>) and start the runner again"
        )
    return paths


def runner_store_paths(environment: Optional[Dict[str, str]] = None, executable: Optional[str] = None) -> List[Path]:
    """The store paths named by ``PATH``, ``PYTHONPATH``, ``AIHC_*`` and the interpreter."""
    environment = dict(os.environ) if environment is None else environment
    values = [value for name, value in environment.items() if name in ("PATH", "PYTHONPATH") or name.startswith("AIHC_")]
    values.append(os.path.realpath(executable or sys.executable))
    found = {_store_root(match) for value in values for match in _STORE_PATH.findall(value)}
    return sorted(Path(path) for path in found)


def verify_closure(paths: Iterable[Path], timeout_seconds: float) -> Dict[str, str]:
    """Problems with the closure of ``paths``, by file or store path; empty when sound.

    ``nix store verify`` compares every file with the hash Nix recorded when
    the path was added, which catches anything that changed it on disk. It
    cannot catch a path that was wrong when it was added -- the M1's compiler
    passed it -- so on macOS every executable and library is also checked
    against its own code signature, which is exactly what the kernel enforces
    page by page while the program runs.
    """
    roots = [str(path) for path in paths]
    if not roots:
        return {}
    problems: Dict[str, str] = {}
    try:
        process = run_command(["nix", "store", "verify", "--no-trust", "--recursive", *roots], Path("/"), timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MachineFault(f"nix store verify did not finish: {error}") from error
    if process.returncode != 0:
        output = process.stderr or process.stdout
        named = {_store_root(match) for match in _STORE_PATH.findall(output)}
        for path in named:
            problems[path] = "contents differ from the hash Nix recorded"
        if not named:
            problems[roots[0]] = first_line(output) or f"nix store verify exited with {process.returncode}"
    if sys.platform == "darwin":
        problems.update(_check_signatures(closure(roots, timeout_seconds), timeout_seconds))
    return problems


def closure(paths: Sequence[str], timeout_seconds: float) -> List[str]:
    try:
        process = run_command(["nix", "path-info", "--recursive", *paths], Path("/"), timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MachineFault(f"nix path-info did not finish: {error}") from error
    if process.returncode != 0:
        raise MachineFault(f"cannot list the closure of {', '.join(paths)}: {first_line(process.stderr)}")
    return [line.strip() for line in process.stdout.splitlines() if line.strip()]


#: Mach-O file types the kernel checks a signature for when it maps them.
_SIGNED_FILE_TYPES = {2: "executable", 6: "dylib", 8: "bundle"}
_MACH_O_64 = b"\xcf\xfa\xed\xfe"

#: Files per ``codesign`` invocation; it takes many paths and names each failure.
_CODESIGN_BATCH = 64


def signed_mach_o(path: Path) -> bool:
    """Whether ``path`` is a 64-bit Mach-O executable, dylib or bundle."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    return len(header) == 16 and header[:4] == _MACH_O_64 and struct.unpack_from("<I", header, 12)[0] in _SIGNED_FILE_TYPES


def _check_signatures(store_paths: Iterable[str], timeout_seconds: float) -> Dict[str, str]:
    files: List[str] = []
    for store_path in store_paths:
        for directory, _, names in os.walk(store_path):
            for name in names:
                path = os.path.join(directory, name)
                if not os.path.islink(path) and signed_mach_o(Path(path)):
                    files.append(path)
    problems: Dict[str, str] = {}
    for start in range(0, len(files), _CODESIGN_BATCH):
        batch = files[start : start + _CODESIGN_BATCH]
        problems.update(codesign_failures(batch, timeout_seconds))
    return problems


def codesign_failures(files: Sequence[str], timeout_seconds: float) -> Dict[str, str]:
    """The files among ``files`` whose code signature does not verify, with why."""
    if not files:
        return {}
    try:
        process = run_command(["codesign", "--verify", "--strict", *files], Path("/"), timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MachineFault(f"codesign did not finish: {error}") from error
    except OSError as error:
        raise MachineFault(f"cannot run codesign: {error}") from error
    if process.returncode == 0:
        return {}
    failures: Dict[str, str] = {}
    for line in (process.stderr or "").splitlines():
        for path in files:
            if line.startswith(f"{path}: "):
                failures[path] = line[len(path) + 2 :].strip()
    # codesign stops at nothing it cannot attribute; name the batch rather
    # than pass a failure off as success.
    return failures or {files[0]: first_line(process.stderr) or f"codesign exited with {process.returncode}"}


def describe_problems(problems: Dict[str, str]) -> str:
    return "\n".join(f"  {path}: {reason}" for path, reason in sorted(problems.items())[:20])


def killed_by_hint(returncode: int) -> str:
    """What a death by signal usually means here."""
    if returncode == -9:
        if sys.platform == "darwin":
            return "SIGKILL; on macOS usually a code signature the kernel rejected, or memory pressure"
        return "SIGKILL; usually the kernel's out-of-memory killer"
    return f"signal {-returncode}"


def first_line(text: Optional[str]) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _nix(
    command: List[str], cwd: Path, timeout_seconds: float, stage: str, machine_only: bool = False
) -> subprocess.CompletedProcess:
    """Run a Nix command; a failure is the machine's or the commit's, never ignored.

    ``machine_only`` is for commands that involve nothing of the commit's, so
    any failure of theirs is the machine's.
    """
    try:
        process = run_command(command, cwd, timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MachineFault(f"{stage} timed out: {error}") from error
    except OSError as error:
        raise MachineFault(f"{stage}: cannot run nix: {error}") from error
    if process.returncode == 0:
        return process
    output = process.stderr or process.stdout
    if machine_only or process.returncode < 0 or _MACHINE_FAILURE.search(output):
        raise MachineFault(f"{stage} failed on this machine:\n{output[-4000:]}")
    raise CompilerDoesNotBuild(output[-8192:])


def _store_root(path: str) -> str:
    match = _STORE_PATH.match(path)
    return match.group(0) if match else path


def _remove_links(link: Path) -> None:
    """Remove ``link`` and the numbered siblings ``nix build`` makes for extra outputs."""
    siblings = [path for path in link.parent.glob(f"{link.name}-*") if path.name[len(link.name) + 1 :].isdigit()]
    for candidate in [link, *siblings]:
        if candidate.is_symlink():
            candidate.unlink()
