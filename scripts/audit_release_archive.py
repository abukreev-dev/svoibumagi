"""Check local distribution archives before any release decision.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import tarfile
import zipfile
from pathlib import Path


REQUIRED_SOURCE = {
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "Dockerfile",
    "compose.yaml",
    "compose.local-http.yaml",
    "deploy/compose.ddl.yaml",
    "deploy/nginx.svoibumagi.conf",
    "deploy/nginx-proxy.conf",
    "deploy/backup-ddl.sh",
    "requirements.lock",
    "docs/prd.md",
    "docs/spec.md",
    "docs/backlog.md",
    "docs/sprints.md",
    "docs/release-acceptance.md",
    "docs/deploy-ddl.md",
    "docs/moex-data-terms.md",
    "scripts/audit_release_archive.py",
    "scripts/audit_dependency_licenses.py",
    "scripts/check_manual_controls.py",
    "scripts/build_acceptance_review.py",
    "scripts/audit_report_values.py",
    "scripts/audit_cross_account_reports.py",
    "scripts/audit_alfa_trade_times.py",
    "scripts/audit_source_provenance.py",
    "scripts/check_alfa_aggregate_controls.py",
    "scripts/audit_sber_dividend_tax.py",
    "scripts/audit_sber_cash_meaning.py",
    "scripts/audit_sber_settlement.py",
    "scripts/smoke_web_samples.py",
    "scripts/smoke_web_all_samples.py",
    "tests/test_prices.py",
    "tests/test_release_archive.py",
    "src/svoibumagi/prices.py",
}
FORBIDDEN_PARTS = {"private", "data", "downloads", "__pycache__", ".pytest_cache"}
FORBIDDEN_NAMES = {".env", ".env.local"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".bak", ".pyc", ".pyo"}
TEXT_SUFFIXES = {".conf", ".css", ".html", ".in", ".json", ".lock", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
TEXT_NAMES = {"LICENSE", "METADATA", "PKG-INFO"}
PRIVATE_PATTERNS = {
    "email address": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "Russian phone number": re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*(?:\d[\s()-]*){10}(?!\d)"),
    "possible passport number": re.compile(r"(?<!\d)\d{4}[\s-]\d{6}(?!\d)"),
    "possible full name": re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b"),
    "absolute user path": re.compile(r"/(?:Users/|home/(?!www/))[^/\s]+/"),
}


def members(path: Path) -> tuple[list[str], list[str]]:
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            entries = archive.getmembers()
            return (
                [entry.name for entry in entries if entry.isfile()],
                [f"unexpected archive entry: {entry.name}" for entry in entries if not (entry.isfile() or entry.isdir())],
            )
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            return (
                [entry.filename for entry in entries if not entry.is_dir()],
                [f"unexpected archive entry: {entry.filename}" for entry in entries if stat.S_ISLNK(entry.external_attr >> 16)],
            )
    raise ValueError(f"Unsupported archive: {path}")


def private_signatures(registry_path: Path) -> set[str]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("schema") != 2 or not isinstance(registry.get("reports"), list):
        raise ValueError("Unsupported private registry schema")
    values = set()
    for report in registry["reports"]:
        digest = str(report["sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid private report digest")
        account_number = report.get("account_number")
        if not isinstance(account_number, str) or len(account_number) < 5:
            raise ValueError("Private registry is missing a source account number")
        values.update((digest, digest[:12], str(report["account_key"]),
                       Path(report["path"]).name))
        values.add(account_number)
    return {value for value in values if len(value) >= 5}


def content_errors(path: Path, signatures: set[str] | None = None) -> list[str]:
    """Inspect release members without printing any matched private value."""
    errors = []
    source = path.name.endswith(".tar.gz")
    if source:
        with tarfile.open(path, "r:gz") as archive:
            contents = [(entry.name, archive.extractfile(entry).read()) for entry in archive.getmembers()
                        if entry.isfile()]
    elif path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            contents = [(name, archive.read(name)) for name in archive.namelist() if not name.endswith("/")]
    else:
        raise ValueError(f"Unsupported archive: {path}")
    for name, data in contents:
        body = data.decode("utf-8", errors="ignore")
        if Path(name).name in {"PKG-INFO", "METADATA"} and "License-Expression: GPL-3.0-or-later" not in body.splitlines():
            errors.append(f"missing GPL license expression in {name}")
        if Path(name).name == "LICENSE" and "GNU GENERAL PUBLIC LICENSE\n                       Version 3" not in body:
            errors.append(f"unexpected license text in {name}")
        if Path(name).suffix.lower() == ".py" and not any(
            "SPDX-License-Identifier: GPL-3.0-or-later" in line for line in body.splitlines()[:5]
        ):
            errors.append(f"missing GPL SPDX notice in {name}")
        if Path(name).suffix.lower() in TEXT_SUFFIXES or Path(name).name in TEXT_NAMES:
            for label, pattern in PRIVATE_PATTERNS.items():
                if pattern.search(body):
                    errors.append(f"possible {label} in {name}")
        if signatures and any(value in body for value in signatures):
            errors.append(f"private report identifier in {name}")
    return errors


def audit(path: Path, signatures: set[str] | None = None) -> tuple[int, list[str]]:
    names, errors = members(path)
    source = path.name.endswith(".tar.gz")
    normalized = ["/".join(name.split("/")[1:]) if source else name for name in names]
    for original, name in zip(names, normalized, strict=True):
        if original.startswith("/") or ".." in Path(original).parts:
            errors.append(f"unsafe path: {original}")
        parts = Path(name).parts
        if (
            FORBIDDEN_PARTS.intersection(part.lower() for part in parts)
            or Path(name).name.lower() in FORBIDDEN_NAMES
            or Path(name).suffix.lower() in FORBIDDEN_SUFFIXES
        ):
            errors.append(f"forbidden path: {name}")
    if source:
        errors.extend(f"missing: {name}" for name in sorted(REQUIRED_SOURCE - set(normalized)))
    else:
        for prefix in ("svoibumagi/prices.py", "svoibumagi/templates/home.html",
                       "svoibumagi/templates/landing.html"):
            if prefix not in normalized:
                errors.append(f"missing: {prefix}")
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in normalized):
            errors.append("missing: GPL license")
    errors.extend(content_errors(path, signatures))
    return len(names), errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-registry", type=Path, help="optional ignored report registry for fingerprint checks")
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        signatures = private_signatures(args.private_registry) if args.private_registry else None
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, f"Private registry is unavailable: {type(exc).__name__}\n")
    failed = False
    for path in args.archives:
        count, errors = audit(path, signatures)
        print(f"{path}: {count} files; {'FAIL' if errors else 'OK'}")
        for error in errors:
            print(f"  {error}")
        failed |= bool(errors)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
