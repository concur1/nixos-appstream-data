#!/usr/bin/env bash
# Writes appstream-generator's configuration to $1/asgen-config.json: suite
# "nixpkgs" with nixpkgs revision $2 as its one section, read from the nix
# store $3 (as in `nix store ls --store`).
set -euo pipefail

cat > "$1/asgen-config.json" << JSON
{
  "ProjectName": "Nix",
  "ArchiveRoot": "$3",
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
        "$2"
      ],
      "architectures": [
        "x86_64-linux"
      ]
    }
  }
}
JSON
