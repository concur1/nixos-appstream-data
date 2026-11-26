#!/usr/bin/env bash
# Fills in catalogue $1 (of section $2, made from packages.json $3) from
# Flathub's (scripts/flathub_merge.py). If that fails, as when Flathub
# can't be reached, only sets the origin, so the pass is still published.
set -euo pipefail

cd "$(dirname "$0")/.."

catalogue=$1
section=$2
packages=$3

# Licences are read per Flathub commit; kept with the workspace.
if python3 scripts/flathub_merge.py merge "$catalogue" --section "$section" \
    --packages "$packages" --license-cache workspace/flathub-licenses.json; then
  exit 0
fi
echo "::warning::Flathub merge failed for $section; publishing it without Flathub's metadata."
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  echo "**Flathub merge failed for \`$section\`; published without Flathub's metadata.**" >> "$GITHUB_STEP_SUMMARY"
fi
python3 scripts/flathub_merge.py --flathub '' merge "$catalogue" --section "$section"
