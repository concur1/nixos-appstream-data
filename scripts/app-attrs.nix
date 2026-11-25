# Attr paths of the "Apps & tools" part of nixpkgs: top-level packages and
# packages in the nested sets in `appSets` (space-separated). Only names, so
# no store paths are computed; build-batch.sh evaluates those a batch at a
# time (batch.nix), which a full `nix-env -qaP --out-path` needs more than
# 9 GB of memory for.
{ nixpkgsPath, appSets }:
let
  pkgs = import nixpkgsPath {
    config = import (nixpkgsPath + "/pkgs/top-level/packages-config.nix");
  };
  inherit (pkgs) lib;

  isDerivation =
    v:
    let
      r = builtins.tryEval (lib.isDerivation v);
    in
    r.success && r.value;

  packagesIn = set: lib.filter (n: isDerivation set.${n}) (lib.attrNames set);

  sets = lib.filter (s: s != "" && pkgs ? ${s}) (lib.splitString " " appSets);
in
lib.sort lib.lessThan (
  packagesIn pkgs ++ lib.concatMap (s: map (n: "${s}.${n}") (packagesIn pkgs.${s})) sets
)
