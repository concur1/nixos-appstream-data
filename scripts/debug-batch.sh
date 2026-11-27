#!/usr/bin/env bash
# Runs build-batch.sh's steps on a few attrs in a scratch workspace and shows
# what appstream-generator makes of them: its hints (why a package gives no
# component), the components and the log, e.g.
#
#   nix shell .#appstream-generator nixpkgs#jq nixpkgs#expect \
#     --command scripts/debug-batch.sh firefox gnome-calculator
#
# REV picks the nixpkgs revision (default: the current $SECTION channel),
# DEBUG_WS the workspace (default: a new temporary directory).
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

WS="${DEBUG_WS:-$(mktemp -d "${TMPDIR:-/tmp}/asgen-debug.XXXXXX")}"
mkdir -p "$WS"
echo "Workspace: $WS"
printf '%s\n' "$@" > "$WS/attrs.txt"

# As in build-batch.sh, with the stand-in descriptions.
PKGROOT="$WS/cache/nixpkgs/$REV/x86_64-linux"
mkdir -p "$PKGROOT" "$WS/nix-index"
nix-env -qaP --out-path --meta --json --file "$SCRIPTS/batch.nix" \
  --arg nixpkgsPath "$NIXPKGS_PATH" --arg attrsFile "$WS/attrs.txt" \
  | jq -c '{version: 2, packages: .}' > "$PKGROOT/packages.json"
"$SCRIPTS/fetch-flathub.sh" "$WS/flathub.xml.gz"
echo "== Given the summary as long description:"
uv run "$SCRIPTS/flathub_merge.py" --flathub "$WS/flathub.xml.gz" describe "$PKGROOT/packages.json"
ln -sfn "$WS/nix-index" "$PKGROOT/index"

"$SCRIPTS/asgen-config.sh" "$WS" "$REV" "https://cache.nixos.org?local-nar-cache=$WS/narcache"

for command in process publish; do
  (cd "$WS" && unbuffer appstream-generator "$command" nixpkgs --verbose >> appstream-generator.log 2>&1) \
    || echo "appstream-generator $command failed: $?"
done

echo
echo "== Hints, by package"
for f in "$WS"/export/hints/nixpkgs/"$REV"/Hints-*.json*; do
  [[ -e "$f" ]] || continue
  if [[ "$f" == *.gz ]]; then gunzip -c "$f"; else cat "$f"; fi | jq .
done
echo
echo "== Components (id, package, icons)"
for f in "$WS"/export/data/nixpkgs/"$REV"/Components-*.xml.gz; do
  [[ -e "$f" ]] || continue
  gunzip -c "$f" | grep -E '<id>|<pkgname>|<icon ' || echo "(none)"
done
echo
echo "== Store listings: share/ of each package"
for f in "$WS"/nix-index/*.json; do
  [[ -e "$f" ]] || continue
  echo "-- $(basename "$f")"
  jq -c '.entries.share | if .type == "directory" then {type, entries: (.entries | keys)} else . end' "$f"
done
echo
echo "== Generator log"
grep -vE 'Rendering HTML page' "$WS/appstream-generator.log"
