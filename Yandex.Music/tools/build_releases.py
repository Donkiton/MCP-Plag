"""Build reproducible archives from the public source tree, never runtime state."""
from pathlib import Path
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RELEASES = (("yandex-music-1.0.3.zip", ROOT / "plugin"),
            ("yandex-music-mcp-2026.10.25.zip", ROOT / "server"))
CACHES = {"__pycache__", ".pytest_cache", ".venv", "venv", ".git"}
PRIVATE_NAMES = {"token", "oauth.json", ".mcp-http-secret", "authorized_keys", "id_rsa", "id_ed25519"}


def source_files(directory):
    return sorted((p for p in directory.rglob("*") if p.is_file()
                  and not CACHES.intersection(p.relative_to(directory).parts)
                  and p.suffix not in (".pyc", ".pyo")),
                  key=lambda p: p.relative_to(directory).as_posix())


def public_file(path):
    assert not path.is_symlink(), f"Symlinks are not published: {path}"
    assert path.name not in PRIVATE_NAMES, f"Runtime credential: {path}"
    assert not path.name.startswith("id_ed25519"), f"Private key: {path}"
    assert path.name != ".env" and (not path.name.startswith(".env.") or path.name == ".env.example"), path
    assert path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".log", ".bak", ".backup"}, path
    assert not any(part.endswith((".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm")) for part in path.parts), path


def main():
    releases = ROOT / "releases"
    releases.mkdir(exist_ok=True)
    for path in source_files(ROOT):
        public_file(path)
    for name, source in RELEASES:
        with zipfile.ZipFile(releases / name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in source_files(source):
                public_file(path)
                entry = zipfile.ZipInfo(path.relative_to(source).as_posix(), (2026, 10, 25, 0, 0, 0))
                entry.create_system = 3
                entry.compress_type = zipfile.ZIP_DEFLATED
                executable = path.name in {"yandex-music-mcp-once", "yandex-music-mcp-ssh"}
                entry.external_attr = (0o100755 if executable else 0o100644) << 16
                archive.writestr(entry, path.read_bytes())
    files = [p for p in source_files(ROOT) if p.name != "SHA256SUMS"]
    lines = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(ROOT).as_posix() for p in files]
    (ROOT / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"Built {len(RELEASES)} archives and {len(files)} checksums")


if __name__ == "__main__":
    main()
