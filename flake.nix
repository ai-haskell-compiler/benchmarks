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
  in {
    packages = forAllSystems (pkgs: let
      ghcWrapper = name: compiler:
        pkgs.writeShellApplication {
          inherit name;
          runtimeInputs = [compiler pkgs.llvmPackages_19.llvm];
          text = ''exec ghc "$@"'';
        };
      wasmClang = pkgs.writeShellApplication {
        name = "clang";
        text = ''
          exec ${pkgs.llvmPackages_19.clang-unwrapped}/bin/clang \
            -resource-dir ${pkgs.llvmPackages_19.clang-unwrapped.lib}/lib/clang/19 \
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
      toolchains = pkgs.symlinkJoin {
        name = "aihc-bench-toolchains";
        paths = [
          (ghcWrapper "ghc-9.12.4" pkgs.haskell.compiler.ghc9124)
          (ghcWrapper "ghc-9.12.4-native-bignum" pkgs.haskell.compiler.native-bignum.ghc9124)
          (ghcWrapper "ghc-9.14.1" pkgs.haskell.compiler.ghc9141)
          (ghcWrapper "ghc-9.14.1-native-bignum" pkgs.haskell.compiler.native-bignum.ghc9141)
          (pkgs.writeShellApplication {
            name = "ghc-9.14.1-wasm";
            runtimeInputs = [ghc-wasm-meta.packages.${pkgs.stdenv.hostPlatform.system}.wasm32-wasi-ghc-9_14];
            text = ''exec wasm32-wasi-ghc "$@"'';
          })
        ];
      };
    in {
      ghc-9-12-4 = ghcWrapper "ghc-9.12.4" pkgs.haskell.compiler.ghc9124;
      ghc-9-12-4-native-bignum = ghcWrapper "ghc-9.12.4-native-bignum" pkgs.haskell.compiler.native-bignum.ghc9124;
      ghc-9-14-1 = ghcWrapper "ghc-9.14.1" pkgs.haskell.compiler.ghc9141;
      ghc-9-14-1-native-bignum = ghcWrapper "ghc-9.14.1-native-bignum" pkgs.haskell.compiler.native-bignum.ghc9141;
      wasmtime = pkgs.writeShellApplication {
        name = "aihc-bench-wasmtime";
        runtimeInputs = [pkgs.wasmtime];
        text = ''exec wasmtime "$@"'';
      };
      inherit toolchains;
      default = pkgs.writeShellApplication {
        name = "aihc-bench";
        runtimeInputs = [pkgs.python3 pkgs.git pkgs.wrangler pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang pkgs.llvmPackages_19.lld pkgs.llvmPackages_19.bintools pkgs.binaryen pkgs.cabal-install];
        text = ''
          export AIHC_BENCH_WASM_CLANG=${wasmClang}/bin
          export AIHC_WASM_SYSROOT=${wasiSysroot}
          export AIHC_BENCH_TOOLCHAINS=${toolchains}
          export PYTHONPATH=${./.}
          exec python3 -m aihc_bench "$@"
        '';
      };
    });

    apps = forAllSystems (pkgs: let
      ghcWrapper = name: compiler:
        pkgs.writeShellApplication {
          inherit name;
          runtimeInputs = [compiler pkgs.llvmPackages_19.llvm];
          text = ''exec ghc "$@"'';
        };
      toolchains = pkgs.symlinkJoin {
        name = "aihc-bench-toolchains";
        paths = [
          (ghcWrapper "ghc-9.12.4" pkgs.haskell.compiler.ghc9124)
          (ghcWrapper "ghc-9.12.4-native-bignum" pkgs.haskell.compiler.native-bignum.ghc9124)
          (ghcWrapper "ghc-9.14.1" pkgs.haskell.compiler.ghc9141)
          (ghcWrapper "ghc-9.14.1-native-bignum" pkgs.haskell.compiler.native-bignum.ghc9141)
          (pkgs.writeShellApplication {
            name = "ghc-9.14.1-wasm";
            runtimeInputs = [ghc-wasm-meta.packages.${pkgs.stdenv.hostPlatform.system}.wasm32-wasi-ghc-9_14];
            text = ''exec wasm32-wasi-ghc "$@"'';
          })
        ];
      };
      wasmClang = pkgs.writeShellApplication {
        name = "clang";
        text = ''
          exec ${pkgs.llvmPackages_19.clang-unwrapped}/bin/clang \
            -resource-dir ${pkgs.llvmPackages_19.clang-unwrapped.lib}/lib/clang/19 \
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
    in {
      default = {
        type = "app";
        program = "${pkgs.lib.getExe (pkgs.writeShellApplication {
          name = "aihc-bench";
          runtimeInputs = [pkgs.python3 pkgs.git pkgs.wrangler pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang pkgs.llvmPackages_19.lld pkgs.llvmPackages_19.bintools pkgs.binaryen pkgs.cabal-install];
          text = ''
            export AIHC_BENCH_WASM_CLANG=${wasmClang}/bin
            export AIHC_WASM_SYSROOT=${wasiSysroot}
            export AIHC_BENCH_TOOLCHAINS=${toolchains}
            export PYTHONPATH=${./.}
            exec python3 -m aihc_bench "$@"
          '';
        })}";
      };
    });

    devShells = forAllSystems (pkgs: {
      default = pkgs.mkShell {
        packages = [pkgs.python3 pkgs.nodejs pkgs.git pkgs.wrangler pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang pkgs.llvmPackages_19.lld pkgs.llvmPackages_19.bintools pkgs.binaryen pkgs.cabal-install];
      };
    });
  };
}
