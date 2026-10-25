"""Verify the public snapshot, manifests and archives offline without credentials."""
from pathlib import Path, PurePosixPath
import ast
import hashlib
import json
import struct
import zipfile
import zlib
from build_releases import ROOT, RELEASES, source_files, public_file

MUSIC_TOOLS = {"account_info", "search", "get_liked_tracks", "get_listening_history",
               "list_my_playlists", "get_playlist_tracks", "get_album_tracks", "get_artist_tracks",
               "get_similar_tracks", "get_my_wave", "get_chart", "create_playlist",
               "add_tracks_to_playlist", "delete_playlist"}
WHEEL_SHA256 = "e14634255e8168a087e0b7aacf515079f5b505394a0b2b7c2f8f3359146d0d0b"


def contained_file(base, reference):
    relative = PurePosixPath(reference)
    assert not relative.is_absolute() and ".." not in relative.parts, reference
    path = (base / reference).resolve()
    assert path.is_relative_to(base.resolve()) and path.is_file(), reference
    return path


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    offset, compressed, size = 8, bytearray(), None
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert zlib.crc32(kind + payload) & 0xffffffff == crc, path
        if kind == b"IHDR":
            width, height, depth, color, compression, filters, interlace = struct.unpack(">IIBBBBB", payload)
            assert width > 0 and height > 0 and (compression, filters, interlace) == (0, 0, 0), path
            size = width, height
        elif kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12
        if kind == b"IEND":
            assert offset == len(data) and size and zlib.decompress(compressed), path
            return size
    raise AssertionError(f"Incomplete PNG: {path}")


def main():
    files = source_files(ROOT)
    for path in files:
        public_file(path)
        if path.suffix == ".py":
            ast.parse(path.read_text("utf-8"), filename=str(path))
        elif path.suffix == ".json":
            json.loads(path.read_text("utf-8"))
    plugin = ROOT / "plugin"
    current = json.loads((plugin / "plugin.json").read_text("utf-8"))
    compat = json.loads((plugin / ".codex-plugin/plugin.json").read_text("utf-8"))
    name = "dev-6ac88e3d727c8191ab80ae2cf2fa5d54"
    for manifest in (current, compat):
        assert manifest["name"] == name and manifest["version"] == "1.0.3"
    interface = current["extensions"]["com.openai"]
    assert interface["interface"]["displayName"] == compat["interface"]["displayName"] == "Яндекс"
    for config in (interface, compat):
        apps = json.loads(contained_file(plugin, config["apps"]).read_text("utf-8"))
        assert apps["apps"] == {name: {"id": "asdk_app_6ac88e3d727c8191ab80ae2cf2fa5d54"}}
        for field in ("logo", "composerIcon"):
            assert png_size(contained_file(plugin, config["interface"][field])) == (192, 192)
    expected_plugin = {".app.json", "plugin.json", ".codex-plugin/plugin.json", "assets/yandex-icon-v103.png"}
    assert {p.relative_to(plugin).as_posix() for p in source_files(plugin)} == expected_plugin
    png_size(ROOT / "server/yandex-original-logo.png")
    catalog = json.loads((ROOT / "server/music_catalog.json").read_text("utf-8"))
    tools = catalog["tools/list"]["tools"]
    assert len(tools) == 14 and {t["name"] for t in tools} == MUSIC_TOOLS
    assert (ROOT / "server/client/music_catalog.json").read_bytes() == (ROOT / "server/music_catalog.json").read_bytes()
    wheel = ROOT / "server/downloads/yandex_music-3.0.0-py3-none-any.whl"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == WHEEL_SHA256
    with zipfile.ZipFile(wheel) as archive:
        assert archive.testzip() is None
        assert "yandex_music-3.0.0.dist-info/licenses/LICENSE" in archive.namelist()
        for item in archive.infolist():
            relative = PurePosixPath(item.filename)
            assert not relative.is_absolute() and ".." not in relative.parts, item.filename
            public_file(Path(item.filename))
            assert not ((item.external_attr >> 16) & 0o170000) == 0o120000, item.filename
    provenance = json.loads((ROOT / "provenance.json").read_text("utf-8"))
    for item in provenance["files"]:
        assert hashlib.sha256(contained_file(ROOT, item["path"]).read_bytes()).hexdigest() == item["sha256"], item["path"]
    assert {p.name for p in (ROOT / "releases").glob("*.zip")} == {name for name, _ in RELEASES}
    for archive_name, directory in RELEASES:
        with zipfile.ZipFile(ROOT / "releases" / archive_name) as archive:
            assert archive.testzip() is None
            expected = {p.relative_to(directory).as_posix(): p for p in source_files(directory)}
            assert len(archive.namelist()) == len(expected) and set(archive.namelist()) == set(expected), archive_name
            for name, path in expected.items():
                contained_file(directory, name)
                assert archive.read(name) == path.read_bytes(), name
                assert not ((archive.getinfo(name).external_attr >> 16) & 0o170000) == 0o120000, name
    checked = set()
    for line in (ROOT / "SHA256SUMS").read_text("utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        assert relative not in checked, relative
        assert hashlib.sha256(contained_file(ROOT, relative).read_bytes()).hexdigest() == digest, relative
        checked.add(relative)
    assert checked == {p.relative_to(ROOT).as_posix() for p in files if p.name != "SHA256SUMS"}
    print(f"OK: plugin 1.0.3, deployment 2026-10-25, 14 music tools, two archives, {len(checked)} checksums")


if __name__ == "__main__":
    main()
