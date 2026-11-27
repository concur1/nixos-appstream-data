#!/usr/bin/env bash
# The unfree catalogue ($SECTION-unfree) for the nixpkgs revision in
# workspace/unfree/pending, which scripts/run-batches.sh writes when a pass
# completes. Hydra builds no unfree packages, so they are built here: those
# of the pass's attrs that are unfree and match a Flathub app
# (flathub_merge.py select). Flathub's metadata then fills in what their
# .desktop files leave out, as for the free catalogue.
#
# One package at a time, each processed by appstream-generator straight
# from the local store and then left to the garbage collector, as some are
# huge. Packages whose store path has a listing from an earlier pass are
# unchanged and not built again. Stops before DEADLINE (seconds since the
# epoch) and goes on next run; the catalogue goes to workspace/out/ once
# every package is done.
set -euo pipefail

cd "$(dirname "$0")/.."
SCRIPTS="$(pwd)/scripts"

SECTION="${SECTION:-nixos-unstable}"
UNFREE="$SECTION-unfree"
DEADLINE="${DEADLINE:-$(( $(date +%s) + 3600 ))}"
# A build that takes longer is given up (and not tried again this pass).
BUILD_TIMEOUT="${UNFREE_BUILD_TIMEOUT:-1800}"
# Below this, the store is garbage collected before the next build.
MIN_FREE_GB="${UNFREE_MIN_FREE_GB:-20}"

WS="$(pwd)/workspace"
UWS="$WS/unfree"
REV=$(cat "$UWS/pending")
NIXPKGS_PATH=$(nix eval --raw "github:nixos/nixpkgs/$REV#path")
# The garbage collection below would take it otherwise. The tools in PATH
# are safe: nix treats paths in running processes' environments as roots.
nix-store --add-root "$UWS/nixpkgs" --realise "$NIXPKGS_PATH" > /dev/null

PKGROOT="$UWS/cache/nixpkgs/$REV/x86_64-linux"
UNFREE_PACKAGES="$UWS/unfree-packages.json"
# One copy for the whole unfree pass, as for a free one.
FLATHUB="$UWS/flathub.xml.gz"
: > "$UWS/appstream-generator.log"

summary() {
  echo "$1"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}

# A new unfree pass: what to build. Most unfree packages have no long
# description, so they get their summary as one (as in build-batch.sh).
if [[ "$(cat "$UWS/rev" 2> /dev/null)" != "$REV" ]]; then
  PASS_PACKAGES="$WS/cache/nixpkgs/$REV/x86_64-linux/packages.json"
  if [[ ! -f "$PASS_PACKAGES" ]]; then
    summary "No package list of nixpkgs $REV left; no unfree catalogue for it."
    echo "$REV" > "$UWS/rev"
    rm -f "$UWS/pending"
    exit 0
  fi
  rm -f "$UWS"/{queue,done,failed}.txt "$FLATHUB"
  # Unfree attrs have no store paths in the pass's package list, as
  # packages-config.nix doesn't allow them; with allowUnfree they do.
  jq -r '.packages | to_entries[] | select(.value.outputs == null) | .key' \
    "$PASS_PACKAGES" > "$UWS/no-outputs.txt"
  nix-env -qaP --out-path --meta --json --file "$SCRIPTS/batch.nix" \
    --arg nixpkgsPath "$NIXPKGS_PATH" --arg attrsFile "$UWS/no-outputs.txt" --arg allowUnfree true \
    | jq -c '{version: 2, packages: .}' > "$UNFREE_PACKAGES"
  "$SCRIPTS/fetch-flathub.sh" "$FLATHUB"
  uv run "$SCRIPTS/flathub_merge.py" --flathub "$FLATHUB" describe "$UNFREE_PACKAGES" > /dev/null
  uv run "$SCRIPTS/flathub_merge.py" --flathub "$FLATHUB" select --packages "$UNFREE_PACKAGES" > "$UWS/queue.txt"
  touch "$UWS"/{done,failed}.txt
  echo "$REV" > "$UWS/rev"
fi

# What the generator reads: the packages done so far, plus $1 if given.
# Packages not in the local store mustn't be in it: the generator would
# record a failed listing for them and never list them again.
write_packages() {
  mkdir -p "$PKGROOT" "$UWS/nix-index"
  ln -sfn "$UWS/nix-index" "$PKGROOT/index"
  jq -c --rawfile finished "$UWS/done.txt" --arg extra "${1:-}" '
    ($finished | split("\n") + [$extra] | map(select(. != ""))) as $keep
    | {version: 2, packages: (.packages | with_entries(select(.key as $k | $keep | index($k))))}' \
    "$UNFREE_PACKAGES" > "$PKGROOT/packages.json"
}

# The generator's listings of attr $1's outputs.
listings() {
  jq -r --arg a "$1" '.packages[$a].outputs[] | ltrimstr("/nix/store/") + ".json"' "$UNFREE_PACKAGES" \
    | sed "s|^|$UWS/nix-index/|"
}
# Whether all of them are there. The generator writes "{}" for a path it
# failed to list.
listed() {
  local f
  for f in $(listings "$1"); do
    [[ -s "$f" ]] && ! grep -qxF '{}' "$f" || return 1
  done
}

# The generator lists the target of a symlinked share/ or share/applications/
# each time it reads the package list (nixindexutils.cpp), and once garbage
# collected, or on the next runner, the target is gone and so is the
# package. So the listing of attr $1 gets the target's contents in place of
# the link, while it is still here.
inline_links() {
  local f key target
  for f in $(listings "$1"); do
    for key in .entries.share .entries.share.entries.applications; do
      target=$(jq -r "$key | select(.type? == \"symlink\") | .target" "$f")
      [[ "$target" == /nix/store/* ]] || continue
      nix store ls --store auto --recursive --json "$target" > "$f.target"
      jq --slurpfile t "$f.target" "$key = (\$t[0] + {type: \"directory\"})" "$f" > "$f.tmp"
      mv "$f.tmp" "$f"
      rm -f "$f.target"
    done
  done
}

"$SCRIPTS/asgen-config.sh" "$UWS" "$REV" auto

asgen() {
  (cd "$UWS" && unbuffer appstream-generator "$@" --verbose >> appstream-generator.log 2>&1)
}

free_gb() {
  df --output=avail -B1G /nix/store | tail -n1 | tr -d ' '
}

built=0
unchanged=0
mapfile -t QUEUE < "$UWS/queue.txt"
for attr in "${QUEUE[@]}"; do
  if grep -qxF "$attr" "$UWS/done.txt" "$UWS/failed.txt"; then
    continue
  fi
  # Room for a build that runs to the timeout, and for processing it.
  if (( $(date +%s) + BUILD_TIMEOUT + 300 > DEADLINE )); then
    break
  fi
  if (( $(free_gb) < MIN_FREE_GB )); then
    nix-store --gc > /dev/null
    if (( $(free_gb) < MIN_FREE_GB )); then
      summary "Stopping: $(free_gb) GB free after garbage collection."
      break
    fi
  fi

  # Unchanged since an earlier pass: the generator has its results.
  if listed "$attr"; then
    echo "$attr" >> "$UWS/done.txt"
    unchanged=$(( unchanged + 1 ))
    continue
  fi

  echo "== Building $attr"
  printf '%s\n' "$attr" > "$UWS/attr.txt"
  if ! timeout "$BUILD_TIMEOUT" nix-build "$SCRIPTS/batch.nix" --no-out-link \
      --arg nixpkgsPath "$NIXPKGS_PATH" --arg attrsFile "$UWS/attr.txt" --arg allowUnfree true \
      -A "$attr" > /dev/null; then
    echo "$attr" >> "$UWS/failed.txt"
    continue
  fi
  # It never lists a path again otherwise.
  listings "$attr" | xargs -r rm -f
  write_packages "$attr"
  asgen process nixpkgs
  if listed "$attr"; then
    inline_links "$attr"
    echo "$attr" >> "$UWS/done.txt"
    built=$(( built + 1 ))
  else
    echo "$attr" >> "$UWS/failed.txt"
  fi
done

left=$(grep -cvxF -f <(cat "$UWS/done.txt" "$UWS/failed.txt") "$UWS/queue.txt" || true)
summary "### Unfree packages: nixpkgs $REV"
summary ""
summary "$(wc -l < "$UWS/queue.txt") match a Flathub app; this run built $built and found $unchanged unchanged."
summary "Done: $(wc -l < "$UWS/done.txt"); failed: $(wc -l < "$UWS/failed.txt"); still to do: $left."
if [[ -s "$UWS/failed.txt" ]]; then
  summary ""
  summary "Failed (not tried again this pass): $(tr '\n' ' ' < "$UWS/failed.txt")"
fi
if (( left > 0 )); then
  exit 0
fi
if [[ ! -s "$UWS/done.txt" ]]; then
  summary "No unfree package was processed; no unfree catalogue for this pass."
  rm -f "$UWS/pending"
  exit 0
fi

# All done. A pass begun before build-unfree.sh fetched Flathub's catalogue
# at its start has none yet.
write_packages
"$SCRIPTS/fetch-flathub.sh" "$FLATHUB"
"$SCRIPTS/export-catalogue.sh" "$UWS" "$REV" "$UNFREE" "$PKGROOT/packages.json" "$FLATHUB"
find "$UWS/cache/nixpkgs" -mindepth 1 -maxdepth 1 ! -name "$REV" -exec rm -rf {} +
rm -f "$UWS/pending"
echo "Unfree catalogue of nixpkgs $REV in workspace/out/$UNFREE."
