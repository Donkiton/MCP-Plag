"""Verify @tg manifests, every custom icon slot, archives and source checksums.

No credentials or Telegram account are required. --endpoint adds read-only HTTPS
checks of the actually served image bytes; it never logs in or sends messages.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct
import urllib.request
import zipfile
import zlib
from build_releases import ROOT, RELEASES, VERSION, distribution_files, is_private_path, source_files
from verify_app_source import verify_app_source

EXPECTED_ENDPOINT = "https://tg-77-238-249-144.sslip.io/mcp"
APPROVED_MASTER = "6911373f58b7298fd65ed37c91f6a18d4640d4eb0b698fad4a5feaf4ff264ab8"
ICON_FIELDS = ("logo", "logoDark", "composerIcon", "composerIconDark")


def contained_file(base, reference):
    path = (base / reference).resolve()
    assert path.is_relative_to(base.resolve()), f"Path escapes package: {reference}"
    assert path.is_file() and not path.is_symlink(), f"Missing/unsafe file: {reference}"
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_info(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"Invalid PNG: {path.name}"
    offset, compressed, header, ended, transparency = 8, bytearray(), None, False, False
    palette = False
    while offset < len(data):
        assert offset + 12 <= len(data), path.name
        length = struct.unpack_from(">I", data, offset)[0]
        assert offset + length + 12 <= len(data), path.name
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert zlib.crc32(kind + payload) & 0xffffffff == crc, path.name
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
            width, height, depth, color, compression, filters, interlace = header
            assert width > 0 and height > 0 and depth in (1, 2, 4, 8, 16), path.name
            assert color in (0, 2, 3, 4, 6) and (compression, filters, interlace) == (0, 0, 0), path.name
            transparency = color in (4, 6)
        elif kind == b"PLTE":
            palette = bool(payload) and len(payload) % 3 == 0
        elif kind == b"tRNS":
            transparency = bool(payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            ended = True
            offset += length + 12
            break
        offset += length + 12
    assert ended and offset == len(data) and header, path.name
    width, height, depth, color, *_ = header
    assert color != 3 or palette, path.name
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = 1 + (width * depth * channels + 7) // 8
    raw = zlib.decompress(compressed)
    assert len(raw) == height * stride, path.name
    assert all(raw[row * stride] <= 4 for row in range(height)), path.name
    return width, height, transparency


def verify_manifests():
    plugin = ROOT / "plugin"
    manifest = json.loads((plugin / "plugin.json").read_text("utf-8"))
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert manifest["name"] == "tg" and manifest["version"] == VERSION
    assert not {"skills", "mcpServers", "apps", "interface"}.intersection(manifest)
    extension = manifest["extensions"]["com.openai"]
    interface = extension["interface"]
    assert interface["displayName"] == "tg"
    assert 0 < len(interface["shortDescription"]) <= 30
    assert interface["category"] == "Communication"
    assert isinstance(interface["capabilities"], list)
    for field in ICON_FIELDS:
        ref = interface[field]
        assert ref.startswith("./assets/"), field
        width, height, transparent = png_info(contained_file(plugin, ref))
        assert 48 <= width <= 4096 and width == height and transparent, field
        assert contained_file(plugin, ref).stat().st_size <= 5 * 1024 * 1024, field
    for field in ("brandColor", "brandColorDark"):
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", interface[field]), field
    prompts = interface["defaultPrompt"]
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert len(set(prompts)) == len(prompts)
    assert all(isinstance(p, str) and 0 < len(p) <= 128 and "\n" not in p for p in prompts)
    mcp = json.loads((plugin / "mcp.json").read_text("utf-8"))
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert set(mcp["mcpServers"]) == {"tg"}
    connection = mcp["mcpServers"]["tg"]
    assert connection["type"] == "streamable-http" and connection["url"] == EXPECTED_ENDPOINT
    auth = connection["extensions"]["com.openai"]["auth"]
    assert auth == {"type": "oauth", "client": {"mode": "dcr"}}
    assert not (plugin / ".app.json").exists() and "apps" not in extension, "Do not package an invented/duplicate App binding"
    assert not (plugin / ".codex-plugin/plugin.json").exists(), "Canonical inline OpenAI settings must remain unambiguous"
    skill = (plugin / "skills/tg-workflow/SKILL.md").read_text("utf-8")
    assert skill.startswith("---\nname: tg-workflow\ndescription: ")
    assert "telegram-accounts-rename" in skill and "account_id" in skill and "timeout" in skill
    return interface


def verify_icons():
    manifest = json.loads((ROOT / "app-assets/manifest.json").read_text("utf-8"))
    assert manifest["approved_variant"] == "A" and len(manifest["slots"]) == 18
    assets = {entry["path"]: entry for entry in manifest["assets"]}
    assert sha(ROOT / "app-assets/tg-master.png") == APPROVED_MASTER
    actual = {p.relative_to(ROOT).as_posix() for base in ("plugin/assets", "app-assets", "server/assets")
              for p in (ROOT / base).rglob("*") if p.is_file() and p.suffix.lower() in (".png", ".ico")}
    assert actual == set(assets), "Untracked or missing branding asset"
    for name, entry in assets.items():
        path = contained_file(ROOT, name)
        assert path.stat().st_size == entry["bytes"] and sha(path) == entry["sha256"], name
        if path.suffix == ".png":
            width, height, transparent = png_info(path)
            assert width == height and transparent, name
        else:
            reserved, kind, count = struct.unpack_from("<HHH", path.read_bytes())
            assert (reserved, kind, count) == (0, 1, 6), name
    for slot, name in manifest["slots"].items():
        assert name in assets, slot
    for size in (64, 128, 256):
        path = ROOT / f"app-assets/tg-{size}.png"
        assert png_info(path)[:2] == (size, size) and path.stat().st_size < 10_000
    for field in ICON_FIELDS:
        assert manifest["slots"][f"plugin.{field}"].startswith("plugin/assets/")
    return manifest


def verify_archives():
    assert {p.name for p in (ROOT / "releases").glob("*.zip")} == {name for name, _ in RELEASES}, "Unexpected release archive"
    for name, source in RELEASES:
        path = ROOT / "releases" / name
        expected = {p.relative_to(source).as_posix(): p for p in source_files(source)}
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            assert archive.testzip() is None and len(names) == len(set(names)), name
            assert set(names) == set(expected), f"Archive is stale or incomplete: {name}"
            for entry in names:
                assert not is_private_path(Path(entry)), entry
                assert not entry.startswith(("/", "../")) and "\\" not in entry, entry
                assert archive.read(entry) == expected[entry].read_bytes(), entry
                assert archive.getinfo(entry).date_time == (2026, 10, 25, 0, 0, 0), entry
            if source.name == "server":
                assert not any(n.startswith(("app/", "web/", "node_modules/", "data/")) for n in names)
                for required in ("package.json", "bun.lock", "LICENSE", "src/server.tsx", "assets/icon-512.png"):
                    assert required in names, required
    expected = {p.relative_to(ROOT).as_posix(): p for p in distribution_files()}
    recorded = {}
    for line in (ROOT / "SHA256SUMS").read_text("utf-8").splitlines():
        digest, name = line.split("  ", 1)
        assert name not in recorded, name
        assert sha(contained_file(ROOT, name)) == digest, name
        recorded[name] = digest
    assert set(recorded) == set(expected), "Checksum inventory is incomplete or stale"
    return len(recorded)


def verify_live(origin):
    assert origin.startswith("https://") and not origin.endswith("/"), "Use bare HTTPS origin"
    paths = {
        "/icon.png": "server/assets/icon-128.png",
        "/icon-256.png": "server/assets/icon-256.png",
        "/icon-512.png": "server/assets/icon-512.png",
        "/favicon.ico": "server/assets/favicon.ico",
        "/apple-touch-icon.png": "server/assets/apple-touch-icon.png",
    }
    for url_path, local in paths.items():
        request = urllib.request.Request(origin + url_path, headers={"User-Agent": "tg-package-verifier/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 200
            actual = response.read(5 * 1024 * 1024 + 1)
        assert actual == (ROOT / local).read_bytes(), f"Unexpected served icon: {url_path}"
    return len(paths)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", help="Optional bare HTTPS origin for read-only served-icon checks")
    args = parser.parse_args()
    interface = verify_manifests()
    app = verify_app_source()
    icons = verify_icons()
    checksums = verify_archives()
    served = verify_live(args.endpoint) if args.endpoint else None
    print(json.dumps({"status": "ok", "plugin": interface["displayName"], "version": VERSION,
                      "branding_assets": len(icons["assets"]), "icon_slots": len(icons["slots"]),
                      "archives": len(RELEASES), "checksums": checksums,
                      "app_backed_plugin_id": app["plugin_id"], "app_backed_version": app["version_after"],
                      "app_manifest_preserved": app["interface_preserved"], "custom_skill_icons": app["custom_skill_icons"],
                      "served_icons_verified": served,
                      "note": "See ICON-COVERAGE.md for confirmed App branding, optional skill removal and remaining OAuth/theme/selector checks; this verifier does not authenticate Telegram."},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
