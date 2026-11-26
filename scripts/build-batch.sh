#!/usr/bin/env bash
# Like scripts/build.sh, but only for "Apps & tools" (top-level packages and
# the nested sets in APP_SETS, as in Dendrite's search tags) and spread over
# several runs: each run evaluates the next BATCH_SIZE attrs and adds them
# to the package list. appstream-generator skips packages already in its
# database, so a run costs about one batch. The run that completes the list
# exports the catalogue into workspace/out/$SECTION, with what it lacks
# filled in from Flathub's (scripts/merge-flathub.sh); scripts/run-batches.sh
# runs batches until then.
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

mkdir -p workspace/nix-index
WS="$(pwd)/workspace"
STATE="$WS/batch-state"
PASS="$WS/pass"

# The generator writes "{}" as the listing of a store path it fails to list
# (it drops nix's error) and never lists that path again.
failed_listings() {
  find "$WS/nix-index" -maxdepth 1 -name '*.json' -size -8c -print0 \
    | xargs -0r grep -lxF -e '{}' || true
}

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

# Progress through the pass, counted in attrs; $1 is how many are done.
status_lines() {
  echo "== $2: nixpkgs $REV =="
  echo "  Already processed: $1 of $TOTAL"
  echo "  This run:          $(( NEXT - DONE )) (attrs $(( DONE + 1 ))..$NEXT)"
  echo "  Still to process:  $(( TOTAL - $1 ))"
  echo "  Failed listings:   $(failed_listings | wc -l) (retried next run)"
  # Apparent size, as tar stores it: the LMDB file may be sparse.
  echo "  Workspace size:    $(du -sh --apparent-size "$WS" | cut -f1) (uncompressed)"
  # The repo's whole Actions cache; GitHub updates this figure with a delay.
  if [[ -n "${GH_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]] && command -v gh > /dev/null; then
    gh api "repos/$GITHUB_REPOSITORY/actions/cache/usage" \
      --jq '"  Actions cache:     \(.active_caches_size_in_bytes / 1e7 | floor / 100) GB in \(.active_caches_count) entries (limit 10 GB by default)"' \
      || echo "  Actions cache:     usage not available"
  fi
}
# Also onto the run's summary page in GitHub Actions, as the log is long.
status() {
  local lines
  lines=$(status_lines "$@")
  echo "$lines"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '```\n%s\n```\n' "$lines" >> "$GITHUB_STEP_SUMMARY"
  fi
}
status "$DONE" "Before"

# Only the run that completes a pass leaves a catalogue here (and
# build-unfree.sh one in out/$SECTION-unfree).
rm -rf "$WS/out/$SECTION"

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
# Failed ones are listed again.
failed_listings | xargs -r rm -f
ln -sfn "$WS/nix-index" "$PKGROOT/index"

if [[ "$ENABLE_NAR_CACHE" == "true" ]]; then
  # Nix needs an absolute path here; with a relative one every
  # `nix store ls` and `nix store cat` fails, and nothing is processed.
  ARCHIVE_ROOT="https://cache.nixos.org?local-nar-cache=$WS/narcache"
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
# Collapsed in the Actions log; the whole output is also in the uploaded log.
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::group::appstream-generator output"
# Appended to: run-batches.sh starts the log afresh for each run.
unbuffer appstream-generator process nixpkgs --verbose 2>&1 | tee -a appstream-generator.log
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::endgroup::"
# Later batches don't read these archives again.
rm -rf narcache
popd

status "$NEXT" "After"

if (( NEXT < TOTAL )); then
  echo "$REV $NEXT" > "$STATE"
  exit 0
fi

# Pass complete: export, and drop results for packages no longer listed.
# `process` skips exporting when a batch has nothing new with metadata, so
# export the whole section from the database here.
pushd workspace
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::group::appstream-generator publish output"
unbuffer appstream-generator publish nixpkgs --verbose 2>&1 | tee -a appstream-generator.log
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::endgroup::"
popd

EXPORT="workspace/export/data/nixpkgs/$REV"
if ! compgen -G "$EXPORT/Components*.gz" > /dev/null; then
  {
    echo "No catalogue in $EXPORT; nothing to publish."
    echo "workspace/export holds:"
    find workspace/export -maxdepth 4 || true
    echo "End of workspace/appstream-generator.log:"
    tail -n 60 workspace/appstream-generator.log
  } >&2
  exit 1
fi
# scripts/publish-release.sh publishes what is in out/.
OUT="$WS/out/$SECTION"
mkdir -p "$OUT"
cp "$EXPORT"/Components*.gz "$OUT"/
cp "$EXPORT"/icons* "$OUT"/

for f in "$OUT"/Components*.gz; do
  "$SCRIPTS/merge-flathub.sh" "$f" "$SECTION" "$PKGROOT/packages.json"
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
echo "Pass complete: catalogue of nixpkgs $REV in $OUT."
