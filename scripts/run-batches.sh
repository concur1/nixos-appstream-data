#!/usr/bin/env bash
# Runs scripts/build-batch.sh one batch after another until the pass
# completes, or until the next batch might not finish within TIME_BUDGET
# seconds of the start (judged by the longest batch so far). The workspace
# is saved between batches, so stopping early loses nothing.
#
# A new pass only starts when the channel has moved on since the last one,
# unless FORCE=1.
#
# A completed pass is followed by the unfree catalogue for its revision
# (scripts/build-unfree.sh), which may also take more than one run; it is
# finished before a new pass starts, as it reads that pass's package list.
set -euo pipefail

cd "$(dirname "$0")/.."

SECTION="${SECTION:-nixos-unstable}"
TIME_BUDGET="${TIME_BUDGET:-17400}"
FORCE="${FORCE:-0}"
WS=workspace
DEADLINE=$(( $(date +%s) + TIME_BUDGET ))

unfree() {
  [[ -f "$WS/unfree/pending" ]] || return 0
  DEADLINE=$DEADLINE scripts/build-unfree.sh
}

# Bumped when the unfree catalogue has to be made afresh: the generator's
# results for packages it already has stay otherwise. 2: descriptions for
# packages without one, and symlinked .desktop directories. 3: the
# generator's summary for desktop entries without a Comment.
UNFREE_FORMAT=3
if [[ -d "$WS/unfree" && "$(cat "$WS/unfree/format" 2> /dev/null)" != "$UNFREE_FORMAT" ]]; then
  rm -rf "$WS/unfree"
fi
# For the last pass, if there's no unfree catalogue for it (as after the
# above).
mkdir -p "$WS/unfree"
if [[ -f "$WS/last-pass" && ! -f "$WS/batch-state" && ! -f "$WS/unfree/rev" ]]; then
  cp "$WS/last-pass" "$WS/unfree/pending"
fi
echo "$UNFREE_FORMAT" > "$WS/unfree/format"
unfree
if [[ -f "$WS/unfree/pending" ]]; then
  echo "Unfree catalogue not complete yet; it goes on next run."
  exit 0
fi

# Bumped when the free catalogue has to be made afresh for the packages given
# a stand-in description (build-batch.sh), as for a change to the generator:
# they are forgotten again, in a new pass. 2: the generator's summary for
# desktop entries without a Comment.
FREE_FORMAT=2
if [[ "$(cat "$WS/format" 2> /dev/null)" != "$FREE_FORMAT" ]]; then
  rm -f "$WS/described-pkids"
  [[ -f "$WS/batch-state" ]] || rm -f "$WS/last-pass"
  echo "$FREE_FORMAT" > "$WS/format"
fi

if [[ ! -f "$WS/batch-state" && -f "$WS/last-pass" && "$FORCE" != 1 ]]; then
  REV=$(nix flake metadata "nixpkgs/$SECTION" --json | jq -r '.revision')
  if [[ "$REV" == "$(cat "$WS/last-pass")" ]]; then
    echo "nixpkgs $REV was the last pass; nothing to do (FORCE=1 to redo it)."
    exit 0
  fi
fi

# The generator log of this run's batches (uploaded by the workflow).
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
    # Unless the unfree catalogue is of this revision already.
    if [[ "$(cat "$WS/unfree/rev" 2> /dev/null)" != "$(cat "$WS/last-pass")" ]]; then
      cp "$WS/last-pass" "$WS/unfree/pending"
      unfree
    fi
    break
  fi
  left=$(( TIME_BUDGET - SECONDS ))
  if (( left < longest * 5 / 4 )); then
    echo "Stopping: ${left}s of the budget left, the longest batch took ${longest}s."
    break
  fi
done
