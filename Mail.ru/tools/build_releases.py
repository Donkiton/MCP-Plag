"""Rebuild release archives and checksums from the clean source tree."""
from pathlib import Path
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RELEASES = (("mail-1.0.0.zip", ROOT / "plugin"),
            ("mailru-mcp-0.4.0.zip", ROOT / "server"))


def source_files(directory):
    return sorted(p for p in directory.rglob("*") if p.is_file()
                  and not {"__pycache__", ".pytest_cache", ".venv", "venv"}.intersection(p.parts)
                  and p.suffix != ".pyc")


def main():
    releases = ROOT / "releases"
    releases.mkdir(exist_ok=True)
    for name, source in RELEASES:
        with zipfile.ZipFile(releases / name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in source_files(source):
                assert not path.is_symlink(), path
                assert path.name != ".env" and (not path.name.startswith(".env.") or path.name == ".env.example"), path
                assert path.suffix not in (".db", ".sqlite3", ".pem", ".key", ".log"), path
                entry = zipfile.ZipInfo(path.relative_to(source).as_posix(), (2026, 10, 15, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
                archive.writestr(entry, path.read_bytes())
    lines = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(ROOT).as_posix()
             for p in source_files(ROOT) if p.name != "SHA256SUMS"]
    (ROOT / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"Built {len(RELEASES)} archives and {len(lines)} checksums")


if __name__ == "__main__":
    main()
