"""Fixture tests for scripts/flathub_merge.py and scripts/ostree_metainfo.py.

uv run pytest
"""

import email.message
import io
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flathub_merge as fm
import ostree_metainfo as om


def catalogue(*components: str) -> ET.Element:
    return ET.fromstring('<components version="1.0" origin="x">\n' + "\n".join(components) + "\n</components>")


def ours(cid: str, name: str, pkgname: str, extra: str = "") -> str:
    return (
        f'<component type="desktop-application">\n  <id>{cid}</id>\n  <name>{name}</name>\n'
        f"  <summary>s</summary>\n  <pkgname>{pkgname}</pkgname>{extra}\n</component>"
    )


def flathub(cid: str, name: str, homepage: str = "", vcs: str = "", extra: str = "") -> str:
    urls = (f'<url type="homepage">{homepage}</url>' if homepage else "") + (
        f'<url type="vcs-browser">{vcs}</url>' if vcs else ""
    )
    return (
        f'<component type="desktop-application"><id>{cid}</id><name>{name}</name>'
        f'<summary>s</summary>{urls}<bundle type="flatpak">app/{cid}/x86_64/stable</bundle>{extra}</component>'
    )


def matches(our_root: ET.Element, fh_root: ET.Element, packages: dict | None = None) -> dict:
    """key -> (status, rule, [flathub ids])"""
    fh = fm.flathub_frame(fh_root)
    names = dict(zip(fh["frow"].to_list(), fh["flathub_id"].to_list()))
    decided = fm.match(fm.ours_frame(our_root, packages or {}), fh)
    return {r["key"]: (r["status"], r["rule"], [names[f] for f in r["frows"]]) for r in decided.iter_rows(named=True)}


class Matching(unittest.TestCase):
    def test_id_ignores_case_and_desktop_suffix(self) -> None:
        m = matches(
            catalogue(ours("org.Example.App.desktop", "App", "app")),
            catalogue(flathub("org.example.app", "Something else")),
        )
        self.assertEqual(m["org.example.app"], ("matched", "id", ["org.example.app"]))

    def test_homepage_normalised(self) -> None:
        m = matches(
            catalogue(
                ours("app.desktop", "Tool", "tool", '\n  <url type="homepage">http://www.tool.org/index.html</url>')
            ),
            catalogue(flathub("org.tool.Tool", "Tool", homepage="https://tool.org/")),
        )
        self.assertEqual(m["app"], ("matched", "homepage", ["org.tool.Tool"]))

    def test_homepage_from_nix_meta_and_flathub_vcs(self) -> None:
        m = matches(
            catalogue(ours("tool.desktop", "Tool", "tool")),
            catalogue(
                flathub("io.github.me.Tool", "Tool", homepage="https://tool.example", vcs="https://github.com/me/tool")
            ),
            {"tool": {"pname": "tool", "meta": {"homepage": "https://github.com/me/tool"}}},
        )
        self.assertEqual(m["tool"], ("matched", "homepage", ["io.github.me.Tool"]))

    def test_bare_forge_homepage_is_ignored(self) -> None:
        m = matches(
            catalogue(ours("tool.desktop", "Tool", "tool", '\n  <url type="homepage">https://github.com</url>')),
            catalogue(flathub("com.github.Tool", "Tool Thing", homepage="https://github.com/")),
        )
        self.assertNotIn("tool", m)

    def test_homepage_shared_by_two_flathub_apps_is_ambiguous(self) -> None:
        m = matches(
            catalogue(
                ours("viewer.desktop", "Suite Viewer", "suite", '\n  <url type="homepage">https://suite.org</url>')
            ),
            catalogue(
                flathub("org.suite.Viewer", "Suite Viewer", homepage="https://suite.org"),
                flathub("org.suite.Editor", "Suite Editor", homepage="https://suite.org"),
            ),
        )
        self.assertEqual(m["viewer"][0], "ambiguous")

    def test_homepage_shared_by_two_of_our_apps_is_ambiguous(self) -> None:
        home = '\n  <url type="homepage">https://suite.org</url>'
        m = matches(
            catalogue(
                ours("suite-a.desktop", "Suite A", "suite-a", home), ours("suite-b.desktop", "Suite B", "suite-b", home)
            ),
            catalogue(flathub("org.suite.Suite", "Suite", homepage="https://suite.org")),
        )
        self.assertEqual(m["suite-a"][0], "ambiguous")
        self.assertEqual(m["suite-b"][0], "ambiguous")

    def test_attrs_sharing_an_id_are_one_app(self) -> None:
        home = '\n  <url type="homepage">https://suite.org</url>'
        root = catalogue(ours("org.suite.App", "App", "app", home), ours("org.suite.App", "App", "app-bin", home))
        fh = catalogue(flathub("org.suite.Suite", "App Suite", homepage="https://suite.org"))
        self.assertEqual(matches(root, fh)["org.suite.app"], ("matched", "homepage", ["org.suite.Suite"]))

    def test_homepage_needs_a_shared_name_word(self) -> None:
        m = matches(
            catalogue(ours("com.dev.Stamp", "Stamp", "stamp", '\n  <url type="homepage">https://dev.org</url>')),
            catalogue(flathub("com.dev.BloodPressure", "Blood Pressure", homepage="https://dev.org")),
        )
        self.assertNotIn("com.dev.stamp", m)

    def test_homepage_with_other_repository_is_no_match(self) -> None:
        extra = '\n  <url type="homepage">https://dux.dev</url>\n  <url type="vcs-browser">https://github.com/dux/files</url>'
        m = matches(
            catalogue(ours("dev.dux.Files", "Dux Files", "dux-files", extra)),
            catalogue(
                flathub(
                    "dev.dux.Calendar",
                    "Dux Calendar",
                    homepage="https://dux.dev",
                    vcs="https://github.com/dux/calendar",
                )
            ),
        )
        self.assertNotIn("dev.dux.files", m)

    def test_name_rule_needs_id_segment_and_name(self) -> None:
        fh = catalogue(flathub("com.vendor.Discord", "Discord"), flathub("com.other.Slack", "Slack Desktop"))
        m = matches(
            catalogue(ours("discord.desktop", "Discord", "discord"), ours("slack.desktop", "Slack", "slack")), fh
        )
        self.assertEqual(m["discord"], ("matched", "name", ["com.vendor.Discord"]))
        self.assertNotIn("slack", m)

    def test_stronger_rule_decides(self) -> None:
        # The ID match wins, and a weaker rule may not take the same app.
        m = matches(
            catalogue(
                ours("org.tool.Tool", "Tool", "tool"),
                ours(
                    "tool-extra.desktop", "Tool Extra", "tool-extra", '\n  <url type="homepage">https://tool.org</url>'
                ),
            ),
            catalogue(flathub("org.tool.Tool", "Tool", homepage="https://tool.org")),
        )
        self.assertEqual(m["org.tool.tool"], ("matched", "id", ["org.tool.Tool"]))
        self.assertEqual(m["tool-extra"][0], "ambiguous")

    def test_only_apps_match(self) -> None:
        fh = catalogue('<component type="runtime"><id>org.tool.Tool</id><name>Tool</name></component>')
        self.assertEqual(matches(catalogue(ours("org.tool.Tool", "Tool", "tool")), fh), {})


class Select(unittest.TestCase):
    def test_unfree_attrs_matching_flathub(self) -> None:
        packages = {
            "discord": {"pname": "discord", "outputs": {"out": "/nix/store/a"}, "meta": {"unfree": True}},
            "discord-ptb": {
                "pname": "discord-ptb",
                "outputs": {"out": "/nix/store/b"},
                "meta": {"unfree": True, "homepage": "https://discord.com/", "mainProgram": "discord"},
            },
            "free-thing": {"pname": "discord", "outputs": {"out": "/nix/store/c"}, "meta": {"unfree": False}},
            "broken": {"pname": "discord", "meta": {"unfree": True}},
        }
        fh = catalogue(flathub("com.discordapp.Discord", "Discord", homepage="https://discord.com"))
        # Several attrs for one app are fine here.
        self.assertEqual(fm.select(packages, fh), ["discord", "discord-ptb"])


class Describe(unittest.TestCase):
    def test_summary_as_long_description_for_flathub_apps(self) -> None:
        def pkg(**meta):
            return {"pname": "firefox", "version": "1", "system": "x86_64-linux",
                    "outputs": {"out": "/nix/store/a"}, "meta": {"description": "Web browser", **meta}}  # fmt: skip

        packages = {
            "firefox": pkg(),
            "firefox-esr": pkg(homepage="https://www.mozilla.org/firefox/"),
            "described": pkg(longDescription="Its own"),
            "elsewhere": {**pkg(), "pname": "elsewhere"},
            "unbuilt": {k: v for k, v in pkg().items() if k != "outputs"},
        }
        fh = catalogue(flathub("org.mozilla.firefox", "Firefox", homepage="https://www.mozilla.org/firefox/"))
        pkids = fm.describe(packages, fh)
        self.assertEqual(pkids, ["firefox/1/x86_64-linux", "firefox-esr/1/x86_64-linux"])
        self.assertEqual(packages["firefox"]["meta"]["longDescription"], "Web browser")
        self.assertEqual(packages["described"]["meta"]["longDescription"], "Its own")
        self.assertNotIn("longDescription", packages["elsewhere"]["meta"])
        self.assertNotIn("longDescription", packages["unbuilt"]["meta"])


class Licences(unittest.TestCase):
    def test_license_allowed(self) -> None:
        for expr in ["CC0-1.0", "CC0", "MIT", "FSFAP", "CC-BY-SA-4.0", "GFDL-1.3-or-later", "GFDL-1.3+",
                     "GPL-2.0+ or GFDL-1.3-only", "CC0-1.0 AND CC-BY-4.0"]:  # fmt: skip
            self.assertTrue(fm.license_allowed(expr), expr)
        for expr in [None, "", "GPL-3.0-or-later", "LicenseRef-proprietary", "CC0-1.0 AND GPL-3.0",
                     "MIT WITH Some-exception", "(CC0-1.0 OR MIT)", "CC-BY-NC-4.0"]:  # fmt: skip
            self.assertFalse(fm.license_allowed(expr), expr)

    def test_exception_only_rules_out_its_alternative(self) -> None:
        self.assertTrue(fm.license_allowed("GPL-2.0 WITH Classpath-exception-2.0 OR CC0-1.0"))
        self.assertFalse(fm.license_allowed("CC0-1.0 WITH Some-exception AND MIT"))

    def test_malformed_expressions(self) -> None:
        for expr in ["MIT OR", "OR CC0-1.0", "MIT CC0-1.0", "MIT AND AND CC0-1.0", "AND", "MIT OR WITH"]:
            self.assertFalse(fm.license_allowed(expr), expr)


class LicenseCache(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "licenses.json"
            fm.write_atomically(path, '{"abc": "CC0-1.0", "def": ""}')
            self.assertEqual(fm.read_license_cache(path), {"abc": "CC0-1.0", "def": ""})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["licenses.json"])

    def test_missing_or_broken_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "licenses.json"
            self.assertEqual(fm.read_license_cache(path), {})
            for broken in ['{"abc": "CC0-', "[1, 2]", '{"abc": 1}', ""]:
                path.write_text(broken)
                self.assertEqual(fm.read_license_cache(path), {}, broken)


class Fetching(unittest.TestCase):
    def responses(self, *outcomes: Exception | bytes) -> mock.MagicMock:
        """urlopen giving each outcome in turn: raised, or as the body."""

        def urlopen(req: object, timeout: float) -> io.BytesIO:
            outcome = next(it)
            if isinstance(outcome, Exception):
                raise outcome
            return io.BytesIO(outcome)

        it = iter(outcomes)
        return mock.MagicMock(side_effect=urlopen)

    def http_error(self, code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://x", code, "error", email.message.Message(), None)

    def test_brief_errors_are_tried_again(self) -> None:
        urlopen = self.responses(OSError("reset"), self.http_error(503), b"body")
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            self.assertEqual(om.get("https://x"), b"body")
        self.assertEqual(urlopen.call_count, 3)

    def test_lasting_errors_raise(self) -> None:
        urlopen = self.responses(OSError("down"), OSError("down"), OSError("down"))
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            self.assertRaises(OSError, om.get, "https://x")

    def test_client_errors_are_not_tried_again(self) -> None:
        urlopen = self.responses(self.http_error(404), b"body")
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            self.assertRaises(urllib.error.HTTPError, om.get, "https://x")
        self.assertEqual(urlopen.call_count, 1)


class Enrich(unittest.TestCase):
    THEIRS = flathub(
        "org.tool.Tool",
        "Tool",
        homepage="https://tool.org",
        vcs="https://github.com/me/tool",
        extra="<description><p>From Flathub</p></description>"
        '<description xml:lang="de"><p>Von Flathub</p></description>'
        "<screenshots><screenshot><image>https://x/1.png</image></screenshot></screenshots>"
        "<categories><category>Utility</category></categories>"
        "<developer><name>Me</name></developer><developer_name>Me</developer_name>"
        '<content_rating type="oars-1.1"/><branding><color type="primary">#fff</color></branding>'
        '<launchable type="desktop-id">org.tool.Tool.desktop</launchable>'
        '<icon type="cached" width="64" height="64">org.tool.Tool.png</icon>'
        '<icon type="remote" width="128" height="128">https://x/icon.png</icon>'
        '<releases><release version="9"/></releases>'
        '<custom><value key="flathub::verification::verified">true</value></custom>',
    )

    def run_merge(self, component: str, license: str | None = "CC0-1.0") -> tuple[ET.Element, list[str]]:
        root = catalogue(component)
        lines = fm.merge(root, catalogue(self.THEIRS), {}, "nixos-unstable", lambda refs: {r: license for r in refs})
        return root, lines

    def test_fills_only_missing_fields(self) -> None:
        root, _ = self.run_merge(
            ours(
                "org.tool.Tool",
                "Tool",
                "tool",
                '\n  <description><p>Ours</p></description>\n  <url type="homepage">https://tool.example</url>'
                '\n  <icon type="stock">tool</icon>\n  <releases><release version="1"/></releases>',
            )
        )
        self.assertEqual(root.get("origin"), "nixos-unstable")
        c = root[0]
        self.assertEqual([d.findtext("p") for d in c.findall("description")], ["Ours"])
        self.assertEqual([u.text for u in c.findall("url[@type='homepage']")], ["https://tool.example"])
        self.assertEqual(c.findtext("url[@type='vcs-browser']"), "https://github.com/me/tool")
        self.assertEqual([i.get("type") for i in c.findall("icon")], ["stock"])
        for tag in ("screenshots", "categories", "developer", "developer_name", "content_rating", "branding"):
            self.assertIsNotNone(c.find(tag), tag)
        self.assertEqual([r.get("version") for r in c.findall("releases/release")], ["1"])
        self.assertIsNone(c.find("launchable"))
        self.assertIsNone(c.find("bundle"))
        self.assertEqual(
            {v.get("key"): v.text for v in c.findall("custom/value")},
            {"NixOS::flathub-id": "org.tool.Tool", "NixOS::flathub-metadata-license": "CC0-1.0"},
        )

    def test_copies_translations_and_remote_icon_only(self) -> None:
        root, _ = self.run_merge(ours("org.tool.Tool", "Tool", "tool"))
        c = root[0]
        self.assertEqual([d.findtext("p") for d in c.findall("description")], ["From Flathub", "Von Flathub"])
        self.assertEqual([i.get("type") for i in c.findall("icon")], ["remote"])

    def test_licence_not_allowed(self) -> None:
        root, lines = self.run_merge(ours("org.tool.Tool", "Tool", "tool"), license="GPL-3.0-or-later")
        c = root[0]
        self.assertIsNone(c.find("description"))
        self.assertIsNone(c.find("custom"))
        self.assertIn("1 matches were skipped for their metadata licence.", "\n".join(lines))

    def test_no_licence_found(self) -> None:
        root, lines = self.run_merge(ours("org.tool.Tool", "Tool", "tool"), license=None)
        self.assertIsNone(root.find("component/description"))
        report = "\n".join(lines)
        self.assertIn("1 matches were skipped for their metadata licence.", report)
        self.assertIn("`org.tool.tool` → `org.tool.Tool`: no metadata licence found", report)

    def test_summary_as_description_is_replaced(self) -> None:
        # As the generator writes nix's summary given as longDescription.
        desc = '\n  <description><p>A tool</p></description>\n  <description xml:lang="en"><p>A tool</p></description>'
        root = catalogue(ours("tool.desktop", "Tool", "tool", desc))
        packages = {"tool": {"pname": "tool", "meta": {"description": "A tool", "homepage": "https://tool.org"}}}
        fm.merge(root, catalogue(self.THEIRS), packages, "s", lambda refs: {r: "CC0-1.0" for r in refs})
        c = root[0]
        self.assertEqual([d.findtext("p") for d in c.findall("description")], ["From Flathub", "Von Flathub"])
        self.assertTrue(all(e.tail == "\n  " for e in c[:-1]))

    def test_real_description_is_kept(self) -> None:
        desc = "\n  <description><p>A tool for things</p></description>"
        root = catalogue(ours("tool.desktop", "Tool", "tool", desc))
        packages = {"tool": {"pname": "tool", "meta": {"description": "A tool", "homepage": "https://tool.org"}}}
        fm.merge(root, catalogue(self.THEIRS), packages, "s", lambda refs: {r: "CC0-1.0" for r in refs})
        self.assertEqual([d.findtext("p") for d in root.findall("component/description")], ["A tool for things"])

    def test_summary_kept_without_a_better_one(self) -> None:
        c = ET.fromstring(ours("tool.desktop", "Tool", "tool", "\n  <description><p>A tool</p></description>"))
        fm.enrich(c, ET.fromstring(flathub("org.tool.Tool", "Tool")), "A tool")
        self.assertEqual([d.findtext("p") for d in c.findall("description")], ["A tool"])

    def test_keeps_indentation(self) -> None:
        root, _ = self.run_merge(ours("org.tool.Tool", "Tool", "tool"))
        c = root[0]
        self.assertEqual(c[-1].tail, "\n")
        self.assertTrue(all(e.tail == "\n  " for e in c[:-1]))


class GVariant(unittest.TestCase):
    def test_strings_and_arrays(self) -> None:
        self.assertEqual(om.parse("s", b"abc\0"), "abc")
        self.assertEqual(om.parse("(ss)", b"a\0bc\0\x02"), ("a", "bc"))
        self.assertEqual(om.parse("as", b"a\0bc\0\x02\x05"), ["a", "bc"])
        self.assertEqual(om.parse("ay", b"\x01\x02"), b"\x01\x02")
        self.assertEqual(om.parse("au", b"\x01\0\0\0\x02\0\0\0"), [1, 2])

    def test_dict_of_variants(self) -> None:
        # {"k": <uint32 7>}: the entry is "k\0", padding to 8, the variant
        # (7, \0, "u") and the key's end offset.
        entry = b"k\0" + b"\0" * 6 + b"\x07\0\0\0\0u" + b"\x02"
        self.assertEqual(om.parse("a{sv}", entry + bytes([len(entry)])), {"k": 7})

    def test_dirtree(self) -> None:
        # ([("a", csum)], [("d", tree, meta)]) with 1-byte checksums.
        # Each array ends with its elements' end offsets, each tuple with
        # those of its variable-size members but the last, in reverse.
        files = b"a\0\x01\x02" + b"\x04"
        dirs = b"d\0\x03\x04\x03\x02" + b"\x06"
        data = files + dirs + bytes([len(files)])
        self.assertEqual(om.parse("(a(say)a(sayay))", data), ([("a", b"\x01")], [("d", b"\x03", b"\x04")]))


if __name__ == "__main__":
    unittest.main()
