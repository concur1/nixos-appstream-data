# The packages named in `attrsFile` (one attr path per line), for
# `nix-env -qaP`. Unfree ones have no store paths unless `allowUnfree`.
{
  nixpkgsPath,
  attrsFile,
  allowUnfree ? false,
}:
let
  pkgs = import nixpkgsPath {
    config = import (nixpkgsPath + "/pkgs/top-level/packages-config.nix") // {
      inherit allowUnfree;
    };
  };
  inherit (pkgs) lib;

  wanted = lib.filter (x: x != "") (lib.splitString "\n" (builtins.readFile attrsFile));
  setByPath =
    path:
    let
      p = lib.splitString "." path;
    in
    lib.setAttrByPath p (lib.attrByPath p null pkgs);
  # Nested sets have to be marked for nix-env to look inside them.
  markRecursive = lib.mapAttrs (
    _: v: if lib.isDerivation v || !lib.isAttrs v then v else lib.recurseIntoAttrs (markRecursive v)
  );
in
markRecursive (lib.foldl' lib.recursiveUpdate { } (map setByPath wanted))
