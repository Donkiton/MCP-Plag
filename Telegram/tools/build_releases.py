"""Build deterministic, credential-free @tg plugin and backend archives."""
from pathlib import Path
import hashlib
import json
import zipfile
from verify_app_source import APP_VERSION, verify_app_source

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
STAMP = (2026, 10, 25, 0, 0, 0)
RELEASES = ((f"tg-{VERSION}.zip", ROOT / "plugin"),
            (f"tg-mcp-{VERSION}.zip", ROOT / "server"),
            (f"tg-app-{APP_VERSION}.zip", ROOT / "plugin-app"))
SERVER_DIRS = {"src", "assets", "scripts", "docs", "tests", "deploy"}
SERVER_FILES = {"package.json", "bun.lock", "tsconfig.json", "README.md", "LICENSE",
                "CHANGELOG.md", ".env.example", "biome.json"}
ROOT_FILES = {"README.md", "CHANGELOG.md", "ICON-COVERAGE.md", "CAPABILITIES.md", "STATUS.md", "provenance.json", ".gitignore"}
EXCLUDED_PARTS = {".git", "node_modules", ".bun", ".cache", "__pycache__", ".pytest_cache",
                  ".venv", "venv", "data", "logs", "uploads", "tmp", "temp", "dist", "dist-ssr"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".key", ".pem", ".p12", ".pfx",
                      ".pyc", ".pyo", ".bak", ".backup"}


def is_private_path(relative):
    """Exclude runtime credentials/state without excluding credential-handling code."""
    if EXCLUDED_PARTS.intersection(relative.parts):
        return True
    name = relative.name.lower()
    if name.startswith(".env") and name != ".env.example":
        return True
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES or ".db-" in name:
        return True
    if name.startswith(("id_rsa", "id_ed25519")):
        return True
    if relative.suffix.lower() == ".json" and not name.endswith(".example.json"):
        if any(word in name for word in ("credentials", "session-store", "access-token", "refresh-token")):
            return True
    return False


def source_files(directory):
    """Return only the intended archive inventory, with no symlinks or runtime data."""
    server = directory.resolve() == (ROOT / "server").resolve()
    result = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if is_private_path(relative):
            continue
        if server and relative.parts[0] not in SERVER_DIRS and relative.as_posix() not in SERVER_FILES:
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing a symlink in package: {relative}")
        if path.is_file():
            result.append(path)
    return result


def distribution_files():
    paths = []
    for folder in ("plugin", "plugin-app", "server", "app-assets", "tools", "releases"):
        directory = ROOT / folder
        if directory.is_dir():
            paths.extend(source_files(directory))
    paths.extend(ROOT / name for name in ROOT_FILES if (ROOT / name).is_file())
    return sorted(set(paths))


def build_archive(destination, source):
    files = source_files(source)
    if not files:
        raise ValueError(f"Empty release source: {source.name}")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            entry = zipfile.ZipInfo(relative, STAMP)
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            archive.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return len(files)


def main():
    manifest = json.loads((ROOT / "plugin/plugin.json").read_text("utf-8"))
    assert manifest["version"] == VERSION
    verify_app_source(write_report=True)
    releases = ROOT / "releases"
    releases.mkdir(exist_ok=True)
    unexpected = {p.name for p in releases.glob("*.zip")} - {name for name, _ in RELEASES}
    assert not unexpected, f"Unexpected archives require review before packaging: {sorted(unexpected)}"
    counts = {name: build_archive(releases / name, source) for name, source in RELEASES}
    lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(ROOT).as_posix()
             for path in distribution_files()]
    (ROOT / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"archives": counts, "checksums": len(lines)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
