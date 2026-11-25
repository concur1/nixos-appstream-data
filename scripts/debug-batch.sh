#!/usr/bin/env bash
# Runs build-batch.sh's steps on a few attrs in a scratch workspace and shows
# what appstream-generator makes of them, e.g.
#
#   nix shell .#appstream-generator nixpkgs#jq nixpkgs#expect \
#     --command scripts/debug-batch.sh firefox gnome-calculator
#
# REV picks the nixpkgs revision (default: the current $SECTION channel).
set -euo pipefail

cd "$(dirname "$0")/.."
SCRIPTS="$(pwd)/scripts"

if (( $# == 0 )); then
  echo "Usage: $0 ATTR..." >&2
  exit 1
fi
SECTION="${SECTION:-nixos-unstable}"
REV="${REV:-$(nix flake metadata "nixpkgs/$SECTION" --json | jq -r '.revision')}"
NIXPKGS_PATH=$(nix eval --raw "github:nixos/nixpkgs/$REV#path")

WS=$(mktemp -d "${TMPDIR:-/tmp}/asgen-debug.XXXXXX")
echo "Workspace: $WS"
printf '%s\n' "$@" > "$WS/attrs.txt"

# As in build-batch.sh.
PKGROOT="$WS/cache/nixpkgs/$REV/x86_64-linux"
mkdir -p "$PKGROOT" "$WS/nix-index"
nix-env -qaP --out-path --meta --json --file "$SCRIPTS/batch.nix" \
  --arg nixpkgsPath "$NIXPKGS_PATH" --arg attrsFile "$WS/attrs.txt" \
  | jq -c '{version: 2, packages: .}' > "$PKGROOT/packages.json"
ln -sfn "$WS/nix-index" "$PKGROOT/index"

cat > "$WS/asgen-config.json" << JSON
{
  "ProjectName": "Nix",
  "ArchiveRoot": "https://cache.nixos.org?local-nar-cache=$WS/narcache",
  "Backend": "nix",
  "Features": {
    "processDesktop": true,
    "validateMetainfo": true,
    "createScreenshotsStore": false,
    "noDownloads": false
  },
  "Suites": {
    "nixpkgs": {
      "sections": [
        "$REV"
      ],
      "architectures": [
        "x86_64-linux"
      ]
    }
  }
}
JSON

(cd "$WS" && unbuffer appstream-generator process nixpkgs --verbose > appstream-generator.log 2>&1) \
  || echo "appstream-generator failed: $?"

echo
echo "== packages.json, without meta"
jq '.packages | map_values(del(.meta))' "$PKGROOT/packages.json"
echo
echo "== Store listings in nix-index/"
ls -la "$WS/nix-index"
for f in "$WS"/nix-index/*.json; do
  [[ -e "$f" ]] || continue
  echo "-- $(basename "$f"): $(wc -c < "$f") bytes; start:"
  head -c 600 "$f"
  echo
  echo "-- desktop/metainfo files in it:"
  grep -oE '"(applications|metainfo|appdata)"|[^"]*\.desktop"|[^"]*\.metainfo\.xml"' "$f" | head || echo "(none)"
done
echo
echo "== Generator log"
cat "$WS/appstream-generator.log"
echo
echo "== Export"
find "$WS/export" -maxdepth 4 2>/dev/null || echo "(no export directory)"
