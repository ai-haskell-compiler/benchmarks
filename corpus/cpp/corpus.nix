# The CPP corpus: every CPP-using module of a pinned Stackage snapshot, with
# the headers and macros a real build would give it. See build.py for what
# the output contains.
#
# Each package tarball is its own fixed-output derivation, so a fetch that
# fails names the package, a re-run only downloads what is missing, and the
# assembly step never touches the network.
{
  pkgs,
  snapshotFile ? ../stackage/lts-24.58.json,
  # Module bytes of benchmark.tsv, the pool the timed benchmark draws its
  # modules from; 0 for all of them. The program takes an even stride of a
  # given number of modules from it, a number per backend and profile, so
  # the pool only has to be larger than the largest of them: an AIHC -O2
  # build preprocesses tens of times faster than an -O0 one. GHC does the
  # whole 75 MB corpus in two seconds, and --report still covers all of it.
  sampleBytes ? 32 * 1024 * 1024,
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
  pkgs.runCommand "aihc-cpp-corpus-${snapshot.snapshot}" {
    nativeBuildInputs = [pkgs.python3];
    tarballsJson = builtins.toJSON tarballs;
    passAsFile = ["tarballsJson"];
    snapshot = snapshotFile;
    include = ./include;
    script = ./build.py;
    sampleBytes = toString sampleBytes;
    passthru = {inherit snapshot tarballs;};
  } ''
    python3 "$script" \
      --snapshot "$snapshot" \
      --tarballs "$tarballsJsonPath" \
      --include "$include" \
      --out "$out" \
      --sample-bytes "$sampleBytes"
  ''
