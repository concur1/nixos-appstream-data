{
  section ? "nixos-unstable",
  lib,
  stdenv,
  fetchurl,
  linkFarm,
}:
let
  # Catalogues published as release assets by scripts/publish-release.sh:
  # one tarball of the share/swcatalog/ tree, or, from releases before that,
  # the generator's files one by one. Sections not published that way yet
  # use their copy in appstream/.
  sources = lib.importJSON ./appstream/sources.json;
  source = sources.${section} or { };
  packed = source ? url;
  src =
    if packed then
      fetchurl { inherit (source) url hash; }
    else if source ? files then
      linkFarm "appstream-${section}" (
        lib.mapAttrsToList (name: file: {
          inherit name;
          path = fetchurl { inherit (file) url hash; };
        }) source.files
      )
    else
      ./appstream + "/${section}";
in
stdenv.mkDerivation {
  pname = "nixos-appstream-data";
  version = "0.0.1";

  inherit src;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/swcatalog
  ''
  + (
    if packed then
      ''
        tar -xzf $src -C $out/share/swcatalog
      ''
    else
      ''
        mkdir -p $out/share/swcatalog/{icons/${section},xml}
        cp $src/Components-*.xml.gz $out/share/swcatalog/xml/${section}.xml.gz
        for tarball in $src/icons-*.tar.gz; do
          [[ -e "$tarball" ]] || continue
          size=$(basename "$tarball" .tar.gz | sed 's/icons-//')
          mkdir -p $out/share/swcatalog/icons/${section}/$size
          tar -xzf "$tarball" -C $out/share/swcatalog/icons/${section}/$size/
        done
      ''
  )
  + ''
    runHook postInstall
  '';
}
