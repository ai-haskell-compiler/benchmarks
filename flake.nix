{
  description = "AIHC historical benchmark suite";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  inputs.ghc-wasm-meta.url = "gitlab:haskell-wasm/ghc-wasm-meta?host=gitlab.haskell.org";

  outputs = {
    nixpkgs,
    ghc-wasm-meta,
    ...
  }: let
    systems = ["aarch64-darwin" "x86_64-linux"];
    forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {inherit system;}));

    # Everything the benchmark runner needs from Nix for one system. Shared by
    # the package, the app and the dev shell so they can never drift apart.
    tooling = pkgs: let
      llvm = pkgs.llvmPackages_19;
      # A GHC toolchain as cabal-install expects to find it: `ghc-<suffix>`
      # plus its `ghc-pkg-<suffix>` and `hsc2hs-<suffix>` siblings. The
      # runner's compile script passes ghc-pkg explicitly; without a sibling
      # cabal would fall back to whatever ghc-pkg is on PATH (nothing, in a
      # clean Nix environment) and fail to configure the package.
      ghcToolchain = suffix: compiler: prefix: extraInputs:
        map (tool:
          pkgs.writeShellApplication {
            name = "${tool}-${suffix}";
            runtimeInputs = [compiler] ++ extraInputs;
            text = ''exec ${prefix}${tool} "$@"'';
          }) ["ghc" "ghc-pkg" "hsc2hs"];
      wasmGhc = ghc-wasm-meta.packages.${pkgs.stdenv.hostPlatform.system}.wasm32-wasi-ghc-9_14;
      toolchains = pkgs.symlinkJoin {
        name = "aihc-bench-toolchains";
        paths =
          ghcToolchain "9.12.4" pkgs.haskell.compiler.ghc9124 "" [llvm.llvm]
          ++ ghcToolchain "9.14.1" pkgs.haskell.compiler.ghc9141 "" [llvm.llvm]
          ++ ghcToolchain "9.14.1-wasm" wasmGhc "wasm32-wasi-" [];
      };
      wasmClang = pkgs.writeShellApplication {
        name = "clang";
        text = ''
          exec ${llvm.clang-unwrapped}/bin/clang \
            -resource-dir ${llvm.clang-unwrapped.lib}/lib/clang/19 \
            "$@"
        '';
      };
      wasiSysroot = let
        wasilibc = pkgs.pkgsCross.wasi32.wasilibc;
      in
        pkgs.runCommand "aihc-bench-wasi-sysroot" {} ''
          mkdir -p "$out/include" "$out/lib"
          ln -s ${wasilibc.dev}/include/* "$out/include/"
          ln -s ${wasilibc}/lib/* "$out/lib/"
          for directory in include lib; do
            if [ ! -e "$out/$directory/wasm32-wasip1" ]; then
              ln -s wasm32-wasi "$out/$directory/wasm32-wasip1"
            fi
          done
          test -e "$out/include/wasm32-wasip1/stdlib.h"
          test -e "$out/lib/wasm32-wasip1/libc.a"
        '';
      # The CPP corpus benchmark's input: see corpus/cpp/corpus.nix.
      cppCorpus = import ./corpus/cpp/corpus.nix {inherit pkgs;};
      runtimeInputs = [pkgs.python3 pkgs.git pkgs.wrangler pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang llvm.lld llvm.bintools llvm.bintools-unwrapped pkgs.binaryen pkgs.cabal-install];
      runner = pkgs.writeShellApplication {
        name = "aihc-bench";
        inherit runtimeInputs;
        text = ''
          export AIHC_BENCH_WASM_CLANG=${wasmClang}/bin
          export AIHC_WASM_SYSROOT=${wasiSysroot}
          export AIHC_BENCH_TOOLCHAINS=${toolchains}
          export AIHC_BENCH_CPP_CORPUS=${cppCorpus}
          export PYTHONPATH=${./.}
          exec python3 -m aihc_bench "$@"
        '';
      };
    in {inherit toolchains runner runtimeInputs cppCorpus;};
  in {
    packages = forAllSystems (pkgs: let
      inherit (tooling pkgs) toolchains runner cppCorpus;
    in {
      inherit toolchains;
      cpp-corpus = cppCorpus;
      wasmtime = pkgs.writeShellApplication {
        name = "aihc-bench-wasmtime";
        runtimeInputs = [pkgs.wasmtime];
        text = ''exec wasmtime "$@"'';
      };
      default = runner;
    });

    apps = forAllSystems (pkgs: {
      default = {
        type = "app";
        program = pkgs.lib.getExe (tooling pkgs).runner;
      };
    });

    devShells = forAllSystems (pkgs: {
      default = pkgs.mkShell {
        packages = [pkgs.nodejs] ++ (tooling pkgs).runtimeInputs;
      };
    });
  };
}
