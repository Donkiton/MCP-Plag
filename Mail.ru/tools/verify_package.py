"""Verify the archived plugin/server without credentials or network access."""
from pathlib import Path
import hashlib
import json
import struct
import zipfile
import zlib
from build_releases import RELEASES, source_files

ROOT = Path(__file__).resolve().parents[1]


def contained_file(base, reference):
    path = (base / reference).resolve()
    assert path.is_relative_to(base.resolve()), f"Path outside package: {reference}"
    assert path.is_file(), f"Missing package resource: {reference}"
    return path


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"Invalid PNG: {path.name}"
    offset, compressed, size, ended = 8, bytearray(), None, False
    palette = False
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert zlib.crc32(kind + payload) & 0xffffffff == crc, path.name
        if kind == b"IHDR":
            width, height, depth, color, compression, filters, interlace = struct.unpack(">IIBBBBB", payload)
            assert width > 0 and height > 0 and depth in (1, 2, 4, 8, 16) and color in (0, 2, 3, 4, 6), path.name
            assert (compression, filters, interlace) == (0, 0, 0), path.name
            size = (width, height, depth, color)
        elif kind == b"PLTE":
            palette = bool(payload) and len(payload) % 3 == 0
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            ended = True
            offset += length + 12
            break
        offset += length + 12
    assert ended and offset == len(data) and size is not None, path.name
    width, height, depth, color = size
    assert color != 3 or palette, path.name
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = 1 + (width * depth * channels + 7) // 8
    raw = zlib.decompress(compressed)
    assert len(raw) == height * stride, path.name
    assert all(raw[row * stride] <= 4 for row in range(height)), path.name
    return width, height


def main():
    plugin = ROOT / "plugin"
    legacy = json.loads((plugin / ".codex-plugin/plugin.json").read_text("utf-8"))
    assert legacy["version"] == "1.0.0"
    assert legacy["name"] == "dev-6acf78aebe5481918fdac47ce0b7e938"
    assert legacy["interface"]["displayName"] == "Mail"
    assert not (plugin / "plugin.json").exists(), "Keep the new App's actual package layout"
    apps = json.loads(contained_file(plugin, legacy["apps"]).read_text("utf-8"))
    assert apps["apps"] == {legacy["name"]: {"id": "asdk_app_6acf78aebe5481918fdac47ce0b7e938"}}
    assert {p.relative_to(plugin).as_posix() for p in source_files(plugin)} == {
        ".app.json", ".codex-plugin/plugin.json"}, "Unexpected files in the current Mail package"
    for size in (64, 128, 256):
        assert png_size(ROOT / f"app-assets/mail-{size}.png") == (size, size)
    package = ROOT / "server/mailru_mcp"
    assert png_size(package / "icon.png") == (512, 512)
    png_size(package / "login-icon.png")
    for name in ("login.html", "login.css", "login.js", "roboto.ttf", "roboto-OFL.txt"):
        assert (package / name).is_file() and (package / name).stat().st_size > 0, name
    html = (package / "login.html").read_text("utf-8")
    css = (package / "login.css").read_text("utf-8")
    nginx = (ROOT / "server/deploy/nginx-vdsina.conf").read_text("utf-8")
    for name in ("login.css", "login.js", "login-icon.png"):
        assert name in html, name
    assert "roboto.ttf" in css
    for name in ("login", "roboto", "cancel", "icon"):
        assert name in nginx, name
    assert "VERSION='0.4.0'" in (package / "server.py").read_text("utf-8")
    assert hashlib.sha256((package / "icon.png").read_bytes()).hexdigest() == "58e1151c20b3f3ea5e8a4a31fd0ea9c543904b462ad12d5e367a16e81a674740"
    for name in ("nginx-vdsina.conf", "nginx-site.example.conf"):
        assert "client_max_body_size 12m;" in (ROOT / "server/deploy" / name).read_text("utf-8"), name
    assert {p.name for p in (ROOT / "releases").glob("*.zip")} == {name for name, _ in RELEASES}, "Only current releases belong in this snapshot"
    for archive, directory in RELEASES:
        with zipfile.ZipFile(ROOT / "releases" / archive) as bundle:
            assert bundle.testzip() is None, archive
            expected = {p.relative_to(directory).as_posix(): p for p in source_files(directory)}
            assert len(bundle.namelist()) == len(expected) and set(bundle.namelist()) == set(expected), archive
            for name, path in expected.items():
                assert bundle.read(name) == path.read_bytes(), name
    tracked = set()
    for line in (ROOT / "SHA256SUMS").read_text("utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        path = contained_file(ROOT, relative)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, relative
        tracked.add(relative)
    expected = {p.relative_to(ROOT).as_posix() for p in source_files(ROOT) if p.name != "SHA256SUMS"}
    assert tracked == expected, "Checksum manifest is incomplete"
    print(f"OK: Mail 1.0.0, server 0.4.0, five PNG icons, two archives, {len(tracked)} checksums")


if __name__ == "__main__":
    main()
