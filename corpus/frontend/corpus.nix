# Complete, preprocessor-free libraries from the shared pinned snapshot,
# together with the real core source closure they resolve/typecheck against.
{pkgs}: let
  parserCorpus = import ../parser/corpus.nix {inherit pkgs;};
  coreCommit = "6ceb5ca6b5e6308fb48ee61a557aa6274aa54c2d";
  core = pkgs.fetchzip {
    url = "https://codeload.github.com/ai-haskell-compiler/aihc/tar.gz/${coreCommit}";
    extension = "tar.gz";
    sha256 = "1bxsxz42sj96x0rwiln0llwwzg6zml5rf1kpkkzc1vr9asrwii8z";
  };
  tarballs = parserCorpus.tarballs;
in pkgs.runCommand "aihc-frontend-corpus-lts-24.58" {
  nativeBuildInputs = [pkgs.python3];
  passthru = {inherit coreCommit;};
} ''
  tar -xzf ${tarballs."colour-2.3.7"}
  tar -xzf ${tarballs."split-0.2.5.1"}
  python3 ${./build.py} \
    --core ${core}/core-libs \
    --workloads ${./workloads.json} \
    --parser-script ${../parser/build.py} \
    --out "$out" colour-2.3.7 split-0.2.5.1
''
