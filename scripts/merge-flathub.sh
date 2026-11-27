#!/usr/bin/env bash
# Fills in catalogue $1 (of section $2, made from packages.json $3) from
# Flathub's catalogue $4 (scripts/flathub_merge.py). Fails if Flathub can't
# be reached, rather than publish the catalogue without what it adds.
set -euo pipefail

cd "$(dirname "$0")/.."

# Licences are read per Flathub commit; kept with the workspace.
uv run scripts/flathub_merge.py --flathub "$4" merge "$1" --section "$2" \
  --packages "$3" --license-cache workspace/flathub-licenses.json
