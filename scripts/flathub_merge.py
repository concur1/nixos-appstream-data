"""Fills in what our generated AppStream components lack from Flathub's
catalogue, and picks the unfree packages worth building.

  flathub_merge.py merge CATALOGUE.xml.gz --section S [--packages packages.json]
  flathub_merge.py select --packages packages.json
  flathub_merge.py describe PACKAGES.json

`merge` rewrites the catalogue in place: it sets its origin to the section
and, for each component that matches a Flathub app, adds the fields it is
missing (see FILL). Only apps whose own metainfo has a metadata licence
that AppStream accepts are used; Flathub's catalogue leaves that tag out,
so it is read from the app's flatpak (ostree_metainfo.py). A report of the
matches goes to stdout and the job summary.

`select` prints the unfree attrs in a packages.json that match a Flathub
app: the unfree packages to build.

`describe` gives packages that match a Flathub app and lack a long
description their summary as one, so the generator keeps their components
(see describe()), and prints their package IDs.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import ostree_metainfo

FLATHUB_CATALOGUE = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"

# Each copied from Flathub when the component has none (with developer,
# missing <url> types and remote icons; see enrich). Releases, bundles,
# launchables, cached icons and <custom> never are: they describe the
# flatpak, not the app.
FILL = ["description", "screenshots", "categories", "keywords", "content_rating", "branding"]

# AppStream's own list (as_license_is_metadata_license_id in as-spdx.c).
METADATA_LICENSES = {
    "FSFAP", "MIT", "0BSD", "CC0-1.0", "CC-BY-3.0", "CC-BY-4.0", "CC-BY-SA-3.0",
    "CC-BY-SA-4.0", "GFDL-1.1", "GFDL-1.2", "GFDL-1.3", "BSL-1.0", "FTL", "FSFUL",
}  # fmt: skip
# Names AppStream reads as SPDX IDs (as_spdx_license_tokenize_drop).
LEGACY_LICENSES = {"CC0": "CC0-1.0", "CC-BY": "CC-BY-3.0", "CC-BY-SA": "CC-BY-SA-3.0", "GFDL": "GFDL-1.3"}

# A bare forge host says nothing about which app it is.
FORGES = {"github.com", "gitlab.com", "codeberg.org", "sourceforge.net", "bitbucket.org", "sr.ht"}

APP_TYPES = {"desktop-application", "console-application"}
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


# --- Keys ------------------------------------------------------------------


def id_key(component_id: str | None) -> str | None:
    if not component_id:
        return None
    k = component_id.strip().casefold()
    return k.removesuffix(".desktop") or None


def url_key(url: str | None) -> str | None:
    """A homepage without scheme, www., fragment, index page or trailing
    slash."""
    if not url:
        return None
    u = url.strip().casefold()
    u = re.sub(r"^[a-z+]+://", "", u).split("#", 1)[0]
    u = re.sub(r"/index\.(html?|php)$", "", u.removeprefix("www."))
    u = u.rstrip("/").removesuffix(".git")
    host, _, path = u.partition("/")
    if not host or (not path and host in FORGES):
        return None
    return u


def component_type(c: ET.Element) -> str:
    t = c.get("type")
    return "desktop-application" if t in (None, "desktop") else t


def license_allowed(expr: str | None) -> bool:
    """Whether an SPDX expression is a metadata licence, as appstreamcli
    validate decides it (as_license_is_metadata_license): one alternative
    of an OR will do, but every part of an AND must be one. Mixed or
    bracketed expressions need every part, where AppStream gives up."""
    if not expr or "(" in expr or ")" in expr:
        return False
    ops, ids = set(), []
    for tok in expr.split():
        if tok in ("AND", "and", "OR", "or"):
            ops.add(tok.upper())
        else:
            # An exception after WITH is no metadata licence, nor is WITH.
            tok = re.sub(r"(-only|-or-later|\+)$", "", tok)
            ids.append(LEGACY_LICENSES.get(tok, tok) in METADATA_LICENSES)
    if not ids:
        return False
    return any(ids) if ops == {"OR"} else all(ids)


# --- Catalogues --------------------------------------------------------------


def read_catalogue(source: str) -> ET.Element:
    if re.match(r"https?://", source):
        req = urllib.request.Request(source, headers={"User-Agent": "nixos-appstream-data"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
    else:
        data = Path(source).read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return ET.fromstring(data)


def write_catalogue(root: ET.Element, path: Path) -> None:
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="utf-8", xml_declaration=True)
    # No name or time in the header, so the same catalogue gives the same file.
    path.write_bytes(gzip.compress(buf.getvalue(), mtime=0))


def words(*names: str | None) -> list[str]:
    """The words of an app's names, for telling apart apps that share a
    homepage."""
    out = set()
    for n in names:
        out.update(w for w in re.split(r"[^0-9a-z]+", (n or "").casefold()) if len(w) >= 3)
    return sorted(out)


def last_segment(component_id: str | None) -> str | None:
    k = id_key(component_id)
    return k.rsplit(".", 1)[-1] if k else None


def untranslated(c: ET.Element, tag: str) -> str | None:
    for e in c.findall(tag):
        if e.get(XML_LANG) is None:
            return (e.text or "").strip() or None
    return None


def flathub_frame(root: ET.Element) -> pl.DataFrame:
    rows = []
    for frow, c in enumerate(root.findall("component")):
        if component_type(c) not in APP_TYPES:
            continue
        cid = c.findtext("id") or ""
        urls = {u.get("type"): u.text for u in c.findall("url")}
        name = untranslated(c, "name")
        rows.append(
            {
                "frow": frow,
                "flathub_id": cid,
                "type": component_type(c),
                "id_key": id_key(cid),
                "last_key": last_segment(cid),
                "name_key": (name or "").casefold() or None,
                "home_keys": sorted(
                    {k for k in (url_key(urls.get("homepage")), url_key(urls.get("vcs-browser"))) if k}
                ),
                "vcs_key": url_key(urls.get("vcs-browser")),
                "words": words(last_segment(cid), name),
                "ref": c.findtext("bundle[@type='flatpak']"),
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "frow": pl.Int64, "flathub_id": pl.String, "type": pl.String, "id_key": pl.String,
            "last_key": pl.String, "name_key": pl.String, "home_keys": pl.List(pl.String),
            "vcs_key": pl.String, "words": pl.List(pl.String), "ref": pl.String,
        },
    )  # fmt: skip


OURS_SCHEMA = {
    "row": pl.Int64, "key": pl.String, "type": pl.String, "id_key": pl.String,
    "home_key": pl.String, "vcs_key": pl.String, "pname": pl.String, "words": pl.List(pl.String),
}  # fmt: skip


def nix_pname(attr: str, pkg: dict) -> str:
    return (pkg.get("pname") or attr.rsplit(".", 1)[-1]).casefold()


def nix_homepage(pkg: dict) -> str | None:
    h = pkg.get("meta", {}).get("homepage")
    if isinstance(h, list):
        h = h[0] if h else None
    return h if isinstance(h, str) else None


def ours_frame(root: ET.Element, packages: dict) -> pl.DataFrame:
    """Our components; nix meta fills in the homepage and pname, as
    components made from a .desktop file have neither."""
    rows = []
    for row, c in enumerate(root.findall("component")):
        attr = c.findtext("pkgname") or ""
        pkg = packages.get(attr, {})
        urls = {u.get("type"): u.text for u in c.findall("url")}
        cid = c.findtext("id")
        rows.append(
            {
                "row": row,
                # Components with one ID are one app, whichever attrs they
                # come from.
                "key": id_key(cid) or f"#{row}",
                "type": component_type(c),
                "id_key": id_key(cid),
                "home_key": url_key(urls.get("homepage")) or url_key(nix_homepage(pkg)),
                "vcs_key": url_key(urls.get("vcs-browser")),
                "pname": nix_pname(attr, pkg) or None,
                "words": words(last_segment(cid), untranslated(c, "name"), nix_pname(attr, pkg)),
            }
        )
    return pl.DataFrame(rows, schema=OURS_SCHEMA)


def packages_frame(packages: dict) -> pl.DataFrame:
    """Packages not built yet: no component ID, and the type is a guess."""
    rows = [
        {
            "row": i,
            "key": attr,
            "type": "desktop-application",
            "id_key": None,
            "home_key": url_key(nix_homepage(pkg)),
            "vcs_key": None,
            "pname": nix_pname(attr, pkg),
            "words": words(nix_pname(attr, pkg), attr.rsplit(".", 1)[-1], pkg.get("meta", {}).get("mainProgram")),
        }
        for i, (attr, pkg) in enumerate(sorted(packages.items()))
    ]
    return pl.DataFrame(rows, schema=OURS_SCHEMA)


# --- Matching ----------------------------------------------------------------

RULES = ["id", "homepage", "name"]


def candidates(ours: pl.DataFrame, flathub: pl.DataFrame) -> pl.DataFrame:
    """(key, frow, rule) for each way a key matches a Flathub app."""
    by_id = ours.join(flathub, on=["id_key", "type"]).select("key", "frow", pl.lit("id").alias("rule"))
    # A homepage can be a developer's site for several apps, so the two
    # must also share a word of their names and not differ in repository.
    by_home = (
        ours.join(
            flathub.explode("home_keys", empty_as_null=False).rename({"home_keys": "home_key"}), on=["home_key", "type"]
        )
        .filter(
            (pl.col("words").list.set_intersection(pl.col("words_right")).list.len() > 0)
            & (
                pl.col("vcs_key").is_null()
                | pl.col("vcs_key_right").is_null()
                | (pl.col("vcs_key") == pl.col("vcs_key_right"))
            )
        )
        .select("key", "frow", pl.lit("homepage").alias("rule"))
    )
    # Both the ID's last part and the name are the pname.
    named = flathub.filter(pl.col("last_key") == pl.col("name_key"))
    by_name = ours.join(named, left_on=["pname", "type"], right_on=["last_key", "type"]).select(
        "key", "frow", pl.lit("name").alias("rule")
    )
    return pl.concat([by_id, by_home, by_name]).unique()


def match(ours: pl.DataFrame, flathub: pl.DataFrame, unique_ours: bool = True) -> pl.DataFrame:
    """One row per key with candidates: (key, rule, status, frows), status
    "matched" or "ambiguous". Rules are tried strongest first, and the
    first that finds anything decides. A match must be the key's only
    candidate and, with unique_ours, the only key for that Flathub app,
    counting apps already taken by a stronger rule."""
    cands = candidates(ours, flathub)
    decided = pl.DataFrame(
        schema={"key": pl.String, "rule": pl.String, "status": pl.String, "frows": pl.List(pl.Int64)}
    )
    taken = pl.DataFrame(schema={"frow": pl.Int64})
    for rule in RULES:
        c = cands.filter(pl.col("rule") == rule).join(decided.select("key"), on="key", how="anti")
        if c.is_empty():
            continue
        per_key = c.group_by("key").agg(pl.col("frow").unique().sort().alias("frows"))
        per_frow = c.group_by("frow").agg(pl.col("key").n_unique().alias("keys"))
        per_frow = per_frow.with_columns(pl.col("frow").is_in(taken["frow"].implode()).alias("taken"))
        bad = (
            per_frow.filter((pl.col("keys") > 1) | pl.col("taken"))["frow"] if unique_ours else per_frow["frow"].clear()
        )
        status = per_key.with_columns(
            pl.lit(rule).alias("rule"),
            pl.when((pl.col("frows").list.len() == 1) & ~pl.col("frows").list.first().is_in(bad.implode()))
            .then(pl.lit("matched"))
            .otherwise(pl.lit("ambiguous"))
            .alias("status"),
        ).select("key", "rule", "status", "frows")
        decided = pl.concat([decided, status])
        taken = pl.concat(
            [taken, status.filter(pl.col("status") == "matched").select(pl.col("frows").list.first().alias("frow"))]
        )
    return decided


# --- Licences ----------------------------------------------------------------


def metadata_licenses(refs: list[str], cache_path: Path | None) -> dict[str, str | None]:
    """Ref -> the metadata licence in its current commit's metainfo, or None.
    Cached by commit, so a pass only reads apps rebuilt since the last one."""
    cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}
    commits = ostree_metainfo.ref_commits()

    def lookup(ref: str) -> tuple[str, str | None]:
        commit = commits.get(ref)
        if commit is None:
            return ref, None
        if commit not in cache:
            app_id = ref.split("/")[1]
            try:
                data = ostree_metainfo.metainfo(ostree_metainfo.FLATHUB, commit, app_id)
                lic = ET.fromstring(data).findtext("metadata_license") if data else None
            except OSError as e:  # Not cached: tried again next time.
                print(f"warning: no metainfo for {ref}: {e}", file=sys.stderr)
                return ref, None
            # Unreadable, which shouldn't stop the merge; the report lists it.
            except Exception as e:  # noqa: BLE001
                print(f"warning: unreadable metainfo for {ref}: {e!r}", file=sys.stderr)
                lic = None
            cache[commit] = (lic or "").strip()
        return ref, cache[commit] or None

    with ThreadPoolExecutor(16) as pool:
        result = dict(pool.map(lookup, refs))
    if cache_path:
        live = set(commits.values())
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({k: v for k, v in sorted(cache.items()) if k in live}, indent=0))
    return result


# --- Enrichment --------------------------------------------------------------


def _append(parent: ET.Element, el: ET.Element) -> None:
    """Adds a copy of el as the last child, in the catalogue's indentation."""
    el = copy.deepcopy(el)
    el.tail = parent[-1].tail
    parent[-1].tail = parent.text
    parent.append(el)


def _remove(parent: ET.Element, el: ET.Element) -> None:
    i = list(parent).index(el)
    if i == len(parent) - 1 and i > 0:
        parent[i - 1].tail = el.tail
    parent.remove(el)


def is_summary_description(c: ET.Element, summary: str | None) -> bool:
    """Whether the component's description is only nix's one-line summary.
    build-unfree.sh gives the generator that as the long description of
    packages without one, as it drops a component made from a .desktop
    file alone if it has no description."""
    descs = c.findall("description")
    return bool(summary and descs) and all(
        len(d) == 1 and d[0].tag == "p" and (d[0].text or "").strip() == summary.strip() for d in descs
    )


def enrich(ours: ET.Element, theirs: ET.Element, summary: str | None = None) -> list[str]:
    """Copies what `ours` lacks from `theirs`; returns what it added. A
    description that is only nix's `summary` counts as missing."""
    added = []
    if theirs.find("description") is not None and is_summary_description(ours, summary):
        for d in ours.findall("description"):
            _remove(ours, d)
    for tag in FILL:
        if ours.find(tag) is None and theirs.find(tag) is not None:
            for el in theirs.findall(tag):
                _append(ours, el)
            added.append(tag)
    if ours.find("developer") is None and ours.find("developer_name") is None:
        devs = theirs.findall("developer") + theirs.findall("developer_name")
        for el in devs:
            _append(ours, el)
        if devs:
            added.append("developer")
    have = {u.get("type") for u in ours.findall("url")}
    for u in theirs.findall("url"):
        if u.get("type") not in have:
            have.add(u.get("type"))
            _append(ours, u)
            added.append(f"url:{u.get('type')}")
    if ours.find("icon") is None:
        remote = [i for i in theirs.findall("icon") if i.get("type") == "remote"]
        for el in remote:
            _append(ours, el)
        if remote:
            added.append("icon")
    return added


def mark_source(c: ET.Element, flathub_id: str, license: str) -> None:
    """Which app the added fields came from, and under which licence."""
    values = []
    for key, value in (("NixOS::flathub-id", flathub_id), ("NixOS::flathub-metadata-license", license)):
        v = ET.Element("value", key=key)
        v.text = value
        values.append(v)
    custom = c.find("custom")
    if custom is not None and len(custom):
        for v in values:
            _append(custom, v)
        return
    if custom is None:
        _append(c, ET.Element("custom"))
        custom = c[-1]
    custom.text = (c.text or "\n") + "  "
    for v in values:
        v.tail = custom.text
        custom.append(v)
    values[-1].tail = c.text


# --- Report ------------------------------------------------------------------


def report(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text)


def _details(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return ["", f"<details><summary>{title} ({len(items)})</summary>", "", *items, "", "</details>"]


# --- Commands ----------------------------------------------------------------


def load_packages(path: str | None) -> dict:
    return json.loads(Path(path).read_text()).get("packages", {}) if path else {}


def merge(root: ET.Element, fh_root: ET.Element, packages: dict, section: str, licenses_for) -> list[str]:
    """Enriches the catalogue `root` from `fh_root` in place and returns the
    report. licenses_for maps a list of flatpak refs to their metadata
    licences."""
    root.set("origin", section)
    comps = root.findall("component")
    fh_comps = fh_root.findall("component")
    flathub = flathub_frame(fh_root)
    ours = ours_frame(root, packages)

    decided = match(ours, flathub)
    matched = (
        decided.filter(pl.col("status") == "matched")
        .with_columns(pl.col("frows").list.first().alias("frow"))
        .join(flathub.select("frow", "flathub_id", "ref"), on="frow")
    )
    licenses = licenses_for(sorted(r for r in matched["ref"].unique().to_list() if r))
    matched = matched.with_columns(
        pl.col("ref").replace_strict(licenses, default=None, return_dtype=pl.String).alias("license"),
    ).with_columns(pl.col("license").map_elements(license_allowed, return_dtype=pl.Boolean).alias("allowed"))

    filled: dict[str, int] = {}
    enriched = 0
    by_key = {m["key"]: m for m in matched.filter(pl.col("allowed")).iter_rows(named=True)}
    for r in ours.iter_rows(named=True):
        m = by_key.get(r["key"])
        if m is None:
            continue
        c = comps[r["row"]]
        summary = packages.get(c.findtext("pkgname") or "", {}).get("meta", {}).get("description")
        added = enrich(c, fh_comps[m["frow"]], summary if isinstance(summary, str) else None)
        if added:
            mark_source(comps[r["row"]], m["flathub_id"], m["license"])
            enriched += 1
        for a in added:
            filled[a] = filled.get(a, 0) + 1

    rejected = [
        f"- `{m['key']}` → `{m['flathub_id']}`: {m['license'] or 'no metadata licence found'}"
        for m in matched.filter(~pl.col("allowed")).sort("key").iter_rows(named=True)
    ]
    names = dict(zip(flathub["frow"].to_list(), flathub["flathub_id"].to_list()))
    ambiguous = [
        f"- `{r['key']}` ({r['rule']}): " + ", ".join(f"`{names[f]}`" for f in r["frows"])
        for r in decided.filter(pl.col("status") == "ambiguous").sort("key").iter_rows(named=True)
    ]
    unmatched = sorted(set(ours["key"]) - set(decided["key"]))
    by_rule = matched.group_by("rule").len().sort("rule")
    lines = [
        f"### Flathub merge: `{section}`",
        "",
        f"{ours['key'].n_unique()} apps ({len(comps)} components); "
        f"{len(matched)} matched a Flathub app ("
        + ", ".join(f"{r['len']} by {r['rule']}" for r in by_rule.iter_rows(named=True))
        + f"), {len(ambiguous)} ambiguously, {len(unmatched)} not at all.",
        (
            f"{enriched} components got fields from Flathub; "
            f"{len(rejected)} matches were skipped for their metadata licence."
        ),
        "",
        "| Field | Components |",
        "| --- | --- |",
        *(f"| {k} | {v} |" for k, v in sorted(filled.items(), key=lambda kv: (-kv[1], kv[0]))),
    ]
    lines += _details("Skipped for the metadata licence", rejected)
    lines += _details("Ambiguous", ambiguous)
    lines += _details("Unmatched", [f"- `{k}`" for k in unmatched])
    return lines


def cmd_merge(args: argparse.Namespace) -> None:
    path = Path(args.catalogue)
    root = read_catalogue(str(path))
    if args.flathub:
        cache = Path(args.license_cache) if args.license_cache else None
        lines = merge(
            root,
            read_catalogue(args.flathub),
            load_packages(args.packages),
            args.section,
            lambda refs: metadata_licenses(refs, cache),
        )
        report(lines)
    else:
        root.set("origin", args.section)
    write_catalogue(root, path)


def flathub_attrs(packages: dict, fh_root: ET.Element) -> list[str]:
    """The attrs among `packages` (not built yet) that match a Flathub app.
    Several attrs may be one app (versions, variants): merge decides on
    their component IDs once they are built."""
    decided = match(packages_frame(packages), flathub_frame(fh_root), unique_ours=False)
    return decided.filter(pl.col("status") == "matched").sort("key")["key"].to_list()


def select(packages: dict, fh_root: ET.Element) -> list[str]:
    """The unfree attrs that can be built and match a Flathub app."""
    unfree = {
        attr: pkg
        for attr, pkg in packages.items()
        if pkg.get("meta", {}).get("unfree") and isinstance(pkg.get("outputs"), dict)
    }
    return flathub_attrs(unfree, fh_root)


def describe(packages: dict, fh_root: ET.Element) -> list[str]:
    """Gives packages that match a Flathub app and have no long description
    their summary as one, in place, and returns their package IDs.

    The generator drops a component made from a .desktop file alone (no
    metainfo, as Firefox's) if its package has no long description; merge
    then replaces the summary with the app's own description."""
    bare = {
        attr: pkg
        for attr, pkg in packages.items()
        if isinstance(pkg.get("outputs"), dict)
        and isinstance(pkg.get("meta", {}).get("description"), str)
        and not pkg["meta"].get("longDescription")
    }
    pkids = []
    for attr in flathub_attrs(bare, fh_root):
        pkg = packages[attr]
        pkg["meta"]["longDescription"] = pkg["meta"]["description"]
        # As the generator's nix backend names packages.
        pkids.append(f"{attr}/{pkg.get('version', '')}/{pkg.get('system', '')}")
    return pkids


def cmd_select(args: argparse.Namespace) -> None:
    for attr in select(load_packages(args.packages), read_catalogue(args.flathub)):
        print(attr)


def cmd_describe(args: argparse.Namespace) -> None:
    path = Path(args.packages)
    data = json.loads(path.read_text())
    # A packages.json, or nix-env's output that one is made of.
    packages = data["packages"] if "version" in data else data
    pkids = describe(packages, read_catalogue(args.flathub))
    tmp = path.with_name(path.name + ".describe")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)
    for pkid in pkids:
        print(pkid)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--flathub", default=FLATHUB_CATALOGUE, help="Flathub's catalogue: a path or URL; empty to only set the origin"
    )
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("merge")
    m.add_argument("catalogue")
    m.add_argument("--section", required=True)
    m.add_argument("--packages", help="the packages.json the catalogue was made from")
    m.add_argument("--license-cache", help="JSON file of metadata licences by commit, kept between runs")
    s = sub.add_parser("select")
    s.add_argument("--packages", required=True)
    d = sub.add_parser("describe")
    d.add_argument("packages", help="a packages.json or nix-env --json output, changed in place")
    args = p.parse_args()
    {"merge": cmd_merge, "select": cmd_select, "describe": cmd_describe}[args.command](args)


if __name__ == "__main__":
    main()
