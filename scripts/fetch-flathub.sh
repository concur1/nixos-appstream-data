#!/usr/bin/env bash
# Downloads Flathub's AppStream catalogue to $1 unless it is there already:
# a pass, or an unfree catalogue, uses one copy throughout, so its batches
# and its merge see the same apps. Fails if Flathub can't be reached.
set -euo pipefail

dest=$1
[[ -s "$dest" ]] && exit 0
mkdir -p "$(dirname "$dest")"
curl -fsSL --retry 3 --retry-all-errors -o "$dest.tmp" \
  https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz
mv "$dest.tmp" "$dest"
