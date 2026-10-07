"""The WASI sysroot the AIHC Wasm backend links, chosen per commit.

Since ai-haskell-compiler/aihc@e8f97b72 (2026-10-05, "link the WASI 0.3 libc
of wasi-sdk") the compiler links the ``wasm32-wasip3`` libc that wasi-sdk 34
builds and refuses any other sysroot: the wasi-libc of nixpkgs, which the
flake assembles into ``AIHC_WASM_SYSROOT``, is built for preview 1 and calls
the host through imports a component cannot have. Every AIHC Wasm cell on
every machine failed to compile from that commit on, with
``AIHC_WASM_SYSROOT does not name a WASI sysroot``.

The sysroot is fetched here, by the runner, rather than added to
``flake.nix``: the experiment ids hash that file, so a change to it restarts
every benchmark's history, and the commits before e8f97b72 still need the
flake's sysroot. The two release assets are pinned by hash, exactly as the
compiler's own ``scripts/nix/wasi-sysroot.nix`` pins them, and cut down to
the headers and archives of the one target, with the compiler runtime
archive beside the libc where the compiler looks for it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from .git_history import GitError

#: The first compiler commit that links the wasi-sdk 34 libc.
WASIP3_LIBC_COMMIT = "e8f97b72e076ac9afcba9323c0001d1d854ed100"

WASI_SDK_RELEASE = "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-34"
WASI_SDK_VERSION = "34.0"
#: (asset, sha256, directory inside the tarball).
WASI_SDK_ASSETS = (
    (f"wasi-sysroot-{WASI_SDK_VERSION}.tar.gz", "9d813544eeebe38b7b8f2244ed591de46b6db812c6dd1a257ff9f0d2a905a2be", f"wasi-sysroot-{WASI_SDK_VERSION}"),
    (f"libclang_rt-{WASI_SDK_VERSION}.tar.gz", "eee3e634dcf71aa22b1333391623cf5c9965a637dc428a27b1a858c026c587f1", f"libclang_rt-{WASI_SDK_VERSION}"),
)
TARGET = "wasm32-wasip3"
#: What the compiler requires of a sysroot, relative to its root.
REQUIRED = (
    f"include/{TARGET}/stdlib.h",
    f"lib/{TARGET}/libc.a",
    f"lib/{TARGET}/libclang_rt.builtins.a",
)


def needs_wasip3_libc(repository: Path, sha: str) -> bool:
    """Whether the compiler at ``sha`` links the wasi-sdk 34 libc.

    A clone that does not know the introducing commit is older than it, so
    nothing it holds can need the new libc.
    """
    try:
        subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", WASIP3_LIBC_COMMIT, sha],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def sysroot_directory(root: Path) -> Path:
    return root / ".cache" / f"wasi-sysroot-{WASI_SDK_VERSION}"


def is_complete(directory: Path) -> bool:
    return all((directory / name).is_file() for name in REQUIRED)


def _download(url: str, destination: Path, sha256: str) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, open(destination, "wb") as out:
        shutil.copyfileobj(response, out)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if digest != sha256:
        raise ValueError(f"{url}: sha256 {digest}, expected {sha256}")


def _extract_target(archive: Path, member_root: str, destination: Path) -> None:
    """Copy the one target's headers, archives and runtime out of ``archive``."""
    wanted = (
        f"{member_root}/include/{TARGET}/",
        f"{member_root}/lib/{TARGET}/",
        f"{member_root}/{TARGET}/libclang_rt.builtins.a",
        f"{member_root}/wasm32-unknown-wasip3/libclang_rt.builtins.a",
    )
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(wanted):
                continue
            relative = member.name[len(member_root) + 1 :]
            if relative.endswith("libclang_rt.builtins.a") and not relative.startswith("lib/"):
                relative = f"lib/{TARGET}/libclang_rt.builtins.a"
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError(f"{archive}: refusing to extract {member.name} outside the sysroot")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def ensure_sysroot(root: Path, download=_download) -> Path:
    """The wasi-sdk sysroot under ``root/.cache``, fetched once.

    Raises when it cannot be fetched or verified; the caller decides what a
    missing sysroot means for the commit.
    """
    directory = sysroot_directory(root)
    if is_complete(directory):
        return directory
    staging = directory.with_name(f"{directory.name}.incoming")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as scratch:
        for asset, sha256, member_root in WASI_SDK_ASSETS:
            archive = Path(scratch) / asset
            download(f"{WASI_SDK_RELEASE}/{asset}", archive, sha256)
            _extract_target(archive, member_root, staging)
    missing = [name for name in REQUIRED if not (staging / name).is_file()]
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"wasi-sdk {WASI_SDK_VERSION} assets lack {', '.join(missing)}")
    shutil.rmtree(directory, ignore_errors=True)
    os.rename(staging, directory)
    return directory


def sysroot_for_commit(repository: Path, sha: str, root: Path) -> Optional[Path]:
    """The sysroot the compiler at ``sha`` needs, or ``None`` for the flake's.

    A fetch that fails is reported and leaves the flake's sysroot in place:
    the Wasm cells then fail to compile with the compiler's own message, as
    they did before, and the commit is still measured on the other backends.
    """
    if not needs_wasip3_libc(repository, sha):
        return None
    try:
        return ensure_sysroot(root)
    except (OSError, ValueError, tarfile.TarError, GitError) as error:
        print(f"warning: could not fetch the wasi-sdk {WASI_SDK_VERSION} sysroot, Wasm cells will use the flake's: {error}")
        return None
