import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench import wasm_sysroot
from aihc_bench.wasm_sysroot import TARGET, ensure_sysroot, is_complete, needs_wasip3_libc, sysroot_for_commit


def _tarball(path: Path, files):
    with tarfile.open(path, "w:gz") as tar:
        for name, content in files:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CommitTests(unittest.TestCase):
    def _repository(self, directory):
        repository = Path(directory) / "aihc"
        repository.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", ""),
        }
        git = lambda *args: subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "before")
        before = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "libc")
        libc = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "after")
        after = git("rev-parse", "HEAD")
        return repository, before, libc, after

    def test_commits_from_the_libc_change_on_need_the_new_sysroot(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, before, libc, after = self._repository(directory)
            with patch.object(wasm_sysroot, "WASIP3_LIBC_COMMIT", libc):
                self.assertFalse(needs_wasip3_libc(repository, before))
                self.assertTrue(needs_wasip3_libc(repository, libc))
                self.assertTrue(needs_wasip3_libc(repository, after))

    def test_a_clone_that_predates_the_change_needs_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, before, _libc, after = self._repository(directory)
            self.assertFalse(needs_wasip3_libc(repository, after))


class SysrootTests(unittest.TestCase):
    """The flake's sysroot is wasi-libc for preview 1, which the compiler
    refuses since aihc@e8f97b72; the wasi-sdk 34 one is fetched by the
    runner so flake.nix, which the experiment ids hash, stays as it is."""

    def _assets(self, scratch):
        sysroot = _tarball(scratch / "s.tar.gz", [
            (f"wasi-sysroot-34.0/include/{TARGET}/stdlib.h", b"h"),
            (f"wasi-sysroot-34.0/include/{TARGET}/sys/types.h", b"t"),
            (f"wasi-sysroot-34.0/lib/{TARGET}/libc.a", b"libc"),
            (f"wasi-sysroot-34.0/lib/{TARGET}/crt1.o", b"crt"),
            ("wasi-sysroot-34.0/lib/wasm32-wasip1/libc.a", b"other target"),
        ])
        builtins = _tarball(scratch / "b.tar.gz", [
            ("libclang_rt-34.0/wasm32-unknown-wasip3/libclang_rt.builtins.a", b"rt"),
            ("libclang_rt-34.0/wasm32-unknown-wasip1/libclang_rt.builtins.a", b"other"),
        ])
        return sysroot, builtins

    def test_the_one_target_is_assembled_from_the_two_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "scratch"
            scratch.mkdir()
            sysroot_hash, builtins_hash = self._assets(scratch)
            assets = (("wasi-sysroot-34.0.tar.gz", sysroot_hash, "wasi-sysroot-34.0"), ("libclang_rt-34.0.tar.gz", builtins_hash, "libclang_rt-34.0"))
            fetched = []

            def download(url, destination, sha256):
                fetched.append(url)
                source = scratch / ("s.tar.gz" if "sysroot" in url else "b.tar.gz")
                destination.write_bytes(source.read_bytes())
                self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), sha256)

            root = Path(directory) / "root"
            with patch.object(wasm_sysroot, "WASI_SDK_ASSETS", assets):
                result = ensure_sysroot(root, download=download)
                self.assertTrue(is_complete(result))
                self.assertEqual((result / f"lib/{TARGET}/libclang_rt.builtins.a").read_bytes(), b"rt")
                self.assertEqual((result / f"include/{TARGET}/sys/types.h").read_bytes(), b"t")
                self.assertFalse((result / "lib/wasm32-wasip1").exists())
                self.assertEqual(len(fetched), 2)
                # Fetched once: the second call finds it complete.
                self.assertEqual(ensure_sysroot(root, download=download), result)
                self.assertEqual(len(fetched), 2)

    def test_a_bad_hash_leaves_no_sysroot_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"

            def download(url, destination, sha256):
                raise ValueError(f"{url}: sha256 mismatch")

            with self.assertRaises(ValueError):
                ensure_sysroot(root, download=download)
            self.assertFalse(wasm_sysroot.sysroot_directory(root).exists())

    def test_a_failed_fetch_keeps_the_flake_sysroot(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(wasm_sysroot, "needs_wasip3_libc", return_value=True),
                patch.object(wasm_sysroot, "ensure_sysroot", side_effect=OSError("no network")),
            ):
                self.assertIsNone(sysroot_for_commit(Path(directory), "f" * 40, Path(directory)))

    def test_an_old_commit_keeps_the_flake_sysroot_without_fetching(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(wasm_sysroot, "needs_wasip3_libc", return_value=False),
                patch.object(wasm_sysroot, "ensure_sysroot") as ensure,
            ):
                self.assertIsNone(sysroot_for_commit(Path(directory), "f" * 40, Path(directory)))
            ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class LinkerTests(unittest.TestCase):
    """wasm-component-ld links the component; the flake does not carry it,
    and flake.nix is hashed into the experiment ids, so it is built from
    the nixpkgs the flake already locks."""

    LOCK = {
        "root": "root",
        "nodes": {
            "root": {"inputs": {"nixpkgs": "nixpkgs_2", "ghc-wasm-meta": "ghc-wasm-meta"}},
            "ghc-wasm-meta": {"inputs": {"nixpkgs": "nixpkgs"}},
            "nixpkgs": {"locked": {"type": "github", "owner": "NixOS", "repo": "nixpkgs", "rev": "a" * 40}},
            "nixpkgs_2": {"locked": {"type": "github", "owner": "NixOS", "repo": "nixpkgs", "rev": "b" * 40}},
        },
    }

    def test_the_root_flakes_nixpkgs_is_chosen_not_an_inputs(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "flake.lock"
            lock.write_text(json.dumps(self.LOCK), encoding="utf-8")
            self.assertEqual(wasm_sysroot.locked_nixpkgs(lock), f"github:NixOS/nixpkgs/{'b' * 40}")

    def test_the_linker_is_built_once_from_the_locked_nixpkgs(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "flake.lock").write_text(json.dumps(self.LOCK), encoding="utf-8")
            commands = []

            def run(command, cwd, timeout, *rest):
                commands.append(list(command))
                link = Path(command[command.index("--out-link") + 1])
                (link / "bin").mkdir(parents=True)
                (link / "bin" / "wasm-component-ld").write_bytes(b"ld")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(wasm_sysroot, "run_command", side_effect=run):
                first = wasm_sysroot.component_linker(root, 30)
                second = wasm_sysroot.component_linker(root, 30)
            self.assertEqual(first, second)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][:3], ["nix", "build", f"github:NixOS/nixpkgs/{'b' * 40}#wasm-component-ld"])

    def test_sysroot_and_linker_come_together_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(wasm_sysroot, "sysroot_for_commit", return_value=root / "sysroot"),
                patch.object(wasm_sysroot, "component_linker", side_effect=ValueError("no nix")),
            ):
                self.assertEqual(wasm_sysroot.wasm_environment_for_commit(root, "f" * 40, root, 30), {})
            with (
                patch.object(wasm_sysroot, "sysroot_for_commit", return_value=root / "sysroot"),
                patch.object(wasm_sysroot, "component_linker", return_value=root / "ld" / "bin"),
            ):
                environment = wasm_sysroot.wasm_environment_for_commit(root, "f" * 40, root, 30)
            self.assertEqual(environment["AIHC_WASM_SYSROOT"], str(root / "sysroot"))
            self.assertTrue(environment["PATH"].startswith(str(root / "ld" / "bin")))


class RunCommandTests(unittest.TestCase):
    def test_wasi_http_is_added_after_the_cli_option(self):
        from aihc_bench.runner import _with_wasi_http

        self.assertEqual(
            _with_wasi_http(["wasmtime", "run", "-S", "cli", "--allow-precompiled", "x.cwasm", "O0"]),
            ["wasmtime", "run", "-S", "cli", "-S", "http", "--allow-precompiled", "x.cwasm", "O0"],
        )
        self.assertEqual(_with_wasi_http(["x"]), ["x"])
