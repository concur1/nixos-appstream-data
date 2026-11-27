#!/usr/bin/env bash
# The end of a pass, free (build-batch.sh) or unfree (build-unfree.sh):
# exports the catalogue of nixpkgs revision $2 from generator workspace $1
# into workspace/out/$3/ for scripts/publish-release.sh, fills in what it
# lacks from Flathub's catalogue $5 (scripts/flathub_merge.py), and drops
# what the generator has of packages no longer in packages.json $4.
set -euo pipefail

cd "$(dirname "$0")/.."
GWS=$1
REV=$2
SECTION=$3
PACKAGES=$4
FLATHUB=$5

# `process` skips exporting when a batch has nothing new with metadata, so
# the whole section is exported from the database here.
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::group::appstream-generator publish output"
(cd "$GWS" && unbuffer appstream-generator publish nixpkgs --verbose 2>&1 | tee -a appstream-generator.log)
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::endgroup::"

EXPORT="$GWS/export/data/nixpkgs/$REV"
if ! compgen -G "$EXPORT/Components*.gz" > /dev/null; then
  {
    echo "No catalogue in $EXPORT; nothing to publish. $GWS/export holds:"
    find "$GWS/export" -maxdepth 4 || true
    echo "End of $GWS/appstream-generator.log:"
    tail -n 60 "$GWS/appstream-generator.log"
  } >&2
  exit 1
fi
OUT="workspace/out/$SECTION"
rm -rf "$OUT"
mkdir -p "$OUT"
cp "$EXPORT"/Components*.gz "$EXPORT"/icons* "$OUT"/
# Licences are read per Flathub commit, and kept with the workspace.
for f in "$OUT"/Components*.gz; do
  uv run scripts/flathub_merge.py --flathub "$FLATHUB" merge "$f" --section "$SECTION" \
    --packages "$PACKAGES" --license-cache workspace/flathub-licenses.json
done
# The unfree catalogue can be of an older revision than the last pass.
echo "$REV" > "$OUT/revision"

(cd "$GWS" && appstream-generator cleanup)

# Store listings of paths no longer in the list.
jq -r '.packages[].outputs[]? | select(type == "string") | ltrimstr("/nix/store/") + ".json"' "$PACKAGES" \
  | sort -u > "$GWS/index-keep"
find "$GWS/nix-index" -maxdepth 1 -name '*.json' -printf '%f\n' | sort \
  | comm -23 - "$GWS/index-keep" \
  | sed "s|^|$GWS/nix-index/|" | xargs -r rm -f
