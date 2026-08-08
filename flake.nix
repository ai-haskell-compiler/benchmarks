{
  description = "AIHC historical benchmark suite";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = {nixpkgs, ...}: let
    systems = ["aarch64-darwin" "x86_64-linux"];
    forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {inherit system;}));
  in {
    packages = forAllSystems (pkgs: let
      ghcWrapper = name: compiler: pkgs.writeShellApplication {
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
      default = pkgs.writeShellApplication {
        name = "aihc-bench";
        runtimeInputs = [pkgs.python3 pkgs.git pkgs.awscli2 pkgs.github-cli pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang pkgs.llvmPackages_19.lld pkgs.llvmPackages_19.bintools pkgs.binaryen];
        text = ''
          export AIHC_BENCH_WASM_CLANG=${wasmClang}/bin
          export PYTHONPATH=${./.}
          exec python3 -m aihc_bench "$@"
        '';
      };
    });

    apps = forAllSystems (pkgs: let
      wasmClang = pkgs.writeShellApplication {
        name = "clang";
        text = ''
          exec ${pkgs.llvmPackages_19.clang-unwrapped}/bin/clang \
            -resource-dir ${pkgs.llvmPackages_19.clang-unwrapped.lib}/lib/clang/19 \
            "$@"
        '';
      };
    in {
      default = {
        type = "app";
        program = "${pkgs.lib.getExe (pkgs.writeShellApplication {
          name = "aihc-bench";
          runtimeInputs = [pkgs.python3 pkgs.git pkgs.awscli2 pkgs.github-cli pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang pkgs.llvmPackages_19.lld pkgs.llvmPackages_19.bintools pkgs.binaryen];
          text = ''
            export AIHC_BENCH_WASM_CLANG=${wasmClang}/bin
            export PYTHONPATH=${./.}
            exec python3 -m aihc_bench "$@"
          '';
        })}";
      };
    });

    devShells = forAllSystems (pkgs: {
      default = pkgs.mkShell {
        packages = [pkgs.python3 pkgs.nodejs pkgs.git pkgs.wasmtime pkgs.wasm-tools pkgs.wit-bindgen pkgs.clang pkgs.llvmPackages_19.lld pkgs.llvmPackages_19.bintools pkgs.binaryen];
      };
    });
  };
}
