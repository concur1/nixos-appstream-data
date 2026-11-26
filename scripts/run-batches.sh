#!/usr/bin/env bash
# Runs scripts/build-batch.sh one batch after another until the pass
# completes, or until the next batch might not finish within TIME_BUDGET
# seconds of the start (judged by the longest batch so far). The workspace
# is saved between batches, so stopping early loses nothing.
#
# A new pass only starts when the channel has moved on since the last one,
# unless FORCE=1.
set -euo pipefail

cd "$(dirname "$0")/.."

SECTION="${SECTION:-nixos-unstable}"
TIME_BUDGET="${TIME_BUDGET:-17400}"
FORCE="${FORCE:-0}"
WS=workspace

if [[ ! -f "$WS/batch-state" && -f "$WS/last-pass" && "$FORCE" != 1 ]]; then
  REV=$(nix flake metadata "nixpkgs/$SECTION" --json | jq -r '.revision')
  if [[ "$REV" == "$(cat "$WS/last-pass")" ]]; then
    echo "nixpkgs $REV was the last pass; nothing to do (FORCE=1 to redo it)."
    exit 0
  fi
fi

# The generator log of this run's batches (uploaded by the workflow).
mkdir -p "$WS"
: > "$WS/appstream-generator.log"

longest=0
while :; do
  started=$SECONDS
  scripts/build-batch.sh
  took=$(( SECONDS - started ))
  if (( took > longest )); then
    longest=$took
  fi

  # build-batch.sh drops its state when it completes a pass.
  if [[ ! -f "$WS/batch-state" ]]; then
    echo "Pass complete."
    break
  fi
  left=$(( TIME_BUDGET - SECONDS ))
  if (( left < longest * 5 / 4 )); then
    echo "Stopping: ${left}s of the budget left, the longest batch took ${longest}s."
    break
  fi
done
