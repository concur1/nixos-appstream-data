#!/usr/bin/env bash
# Publishes each catalogue in workspace/out/<section>/ (left by the run that
# completes a pass) as a GitHub release, and points appstream/sources.json at
# it; default.nix fetches it from there. The release holds one tarball,
# <section>.tar.gz, laid out as share/swcatalog/ is: xml/<section>.xml.gz and
# icons/<section>/<size>/. Also drops the section's old copy in
# appstream/<section>/ and all but the newest KEEP_RELEASES of its releases.
# Leaves the changes staged for the caller to commit, and a line for each
# section published in workspace/published.txt.
#
# Needs gh with a token that can write contents, and GITHUB_REPOSITORY.
set -euo pipefail

cd "$(dirname "$0")/.."

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
KEEP_RELEASES="${KEEP_RELEASES:-10}"
OUT=workspace/out
MANIFEST=appstream/sources.json

PUBLISHED=workspace/published.txt
: > "$PUBLISHED"
if ! compgen -G "$OUT/*/" > /dev/null; then
  echo "No catalogue in $OUT; nothing to publish."
  exit 0
fi

for dir in "$OUT"/*/; do
  section=$(basename "$dir")
  # The nixpkgs revision it was made from (the unfree catalogue can be of an
  # older one than the last pass). One without was left by a run from
  # before, and published by it.
  if [[ ! -f "${dir}revision" ]]; then
    echo "Skipping $dir: no revision, so published already."
    continue
  fi
  REV=$(cat "${dir}revision")
  tag="$section-$(date -u +%Y%m%d-%H%M%S)"
  base="https://github.com/$REPO/releases/download/$tag"

  # The catalogue's origin is the section, and software centres look for
  # its icons under icons/<origin>/.
  tree=$(mktemp -d)
  mkdir -p "$tree/xml" "$tree/icons/$section"
  cp "$dir"Components-x86_64-linux.xml.gz "$tree/xml/$section.xml.gz"
  for f in "$dir"icons-*.tar.gz; do
    [[ -e "$f" ]] || continue
    size=$(basename "$f" .tar.gz)
    mkdir -p "$tree/icons/$section/${size#icons-}"
    tar -xzf "$f" -C "$tree/icons/$section/${size#icons-}"
  done
  stage=$(mktemp -d)
  tarball="$stage/$section.tar.gz"
  # The same catalogue makes the same file.
  tar -C "$tree" -c --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 . \
    | gzip -9n > "$tarball"
  rm -rf "$tree"

  gh release create "$tag" "$tarball" --repo "$REPO" \
    --title "$section: nixpkgs ${REV:0:12}" \
    --notes "AppStream catalogue for $section, from nixpkgs $REV."

  [[ -f "$MANIFEST" ]] || echo '{}' > "$MANIFEST"
  jq --arg section "$section" --arg tag "$tag" --arg rev "$REV" \
    --arg url "$base/$section.tar.gz" --arg hash "$(nix-hash --flat --type sha256 --sri "$tarball")" \
    '.[$section] = {release: $tag, revision: $rev, url: $url, hash: $hash}' "$MANIFEST" > "$MANIFEST.tmp"
  rm -rf "$stage"
  mv "$MANIFEST.tmp" "$MANIFEST"
  git add "$MANIFEST"
  git rm -rq --ignore-unmatch "appstream/$section"
  echo "$section: nixpkgs ${REV:0:12}" >> "$PUBLISHED"

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
