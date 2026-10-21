"""The frontend corpus must retain libraries and close their core imports."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("frontend_corpus", ROOT / "corpus/frontend/build.py")
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)


class FrontendCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.core = self.root / "core"
        self.package = self.root / "example-1.0"
        self.package.mkdir()
        self.cabal = self.package / "example.cabal"
        self.cabal.write_text("""name: example
version: 1.0
library
  exposed-modules: Example
  other-modules: Example.Internal
  hs-source-dirs: src
  build-depends: base >= 4 && < 5
  default-language: Haskell2010

test-suite ignored
  main-is: Broken.hs
""")
        self.write(self.package / "src/Example.hs", "module Example where\nimport Example.Internal\n")
        self.write(self.package / "src/Example/Internal.hs", "module Example.Internal where\n")
        self.write(self.package / "Broken.hs", "deliberately invalid test input")
        for module in corpus.WIRED:
            self.write(self.core / "aihc-prim/src" / (module.replace(".", "/") + ".hs"),
                       "module " + module + " where\n")
        self.write(self.core / "aihc-base/src/Prelude.hs", "module Prelude where\nimport GHC.Types\n")
        self.out = self.root / "out"

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def assemble(self):
        return corpus.assemble([self.package], self.core, self.out, ROOT / "corpus/parser/build.py")

    def test_keeps_complete_library_and_transitive_core_closure(self):
        count = self.assemble()
        manifest = (self.out / "modules.tsv").read_text()
        self.assertEqual(count, len(corpus.WIRED) + 3)
        self.assertIn("example\texample/Example/Internal.hs\tHaskell2010\t", manifest)
        self.assertIn("aihc-base\taihc-base/Prelude.hs\tGHC2021\tNoImplicitPrelude", manifest)
        self.assertNotIn("Broken", manifest)
        self.assertEqual((self.out / "aihc-base/Prelude.hs").read_bytes(),
                         (self.core / "aihc-base/src/Prelude.hs").read_bytes())
        self.assertEqual((self.out / "packages.tsv").read_text(),
                         "aihc-base\taihc-prim\naihc-prim\t\nexample\taihc-base\n")
        self.assertEqual((self.out / "example/Example.hs").read_bytes(),
                         (self.package / "src/Example.hs").read_bytes())

    def test_missing_import_is_an_error(self):
        self.write(self.package / "src/Example.hs", "module Example where\nimport Missing\n")
        with self.assertRaisesRegex(ValueError, "no provider.*Missing"):
            self.assemble()

    def test_conditional_or_preprocessed_library_is_an_error(self):
        original = self.cabal.read_text()
        for addition in ["  if os(linux)\n    build-depends: unix\n", "  default-extensions: CPP\n"]:
            with self.subTest(addition=addition):
                self.cabal.write_text(original.replace("\ntest-suite", "\n" + addition + "\ntest-suite"))
                with self.assertRaisesRegex(ValueError, "conditional|preprocessor"):
                    self.assemble()

    def test_workload_manifest_requires_complete_valid_cells(self):
        self.assemble()
        cells = json.loads((ROOT / "corpus/frontend/workloads.json").read_text())
        for backends in cells.values():
            for profiles in backends.values():
                for cell in profiles.values():
                    cell["packages"] = ["aihc-base", "example"]
        path = self.root / "workloads.json"
        path.write_text(json.dumps(cells))
        corpus.write_workloads(path, self.out)
        self.assertEqual(len((self.out / "workloads.tsv").read_text().splitlines()), 24)
        for field, value, message in [("packages", ["missing"], "package selection"), ("iterations", 0, "positive integer"), ("expected", "two\nlines", "single-line")]:
            with self.subTest(field=field):
                original = cells["resolve"]["native"]["O0"][field]
                cells["resolve"]["native"]["O0"][field] = value
                path.write_text(json.dumps(cells))
                with self.assertRaisesRegex(ValueError, message):
                    corpus.write_workloads(path, self.out)
                cells["resolve"]["native"]["O0"][field] = original
