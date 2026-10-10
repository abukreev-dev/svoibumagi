"""Backup failure controls. SPDX-License-Identifier: GPL-3.0-or-later"""

from pathlib import Path
import hashlib
import json
import sqlite3
import zipfile

import pytest

from svoibumagi.backup import create_backup, restore_backup
from svoibumagi.storage import Storage


def test_tampered_backup_does_not_create_destination(tmp_path: Path):
    storage = Storage(tmp_path / "data")
    storage.create_account("sber", "brokerage", "Synthetic")
    archive_path = tmp_path / "valid.zip"
    create_backup(storage, archive_path)
    tampered_path = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive_path) as original, zipfile.ZipFile(tampered_path, "w") as tampered:
        for name in original.namelist():
            content = original.read(name)
            if name == "ledger.sqlite3":
                content = b"corrupt" + content
            tampered.writestr(name, content)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="integrity check failed"):
        restore_backup(tampered_path, destination)
    assert not destination.exists()
    assert storage.list_accounts()[0]["name"] == "Synthetic"


def test_valid_archive_with_unsupported_schema_does_not_leave_destination(tmp_path: Path):
    storage = Storage(tmp_path / "data")
    storage.create_account("sber", "brokerage", "Synthetic")
    archive_path = tmp_path / "valid.zip"
    create_backup(storage, archive_path)
    altered_db = tmp_path / "future.sqlite3"
    with zipfile.ZipFile(archive_path) as source:
        altered_db.write_bytes(source.read("ledger.sqlite3"))
        manifest = json.loads(source.read("manifest.json"))
    with sqlite3.connect(altered_db) as con:
        con.execute("PRAGMA user_version=99")
    payload = altered_db.read_bytes()
    manifest["files"]["ledger.sqlite3"] = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    future_archive = tmp_path / "future.zip"
    with zipfile.ZipFile(archive_path) as original, zipfile.ZipFile(future_archive, "w") as changed:
        for name in original.namelist():
            changed.writestr(name, json.dumps(manifest) if name == "manifest.json" else
                             payload if name == "ledger.sqlite3" else original.read(name))
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="newer"):
        restore_backup(future_archive, destination)
    assert not destination.exists()
    assert storage.list_accounts()[0]["name"] == "Synthetic"


def test_manifest_cannot_override_source_hash_recorded_in_ledger(tmp_path: Path):
    storage = Storage(tmp_path / "data")
    account = storage.create_account("sber", "brokerage", "Synthetic")
    original = b"synthetic original report"
    digest = hashlib.sha256(original).hexdigest()
    report = storage.files / f"{digest}.html"
    report.write_bytes(original)
    with storage._connect() as con:
        con.execute("INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (account, digest, "synthetic.html", str(report), len(original), "sber", "2025-01-01"))
    archive_path = tmp_path / "valid.zip"
    create_backup(storage, archive_path)
    replacement = b"different synthetic report"
    entry = f"files/{report.name}"
    with zipfile.ZipFile(archive_path) as source:
        manifest = json.loads(source.read("manifest.json"))
    manifest["files"][entry] = {"sha256": hashlib.sha256(replacement).hexdigest(), "size": len(replacement)}
    forged_archive = tmp_path / "forged.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(forged_archive, "w") as forged:
        for name in source.namelist():
            forged.writestr(name, json.dumps(manifest) if name == "manifest.json" else
                            replacement if name == entry else source.read(name))
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="ledger SHA-256"):
        restore_backup(forged_archive, destination)
    assert not destination.exists()
    report.write_bytes(replacement)
    invalid_backup = tmp_path / "invalid.zip"
    with pytest.raises(ValueError, match="differs from its SHA-256"):
        create_backup(storage, invalid_backup)
    assert not invalid_backup.exists()


def test_backup_archives_source_snapshot_when_original_changes_during_writing(tmp_path: Path, monkeypatch):
    storage = Storage(tmp_path / "data")
    account = storage.create_account("sber", "brokerage", "Synthetic")
    original = b"synthetic immutable source"
    digest = hashlib.sha256(original).hexdigest()
    report = storage.files / f"{digest}.html"
    report.write_bytes(original)
    with storage._connect() as con:
        con.execute("INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (account, digest, "synthetic.html", str(report), len(original), "sber", "2025-01-01"))

    real_write = zipfile.ZipFile.write

    def mutate_original_before_archive_write(archive, filename, arcname=None, *args, **kwargs):
        if arcname == f"files/{report.name}":
            report.write_bytes(b"external modification during backup")
        return real_write(archive, filename, arcname, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", mutate_original_before_archive_write)
    archive_path = tmp_path / "snapshot.zip"
    create_backup(storage, archive_path)
    restored = restore_backup(archive_path, tmp_path / "restored")
    assert (restored.files / report.name).read_bytes() == original


def test_backup_rejects_inconsistent_source_size_and_noncanonical_name(tmp_path: Path):
    storage = Storage(tmp_path / "data")
    account = storage.create_account("sber", "brokerage", "Synthetic")
    content = b"synthetic source"
    digest = hashlib.sha256(content).hexdigest()
    wrong_name = storage.files / "synthetic.html"
    wrong_name.write_bytes(content)
    with storage._connect() as con:
        con.execute("INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (account, digest, "synthetic.html", str(wrong_name),
                                           len(content), "sber", "2025-01-01"))
    with pytest.raises(ValueError, match="inconsistent metadata"):
        create_backup(storage, tmp_path / "wrong-name.zip")
    assert not (tmp_path / "wrong-name.zip").exists()

    canonical = storage.files / f"{digest}.html"
    wrong_name.rename(canonical)
    with storage._connect() as con:
        con.execute("DROP TRIGGER protect_source_file_identity")
        con.execute("UPDATE source_files SET private_path=?,size=?", (str(canonical), len(content) + 1))
    with pytest.raises(ValueError, match="inconsistent metadata"):
        create_backup(storage, tmp_path / "wrong-size.zip")
    assert not (tmp_path / "wrong-size.zip").exists()


def test_backup_rejects_broken_sqlite_references_before_writing_archive(tmp_path: Path):
    storage = Storage(tmp_path / "data")
    with sqlite3.connect(storage.db_path) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("INSERT INTO account_history(account_id,from_date,reason,confirmed_at) VALUES(?,?,?,?)",
                    (999, "2025-01-01", "Synthetic invalid reference", "2025-01-01"))
    archive = tmp_path / "broken.zip"
    with pytest.raises(ValueError, match="broken references"):
        create_backup(storage, archive)
    assert not archive.exists()


def test_restore_rejects_source_size_that_disagrees_with_ledger(tmp_path: Path):
    storage = Storage(tmp_path / "data")
    account = storage.create_account("sber", "brokerage", "Synthetic")
    content = b"synthetic source"
    digest = hashlib.sha256(content).hexdigest()
    report = storage.files / f"{digest}.html"
    report.write_bytes(content)
    with storage._connect() as con:
        con.execute("INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (account, digest, "synthetic.html", str(report),
                                           len(content), "sber", "2025-01-01"))
    original_archive = tmp_path / "valid.zip"
    create_backup(storage, original_archive)
    altered_db = tmp_path / "altered.sqlite3"
    with zipfile.ZipFile(original_archive) as source:
        altered_db.write_bytes(source.read("ledger.sqlite3"))
        manifest = json.loads(source.read("manifest.json"))
    with sqlite3.connect(altered_db) as con:
        con.execute("DROP TRIGGER protect_source_file_identity")
        con.execute("UPDATE source_files SET size=size+1")
    altered_bytes = altered_db.read_bytes()
    manifest["files"]["ledger.sqlite3"] = {"sha256": hashlib.sha256(altered_bytes).hexdigest(),
                                             "size": len(altered_bytes)}
    forged_archive = tmp_path / "forged.zip"
    with zipfile.ZipFile(original_archive) as source, zipfile.ZipFile(forged_archive, "w") as target:
        for name in source.namelist():
            target.writestr(name, json.dumps(manifest) if name == "manifest.json" else
                            altered_bytes if name == "ledger.sqlite3" else source.read(name))
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="metadata does not match"):
        restore_backup(forged_archive, destination)
    assert not destination.exists()
