# The MicroHs inputs of the microhs-self-compile benchmark, from one MicroHs
# release on Hackage.
#
# The corpus is what MicroHs's Makefile hands the compiler when it
# regenerates generated/mhs.c: src and mhs, the compiler as MicroHs builds
# itself, and lib, its own Prelude and base library. The C runtime under
# src/runtime is left out; generating C reads none of it.
#
# The package (passthru.package) is the unpacked release, as Hackage serves
# it. Each compiler builds its mhs executable without changes, and the
# benchmark runs mhs on the corpus.
{pkgs}: let
  version = "0.16.0.0";
  tarball = pkgs.fetchurl {
    url = "mirror://hackage/MicroHs-${version}.tar.gz";
    sha256 = "d8cfa846765ee091098abb2abb83aa36ee0c1fbb57ac8a53c43c169761493155";
  };
  package = pkgs.runCommand "aihc-microhs-package-${version}" {} ''
    tar -xzf ${tarball}
    mv MicroHs-${version} "$out"
  '';
in
  pkgs.runCommand "aihc-microhs-corpus-${version}" {
    passthru = {inherit tarball version package;};
  } ''
    tar -xzf ${tarball}
    mkdir "$out"
    cp -R MicroHs-${version}/lib MicroHs-${version}/mhs MicroHs-${version}/src MicroHs-${version}/LICENSE "$out/"
    rm -r "$out/src/runtime"
  ''
