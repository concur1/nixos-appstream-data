"""Fixture tests for scripts/flathub_merge.py and scripts/ostree_metainfo.py.

  python3 -m unittest discover tests
"""

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flathub_merge as fm  # noqa: E402
import ostree_metainfo as om  # noqa: E402


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
    def test_id_ignores_case_and_desktop_suffix(self):
        m = matches(
            catalogue(ours("org.Example.App.desktop", "App", "app")),
            catalogue(flathub("org.example.app", "Something else")),
        )
        self.assertEqual(m["org.example.app"], ("matched", "id", ["org.example.app"]))

    def test_homepage_normalised(self):
        m = matches(
            catalogue(ours("app.desktop", "Tool", "tool", '\n  <url type="homepage">http://www.tool.org/index.html</url>')),
            catalogue(flathub("org.tool.Tool", "Tool", homepage="https://tool.org/")),
        )
        self.assertEqual(m["app"], ("matched", "homepage", ["org.tool.Tool"]))

    def test_homepage_from_nix_meta_and_flathub_vcs(self):
        m = matches(
            catalogue(ours("tool.desktop", "Tool", "tool")),
            catalogue(flathub("io.github.me.Tool", "Tool", homepage="https://tool.example", vcs="https://github.com/me/tool")),
            {"tool": {"pname": "tool", "meta": {"homepage": "https://github.com/me/tool"}}},
        )
        self.assertEqual(m["tool"], ("matched", "homepage", ["io.github.me.Tool"]))

    def test_bare_forge_homepage_is_ignored(self):
        m = matches(
            catalogue(ours("tool.desktop", "Tool", "tool", '\n  <url type="homepage">https://github.com</url>')),
            catalogue(flathub("com.github.Tool", "Tool Thing", homepage="https://github.com/")),
        )
        self.assertNotIn("tool", m)

    def test_homepage_shared_by_two_flathub_apps_is_ambiguous(self):
        m = matches(
            catalogue(ours("viewer.desktop", "Suite Viewer", "suite", '\n  <url type="homepage">https://suite.org</url>')),
            catalogue(
                flathub("org.suite.Viewer", "Suite Viewer", homepage="https://suite.org"),
                flathub("org.suite.Editor", "Suite Editor", homepage="https://suite.org"),
            ),
        )
        self.assertEqual(m["viewer"][0], "ambiguous")

    def test_homepage_shared_by_two_of_our_apps_is_ambiguous(self):
        home = '\n  <url type="homepage">https://suite.org</url>'
        m = matches(
            catalogue(ours("suite-a.desktop", "Suite A", "suite-a", home), ours("suite-b.desktop", "Suite B", "suite-b", home)),
            catalogue(flathub("org.suite.Suite", "Suite", homepage="https://suite.org")),
        )
        self.assertEqual(m["suite-a"][0], "ambiguous")
        self.assertEqual(m["suite-b"][0], "ambiguous")

    def test_attrs_sharing_an_id_are_one_app(self):
        home = '\n  <url type="homepage">https://suite.org</url>'
        root = catalogue(ours("org.suite.App", "App", "app", home), ours("org.suite.App", "App", "app-bin", home))
        fh = catalogue(flathub("org.suite.Suite", "App Suite", homepage="https://suite.org"))
        self.assertEqual(matches(root, fh)["org.suite.app"], ("matched", "homepage", ["org.suite.Suite"]))

    def test_homepage_needs_a_shared_name_word(self):
        m = matches(
            catalogue(ours("com.dev.Stamp", "Stamp", "stamp", '\n  <url type="homepage">https://dev.org</url>')),
            catalogue(flathub("com.dev.BloodPressure", "Blood Pressure", homepage="https://dev.org")),
        )
        self.assertNotIn("com.dev.stamp", m)

    def test_homepage_with_other_repository_is_no_match(self):
        extra = '\n  <url type="homepage">https://dux.dev</url>\n  <url type="vcs-browser">https://github.com/dux/files</url>'
        m = matches(
            catalogue(ours("dev.dux.Files", "Dux Files", "dux-files", extra)),
            catalogue(flathub("dev.dux.Calendar", "Dux Calendar", homepage="https://dux.dev", vcs="https://github.com/dux/calendar")),
        )
        self.assertNotIn("dev.dux.files", m)

    def test_name_rule_needs_id_segment_and_name(self):
        fh = catalogue(flathub("com.vendor.Discord", "Discord"), flathub("com.other.Slack", "Slack Desktop"))
        m = matches(catalogue(ours("discord.desktop", "Discord", "discord"), ours("slack.desktop", "Slack", "slack")), fh)
        self.assertEqual(m["discord"], ("matched", "name", ["com.vendor.Discord"]))
        self.assertNotIn("slack", m)

    def test_stronger_rule_decides(self):
        # The ID match wins, and a weaker rule may not take the same app.
        m = matches(
            catalogue(
                ours("org.tool.Tool", "Tool", "tool"),
                ours("tool-extra.desktop", "Tool Extra", "tool-extra", '\n  <url type="homepage">https://tool.org</url>'),
            ),
            catalogue(flathub("org.tool.Tool", "Tool", homepage="https://tool.org")),
        )
        self.assertEqual(m["org.tool.tool"], ("matched", "id", ["org.tool.Tool"]))
        self.assertEqual(m["tool-extra"][0], "ambiguous")

    def test_only_apps_match(self):
        fh = catalogue(
            '<component type="runtime"><id>org.tool.Tool</id><name>Tool</name></component>'
        )
        self.assertEqual(matches(catalogue(ours("org.tool.Tool", "Tool", "tool")), fh), {})


class Select(unittest.TestCase):
    def test_unfree_attrs_matching_flathub(self):
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


class Licences(unittest.TestCase):
    def test_license_allowed(self):
        for expr in ["CC0-1.0", "CC0", "MIT", "FSFAP", "CC-BY-SA-4.0", "GFDL-1.3-or-later", "GFDL-1.3+",
                     "GPL-2.0+ or GFDL-1.3-only", "CC0-1.0 AND CC-BY-4.0"]:  # fmt: skip
            self.assertTrue(fm.license_allowed(expr), expr)
        for expr in [None, "", "GPL-3.0-or-later", "LicenseRef-proprietary", "CC0-1.0 AND GPL-3.0",
                     "MIT WITH Some-exception", "(CC0-1.0 OR MIT)", "CC-BY-NC-4.0"]:  # fmt: skip
            self.assertFalse(fm.license_allowed(expr), expr)


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

    def run_merge(self, component: str, license="CC0-1.0"):
        root = catalogue(component)
        lines = fm.merge(root, catalogue(self.THEIRS), {}, "nixos-unstable", lambda refs: {r: license for r in refs})
        return root, lines

    def test_fills_only_missing_fields(self):
        root, _ = self.run_merge(
            ours(
                "org.tool.Tool",
                "Tool",
                "tool",
                "\n  <description><p>Ours</p></description>\n  <url type=\"homepage\">https://tool.example</url>"
                '\n  <icon type="stock">tool</icon>\n  <releases><release version="1"/></releases>',
            )
        )
        self.assertEqual(root.get("origin"), "nixos-unstable")
        c = root.find("component")
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

    def test_copies_translations_and_remote_icon_only(self):
        root, _ = self.run_merge(ours("org.tool.Tool", "Tool", "tool"))
        c = root.find("component")
        self.assertEqual([d.findtext("p") for d in c.findall("description")], ["From Flathub", "Von Flathub"])
        self.assertEqual([i.get("type") for i in c.findall("icon")], ["remote"])

    def test_licence_not_allowed(self):
        root, lines = self.run_merge(ours("org.tool.Tool", "Tool", "tool"), license="GPL-3.0-or-later")
        c = root.find("component")
        self.assertIsNone(c.find("description"))
        self.assertIsNone(c.find("custom"))
        self.assertIn("1 matches were skipped for their metadata licence.", "\n".join(lines))

    def test_no_licence_found(self):
        root, _ = self.run_merge(ours("org.tool.Tool", "Tool", "tool"), license=None)
        self.assertIsNone(root.find("component/description"))

    def test_keeps_indentation(self):
        root, _ = self.run_merge(ours("org.tool.Tool", "Tool", "tool"))
        c = root.find("component")
        self.assertEqual(c[-1].tail, "\n")
        self.assertTrue(all(e.tail == "\n  " for e in c[:-1]))


class GVariant(unittest.TestCase):
    def test_strings_and_arrays(self):
        self.assertEqual(om.parse("s", b"abc\0"), "abc")
        self.assertEqual(om.parse("(ss)", b"a\0bc\0\x02"), ("a", "bc"))
        self.assertEqual(om.parse("as", b"a\0bc\0\x02\x05"), ["a", "bc"])
        self.assertEqual(om.parse("ay", b"\x01\x02"), b"\x01\x02")
        self.assertEqual(om.parse("au", b"\x01\0\0\0\x02\0\0\0"), [1, 2])

    def test_dict_of_variants(self):
        # {"k": <uint32 7>}: the entry is "k\0", padding to 8, the variant
        # (7, \0, "u") and the key's end offset.
        entry = b"k\0" + b"\0" * 6 + b"\x07\0\0\0\0u" + b"\x02"
        self.assertEqual(om.parse("a{sv}", entry + bytes([len(entry)])), {"k": 7})

    def test_dirtree(self):
        # ([("a", csum)], [("d", tree, meta)]) with 1-byte checksums.
        # Each array ends with its elements' end offsets, each tuple with
        # those of its variable-size members but the last, in reverse.
        files = b"a\0\x01\x02" + b"\x04"
        dirs = b"d\0\x03\x04\x03\x02" + b"\x06"
        data = files + dirs + bytes([len(files)])
        self.assertEqual(om.parse("(a(say)a(sayay))", data), ([("a", b"\x01")], [("d", b"\x03", b"\x04")]))


if __name__ == "__main__":
    unittest.main()
