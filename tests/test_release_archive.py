"""Release archive content inspection. SPDX-License-Identifier: GPL-3.0-or-later"""

from io import BytesIO
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts.audit_release_archive import content_errors, private_signatures


def test_archive_audit_reports_private_identifiers_without_printing_them(tmp_path: Path):
    digest = "a" * 64
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema": 2, "reports": [{
        "sha256": digest, "account_key": "b" * 64, "account_number": "SYN1234",
        "path": "/synthetic/broker-report.html"}]}))
    signatures = private_signatures(registry)
    email = "synthetic" + chr(64) + "example.test"
    body = f"Closed source {digest[:12]} and {email}".encode()

    source = tmp_path / "synthetic.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        entry = tarfile.TarInfo("synthetic/README.md")
        entry.size = len(body)
        archive.addfile(entry, BytesIO(body))
        license_entry = tarfile.TarInfo("synthetic/LICENSE")
        license_entry.size = len(body)
        archive.addfile(license_entry, BytesIO(body))
    errors = content_errors(source, signatures)
    assert any("private report identifier" in error for error in errors)
    assert any("email address" in error for error in errors)
    assert any("private report identifier in synthetic/LICENSE" == error for error in errors)
    assert all(digest[:12] not in error and email not in error for error in errors)

    account_body = b"Private account SYN1234 in a public document"
    with tarfile.open(source, "w:gz") as archive:
        entry = tarfile.TarInfo("synthetic/README.md")
        entry.size = len(account_body)
        archive.addfile(entry, BytesIO(account_body))
    assert "private report identifier in synthetic/README.md" in content_errors(source, signatures)

    registry.write_text(json.dumps({"schema": 2, "reports": [{
        "sha256": digest, "account_key": "b" * 64,
        "path": "/synthetic/broker-report.html"}]}))
    with pytest.raises(ValueError, match="missing a source account number"):
        private_signatures(registry)

    wheel = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("svoibumagi/README.md", body)
        archive.writestr("svoibumagi-0.1.0.dist-info/METADATA", body)
        archive.writestr("svoibumagi/unlicensed.py", b"value = 1\n")
    assert any("private report identifier" in error for error in content_errors(wheel, signatures))
    assert "possible email address in svoibumagi-0.1.0.dist-info/METADATA" in content_errors(wheel, set())
    assert "missing GPL license expression in svoibumagi-0.1.0.dist-info/METADATA" in content_errors(wheel, set())
    assert "missing GPL SPDX notice in svoibumagi/unlicensed.py" in content_errors(wheel, set())


def test_archive_audit_allows_public_deploy_path_but_rejects_home_directory(tmp_path: Path):
    source = tmp_path / "synthetic.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name, body in {
            "synthetic/deploy/nginx.conf": b"root /home/www/svoibumagi.bukreev.pro/acme;",
            "synthetic/deploy/script.sh": b"cd /home/" + b"privateuser/secret-project",
        }.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, BytesIO(body))
    errors = content_errors(source)
    assert not any("nginx.conf" in error for error in errors)
    assert "possible absolute user path in synthetic/deploy/script.sh" in errors
