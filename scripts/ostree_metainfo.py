"""Reads an app's own metainfo file out of a remote OSTree repo (Flathub's),
for its <metadata_license>: Flathub's catalogue and API leave that tag out.

An archive-z2 repo is plain files over HTTPS, so this needs no ostree or
flatpak: the summary maps refs to commits, and from a commit the path
files/share/metainfo/ is a few directory objects away. Objects are GVariant,
read by the small parser below (only the types OSTree uses).
"""

from __future__ import annotations

import gzip
import struct
import urllib.request
import zlib

# --- GVariant --------------------------------------------------------------

_FIXED = {"y": 1, "b": 1, "n": 2, "q": 2, "i": 4, "u": 4, "h": 4, "x": 8, "t": 8, "d": 8}


def _split_type(t: str, i: int = 0) -> int:
    """End index of the single complete type starting at t[i]."""
    c = t[i]
    if c in "am":
        return _split_type(t, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        i += 1
        while t[i] != close:
            i = _split_type(t, i)
        return i + 1
    return i + 1


def _members(t: str) -> list[str]:
    """The member types of a tuple or dict entry type."""
    out, i = [], 1
    while i < len(t) - 1:
        j = _split_type(t, i)
        out.append(t[i:j])
        i = j
    return out


def _align(t: str) -> int:
    c = t[0]
    if c in _FIXED:
        return _FIXED[c]
    if c in "am":
        return _align(t[1:])
    if c in "({":
        return max((_align(m) for m in _members(t)), default=1)
    if c == "v":
        return 8
    return 1  # s, o, g


def _fixed_size(t: str) -> int | None:
    """Size of a fixed-size type, or None for variable-size ones."""
    c = t[0]
    if c in _FIXED:
        return _FIXED[c]
    if c in "({":
        size = 0
        for m in _members(t):
            s = _fixed_size(m)
            if s is None:
                return None
            a = _align(m)
            size = (size + a - 1) // a * a + s
        a = _align(t)
        # A unit tuple is one byte.
        return max((size + a - 1) // a * a, 1)
    return None


def _offset_size(n: int) -> int:
    if n <= 0xFF:
        return 1
    if n <= 0xFFFF:
        return 2
    if n <= 0xFFFFFFFF:
        return 4
    return 8


def _read_offset(data: bytes, pos: int, size: int) -> int:
    return int.from_bytes(data[pos : pos + size], "little")


def parse(t: str, data: bytes):
    """Deserialises GVariant `data` of type `t`. Byte arrays come back as
    bytes, dict arrays as dicts, variants as their value."""
    c = t[0]
    if c in _FIXED:
        if len(data) < _FIXED[c]:
            return 0
        fmt = {"y": "B", "b": "?", "n": "h", "q": "H", "i": "i", "u": "I", "h": "i", "x": "q", "t": "Q", "d": "d"}[c]
        return struct.unpack("<" + fmt, data[: _FIXED[c]])[0]
    if c in "sog":
        return data[:-1].decode("utf-8", "replace") if data else ""
    if c == "v":
        sep = data.rfind(b"\0")
        return parse(data[sep + 1 :].decode(), data[:sep])
    if c == "m":
        if not data:
            return None
        inner = t[1:]
        return parse(inner, data if _fixed_size(inner) is not None else data[:-1])
    if c == "a":
        inner = t[1:]
        items = _parse_array(inner, data)
        if inner[0] == "{":
            return dict(items)
        if inner == "y":
            return bytes(data)
        return items
    if c in "({":
        return _parse_tuple(_members(t), data)
    raise ValueError(f"unsupported GVariant type {t!r}")


def _parse_array(inner: str, data: bytes) -> list:
    if not data:
        return []
    size = _fixed_size(inner)
    if size is not None:
        return [parse(inner, data[i : i + size]) for i in range(0, len(data), size)]
    osz = _offset_size(len(data))
    last_end = _read_offset(data, len(data) - osz, osz)
    n = (len(data) - last_end) // osz
    items, start, align = [], 0, _align(inner)
    for k in range(n):
        end = _read_offset(data, last_end + k * osz, osz)
        start = (start + align - 1) // align * align
        items.append(parse(inner, data[start:end]))
        start = end
    return items


def _parse_tuple(members: list[str], data: bytes) -> tuple:
    osz = _offset_size(len(data))
    # Offsets of variable-size members but the last, stored from the end.
    offsets_end = len(data)
    values, pos = [], 0
    for k, m in enumerate(members):
        a = _align(m)
        pos = (pos + a - 1) // a * a
        size = _fixed_size(m)
        if size is not None:
            end = pos + size
        elif k == len(members) - 1:
            end = offsets_end
        else:
            offsets_end -= osz
            end = _read_offset(data, offsets_end, osz)
        values.append(parse(m, data[pos:end]))
        pos = end
    return tuple(values)


# --- OSTree ----------------------------------------------------------------

FLATHUB = "https://dl.flathub.org/repo"


def _get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nixos-appstream-data"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _object_url(repo: str, checksum: bytes | str, kind: str) -> str:
    h = checksum.hex() if isinstance(checksum, bytes) else checksum
    return f"{repo}/objects/{h[:2]}/{h[2:]}.{kind}"


def ref_commits(repo: str = FLATHUB, arch: str = "x86_64") -> dict[str, str]:
    """Ref -> commit checksum, from the repo's per-arch summary (a fraction
    of the full one, which also lists every other arch)."""
    idx = parse("(a{s(ayaaya{sv})}a{sv})", _get(f"{repo}/summary.idx"))
    subsummary = idx[0][arch][0].hex()
    summary = parse(
        "(a(s(taya{sv}))a{sv})",
        gzip.decompress(_get(f"{repo}/summaries/{subsummary}.gz")),
    )
    return {ref: data[1].hex() for ref, data in summary[0]}


def _dirtree(repo: str, checksum: bytes) -> tuple[dict, dict]:
    files, dirs = parse("(a(say)a(sayay))", _get(_object_url(repo, checksum, "dirtree")))
    return dict(files), {name: tree for name, tree, _meta in dirs}


def _file_content(repo: str, checksum: bytes) -> bytes:
    data = _get(_object_url(repo, checksum, "filez"))
    # A big-endian header size and 4 bytes of padding, the header, then the
    # content as raw deflate.
    (hsize,) = struct.unpack(">I", data[:4])
    return zlib.decompress(data[8 + hsize :], -15)


def metainfo(repo: str, commit: str, app_id: str) -> bytes | None:
    """The app's metainfo file in `commit`, or None if it has none.

    flatpak-builder installs it as files/share/metainfo/<id>.metainfo.xml;
    older apps use share/appdata/ and .appdata.xml."""
    root = parse("(a{sv}aya(say)sstayay)", _get(_object_url(repo, commit, "commit")))[6]
    tree = root
    for name in ("files", "share"):
        _files, dirs = _dirtree(repo, tree)
        if name not in dirs:
            return None
        tree = dirs[name]
    _files, dirs = _dirtree(repo, tree)
    for sub in ("metainfo", "appdata"):
        if sub not in dirs:
            continue
        files, _dirs = _dirtree(repo, dirs[sub])
        for suffix in (".metainfo.xml", ".appdata.xml"):
            if app_id + suffix in files:
                return _file_content(repo, files[app_id + suffix])
    return None
