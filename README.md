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

# Licenses

All metadata and icons fall under the licenses specified in their respective metainfo files under the `metadata_license` tag.

The rest of the code in this repository is licensed under the [MIT license](./LICENSE).
