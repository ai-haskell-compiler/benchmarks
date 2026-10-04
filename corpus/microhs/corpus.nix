# The MicroHs corpus: the Haskell sources of a MicroHs release, which the
# microhs-self-compile benchmark compiles to C with a MicroHs built from the
# same release (vendored under benchmarks/microhs-self-compile/microhs).
#
# The output is what MicroHs's Makefile hands the compiler when it
# regenerates generated/mhs.c: src and mhs, the compiler as MicroHs builds
# itself, and lib, its own Prelude and base library. The C runtime under
# src/runtime is left out; generating C reads none of it.
{pkgs}: let
  version = "0.16.0.0";
  tarball = pkgs.fetchurl {
    url = "mirror://hackage/MicroHs-${version}.tar.gz";
    sha256 = "d8cfa846765ee091098abb2abb83aa36ee0c1fbb57ac8a53c43c169761493155";
  };
in
  pkgs.runCommand "aihc-microhs-corpus-${version}" {
    passthru = {inherit tarball version;};
  } ''
    tar -xzf ${tarball}
    mkdir "$out"
    cp -R MicroHs-${version}/lib MicroHs-${version}/mhs MicroHs-${version}/src MicroHs-${version}/LICENSE "$out/"
    rm -r "$out/src/runtime"
  ''
