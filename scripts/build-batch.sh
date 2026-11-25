#!/usr/bin/env bash
# Like scripts/build.sh, but only for "Apps & tools" (top-level packages and
# the nested sets in APP_SETS, as in Dendrite's search tags) and spread over
# several runs: each run evaluates the next BATCH_SIZE attrs and adds them
# to the package list. appstream-generator skips packages already in its
# database, so a run costs about one batch. The run that completes the list
# exports the catalogue into appstream/$SECTION.
#
# Evaluating store paths a batch at a time also keeps memory low: a full
# `nix-env -qaP --out-path` of nixpkgs needs more than 9 GB.
#
# Keep workspace/ between runs (db, export/media, cache, pass, state); it is
# the only state. One pass stays on one nixpkgs revision.
set -euo pipefail

cd "$(dirname "$0")/.."
SCRIPTS="$(pwd)/scripts"

SECTION="${SECTION:-nixos-unstable}"
if [[ ! "$SECTION" =~ ^[A-Za-z0-9._-]+$ || "$SECTION" == *..* ]]; then
  echo "Invalid SECTION: $SECTION" >&2
  exit 1
fi
BATCH_SIZE="${BATCH_SIZE:-2000}"
# Nested sets that hold apps; other nested attrs are libraries and plugins.
# Read at the start of a pass.
APP_SETS="${APP_SETS:-kdePackages pantheon lxqt enlightenment}"
ENABLE_NAR_CACHE="${ENABLE_NAR_CACHE:-true}"

mkdir -p workspace
WS="$(pwd)/workspace"
STATE="$WS/batch-state"
PASS="$WS/pass"

# "<revision> <attrs done>" while a pass is under way.
if [[ -f "$STATE" ]]; then
  read -r REV DONE < "$STATE"
else
  REV=$(nix flake metadata "nixpkgs/$SECTION" --json | jq -r '.revision')
  DONE=0
fi
NIXPKGS_PATH=$(nix eval --raw "github:nixos/nixpkgs/$REV#path")

# A new pass: the attrs to go through, one per line.
if [[ "$DONE" == 0 ]]; then
  rm -rf "$PASS"
  mkdir -p "$PASS"
  nix-instantiate --eval --strict --json "$SCRIPTS/app-attrs.nix" \
    --arg nixpkgsPath "$NIXPKGS_PATH" --argstr appSets "$APP_SETS" \
    | jq -r '.[]' > "$PASS/attrs.txt"
fi
TOTAL=$(wc -l < "$PASS/attrs.txt")
NEXT=$(( DONE + BATCH_SIZE < TOTAL ? DONE + BATCH_SIZE : TOTAL ))
echo "nixpkgs $REV: attrs $DONE..$NEXT of $TOTAL"

# This batch's packages, listed the way the generator lists them.
BATCH="$PASS/batch-$(printf '%06d' "$DONE").json"
sed -n "$(( DONE + 1 )),${NEXT}p" "$PASS/attrs.txt" > "$PASS/batch-attrs.txt"
nix-env -qaP --out-path --meta --json --file "$SCRIPTS/batch.nix" \
  --arg nixpkgsPath "$NIXPKGS_PATH" --arg attrsFile "$PASS/batch-attrs.txt" \
  > "$BATCH.tmp"
mv "$BATCH.tmp" "$BATCH"

# Only the old revisions' package lists go; the database keeps results for
# packages whose version didn't change.
mkdir -p "$WS/cache/nixpkgs"
find "$WS/cache/nixpkgs" -mindepth 1 -maxdepth 1 ! -name "$REV" -exec rm -rf {} +

# What the generator reads: every batch so far.
PKGROOT="$WS/cache/nixpkgs/$REV/x86_64-linux"
mkdir -p "$PKGROOT"
jq -cs '{version: 2, packages: add}' "$PASS"/batch-*.json > "$PKGROOT/packages.json"

# Store path listings never change, so they are shared across revisions.
mkdir -p "$WS/nix-index"
ln -sfn "$WS/nix-index" "$PKGROOT/index"

if [[ "$ENABLE_NAR_CACHE" == "true" ]]; then
  ARCHIVE_ROOT="https://cache.nixos.org?local-nar-cache=./narcache"
else
  ARCHIVE_ROOT="https://cache.nixos.org"
fi

cat > "$WS/asgen-config.json" << JSON
{
  "ProjectName": "Nix",
  "ArchiveRoot": "$ARCHIVE_ROOT",
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

pushd workspace
unbuffer appstream-generator process nixpkgs --verbose 2>&1 | tee appstream-generator.log
# Later batches don't read these archives again.
rm -rf narcache
popd

if (( NEXT < TOTAL )); then
  echo "$REV $NEXT" > "$STATE"
  echo "Batch done; $(( TOTAL - NEXT )) attrs left in this pass."
  exit 0
fi

# Pass complete: export, and drop results for packages no longer listed.
rm -rf appstream/"$SECTION"
mkdir -p appstream/"$SECTION"
cp workspace/export/data/nixpkgs/"$REV"/Components*.gz appstream/"$SECTION"/
cp workspace/export/data/nixpkgs/"$REV"/icons* appstream/"$SECTION"/

for f in appstream/"$SECTION"/Components*.gz; do
  gunzip "$f"
  sed -i "s/origin=\"[^\"]*\"/origin=\"$SECTION\"/" "${f%.gz}"
  gzip "${f%.gz}"
done

(cd workspace && appstream-generator cleanup)

# Listings of store paths no longer in the list.
jq -r '.packages[].outputs[]? | select(type == "string") | ltrimstr("/nix/store/") + ".json"' \
  "$PKGROOT/packages.json" | sort -u > "$PASS/index-keep"
find "$WS/nix-index" -maxdepth 1 -name '*.json' -printf '%f\n' | sort \
  | comm -23 - "$PASS/index-keep" \
  | sed "s|^|$WS/nix-index/|" | xargs -r rm -f

rm -f "$STATE"
echo "$REV" > "$WS/last-pass"
echo "Pass complete: appstream/$SECTION updated from nixpkgs $REV."
