"""Private consistent backup and checked restore. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import zipfile

from .storage import Storage


BACKUP_VERSION = 1
MAX_RESTORE_BYTES = 1_000_000_000


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _canonical_source_name(name: str, digest: str, broker: str) -> bool:
    suffixes = {"sber": (".html",), "alfa": (".xls", ".xlsx")}
    return broker in suffixes and name in {digest + suffix for suffix in suffixes[broker]}


def create_backup(storage: Storage, destination: Path) -> None:
    """Write a backup only to a new path, with owner-only permissions."""
    if destination.exists():
        raise FileExistsError(destination)
    with TemporaryDirectory(prefix="backup-", dir=storage.root) as temp:
        db_copy = Path(temp) / "ledger.sqlite3"
        with storage._connect() as source, sqlite3.connect(db_copy) as target:
            source.backup(target)
        source_files: dict[str, tuple[Path, str]] = {}
        with sqlite3.connect(db_copy) as snapshot:
            if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite backup failed integrity check")
            snapshot.execute("PRAGMA foreign_keys=ON")
            if snapshot.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("SQLite backup has broken references")
            for expected_sha, private_path, expected_size, broker in snapshot.execute(
                    "SELECT sha256,private_path,size,detected_broker FROM source_files"):
                original = Path(private_path)
                if (original.parent.resolve() != storage.files.resolve() or original.is_symlink()
                        or not _canonical_source_name(original.name, expected_sha, broker)
                        or not original.is_file() or original.stat().st_size != expected_size
                        or _sha(original) != expected_sha):
                    raise ValueError("Ledger source file is missing, differs from its SHA-256, or has inconsistent metadata")
                previous = source_files.setdefault(original.name, (original, expected_sha))
                if previous[1] != expected_sha:
                    raise ValueError("Two ledger files share a name but have different contents")
        paths = {"ledger.sqlite3": db_copy}
        copied_files = Path(temp) / "files"
        copied_files.mkdir(mode=0o700)
        for name, (original, expected_sha) in source_files.items():
            copy = copied_files / name
            shutil.copyfile(original, copy)
            os.chmod(copy, 0o600)
            if _sha(copy) != expected_sha:
                raise ValueError("Ledger source file changed during backup")
            paths[f"files/{name}"] = copy
        manifest = {
            "version": BACKUP_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": {name: {"sha256": _sha(path), "size": path.stat().st_size} for name, path in paths.items()},
        }
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
                for name, path in paths.items():
                    archive.write(path, name)
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def restore_backup(archive_path: Path, destination: Path) -> Storage:
    """Restore into a path that does not exist; validate before making it visible."""
    if destination.exists():
        raise FileExistsError(destination)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="restore-", dir=parent) as temp:
        staging = Path(temp) / "data"
        staging.mkdir(mode=0o700)
        (staging / "files").mkdir(mode=0o700)
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("Invalid backup entries")
            total = sum(info.file_size for info in archive.infolist())
            if total > MAX_RESTORE_BYTES:
                raise ValueError("Backup exceeds restore limit")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("version") != BACKUP_VERSION or set(names) != set(manifest.get("files", {})) | {"manifest.json"}:
                raise ValueError("Unsupported or incomplete backup")
            for name, expected in manifest["files"].items():
                if name != "ledger.sqlite3" and (not name.startswith("files/") or Path(name).name != name[6:] or name[6:] in {"", ".", ".."}):
                    raise ValueError("Unsafe backup path")
                target = staging / name
                with archive.open(name) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                os.chmod(target, 0o600)
                if target.stat().st_size != expected["size"] or _sha(target) != expected["sha256"]:
                    raise ValueError("Backup integrity check failed")
        db_path = staging / "ledger.sqlite3"
        with sqlite3.connect(db_path) as con:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            con.execute("PRAGMA foreign_keys = ON")
            if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Backup has broken references")
            for source_id, expected_sha, old_path, expected_size, broker in con.execute(
                    "SELECT id,sha256,private_path,size,detected_broker FROM source_files").fetchall():
                basename = Path(old_path).name
                restored_source = staging / "files" / basename
                if not _canonical_source_name(basename, expected_sha, broker) or not restored_source.is_file():
                    raise ValueError("Source file metadata does not match backup")
                if _sha(restored_source) != expected_sha:
                    raise ValueError("Source file does not match ledger SHA-256")
                if restored_source.stat().st_size != expected_size:
                    raise ValueError("Source file metadata does not match backup")
                con.execute("UPDATE source_files SET private_path=? WHERE id=?", (str(destination / "files" / basename), source_id))
        os.replace(staging, destination)
        try:
            return Storage(destination)
        except Exception:
            # Storage may reject a coherent archive with an unsupported schema
            # or inconsistent saved account identity. Keep the target absent.
            shutil.rmtree(destination)
            raise
