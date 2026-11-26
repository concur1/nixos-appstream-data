# Appstream data for NixOS

```shell
nix build .#appstream-data
```

or for unfree metadata

```shell
nix build .#appstream-data-unfree
```

or for both free and unfree combined

```shell
nix build .#appstream-data-all
```

The catalogues are built by the `Build AppStream data (batches)` workflow and
published as GitHub release assets; `appstream/sources.json` points at the
current release of each section, and the builds above fetch from there.

The unfree catalogue covers the unfree packages that match an app on
Flathub: Hydra doesn't build them, so the workflow does, one at a time.

## Metadata from Flathub

Where a component lacks a description, screenshots, categories, keywords,
developer, content rating, branding, links or an icon, they are filled in
from the matching app in [Flathub's catalogue](https://flathub.org)
(`scripts/flathub_merge.py`). Apps match by component ID, by homepage, or
by name, and only when the match is unique; each run's summary lists the
matches. Only metadata whose own licence AppStream accepts as a metadata
licence is used; it is read from the app's metainfo file on Flathub. Such
components record the app and its licence in
`<custom><value key="NixOS::flathub-id">` and
`<value key="NixOS::flathub-metadata-license">`.

The tests of the scripts:

```shell
nix shell .#python --command python3 -m unittest discover tests
```

# Licenses

All metadata and icons fall under the licenses specified in their respective metainfo files under the `metadata_license` tag. For metadata taken from Flathub, that is the licence in `NixOS::flathub-metadata-license`.

The rest of the code in this repository is licensed under the [MIT license](./LICENSE).
