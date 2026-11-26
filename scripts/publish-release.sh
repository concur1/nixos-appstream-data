#!/usr/bin/env bash
# Publishes each catalogue in workspace/out/<section>/ (left by the run that
# completes a pass) as a GitHub release, and points appstream/sources.json at
# its files; default.nix fetches them from there. Also drops the section's
# old copy in appstream/<section>/ and all but the newest KEEP_RELEASES of
# its releases. Leaves the changes staged for the caller to commit.
#
# Needs gh with a token that can write contents, and GITHUB_REPOSITORY.
set -euo pipefail

cd "$(dirname "$0")/.."

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
KEEP_RELEASES="${KEEP_RELEASES:-10}"
OUT=workspace/out
MANIFEST=appstream/sources.json

if ! compgen -G "$OUT/*/" > /dev/null; then
  echo "No catalogue in $OUT; nothing to publish."
  exit 0
fi
REV=$(cat workspace/last-pass)

for dir in "$OUT"/*/; do
  section=$(basename "$dir")
  tag="$section-$(date -u +%Y%m%d-%H%M%S)"
  base="https://github.com/$REPO/releases/download/$tag"

  # Release assets are named <section>.<file>, without "@" (as in
  # icons-64x64@2.tar.gz), which isn't safe in asset names.
  stage=$(mktemp -d)
  files='{}'
  for f in "$dir"*; do
    name=$(basename "$f")
    asset="$section.${name//@/-at-}"
    cp "$f" "$stage/$asset"
    files=$(jq --arg name "$name" --arg url "$base/$asset" \
      --arg hash "$(nix-hash --flat --type sha256 --sri "$f")" \
      '.[$name] = {url: $url, hash: $hash}' <<< "$files")
  done

  gh release create "$tag" "$stage"/* --repo "$REPO" \
    --title "$section: nixpkgs ${REV:0:12}" \
    --notes "AppStream catalogue for $section, from nixpkgs $REV."
  rm -rf "$stage"

  [[ -f "$MANIFEST" ]] || echo '{}' > "$MANIFEST"
  jq --arg section "$section" --arg tag "$tag" --arg rev "$REV" --argjson files "$files" \
    '.[$section] = {release: $tag, revision: $rev, files: $files}' "$MANIFEST" > "$MANIFEST.tmp"
  mv "$MANIFEST.tmp" "$MANIFEST"
  git add "$MANIFEST"
  git rm -rq --ignore-unmatch "appstream/$section"

  # This section's releases only: the tag is <section>-<date>-<time>, so
  # nixos-unstable doesn't take in nixos-unstable-unfree's.
  gh release list --repo "$REPO" --limit 1000 --json tagName,createdAt \
    | jq -r --arg section "$section" --argjson keep "$KEEP_RELEASES" '
        map(select(.tagName | startswith($section + "-")
          and (ltrimstr($section + "-") | test("^[0-9]{8}-[0-9]{6}$"))))
        | sort_by(.createdAt) | reverse | .[$keep:][] | .tagName' \
    | while read -r old; do
        echo "Deleting old release $old"
        gh release delete "$old" --repo "$REPO" --cleanup-tag --yes
      done
done
