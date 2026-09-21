# The parser corpus: every module of a pinned Stackage snapshot that a parser
# can read as it sits on disk, with the language and extensions its package
# declares. See build.py for what the output contains.
#
# Each package tarball is its own fixed-output derivation, shared with the
# CPP corpus since both read the same snapshot, so a fetch that fails names
# the package, a re-run only downloads what is missing, and the assembly step
# never touches the network.
{
  pkgs,
  snapshotFile ? ../stackage/lts-24.58.json,
  # Module bytes the timed benchmark sweeps (benchmark.tsv); 0 for all of
  # them. Sized so that a native AIHC -O0 build of the sweep takes a few
  # seconds: it parses hand-written modules at around 17 KB/s today, and
  # Wasm under Wasmtime at around 4 KB/s after two seconds of startup. A
  # GHC build sweeps the whole 240 MB corpus with --report in about two
  # minutes.
  sampleBytes ? 32 * 1024,
}: let
  inherit (pkgs) lib;
  snapshot = builtins.fromJSON (builtins.readFile snapshotFile);
  tarball = package:
    pkgs.fetchurl {
      url = "mirror://hackage/${package.name}-${package.version}.tar.gz";
      inherit (package) sha256;
    };
  tarballs = lib.listToAttrs (map (package: {
      name = "${package.name}-${package.version}";
      value = tarball package;
    })
    snapshot.packages);
in
  pkgs.runCommand "aihc-parser-corpus-${snapshot.snapshot}" {
    nativeBuildInputs = [pkgs.python3];
    tarballsJson = builtins.toJSON tarballs;
    passAsFile = ["tarballsJson"];
    snapshot = snapshotFile;
    script = ./build.py;
    sampleBytes = toString sampleBytes;
    passthru = {inherit snapshot tarballs;};
  } ''
    python3 "$script" \
      --snapshot "$snapshot" \
      --tarballs "$tarballsJsonPath" \
      --out "$out" \
      --sample-bytes "$sampleBytes"
  ''
