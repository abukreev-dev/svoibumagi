"""Read-only audit of committed sources and duplicate account bindings.

Only numeric identifiers are printed; no report content or personal paths.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def audit(db_path: Path) -> dict:
    if not db_path.is_file():
        raise ValueError("SQLite database does not exist")
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.execute("PRAGMA query_only=ON")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version not in {18, 19, 20}:
            raise ValueError(f"Expected schema v18, v19 or v20, found v{version}")
        con.execute("BEGIN")
        events = con.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
        observations = con.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0]
        missing_events = [row[0] for row in con.execute("""
            SELECT e.id FROM ledger_events e WHERE NOT EXISTS (
                SELECT 1 FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id
                WHERE a.source_id=e.source_id AND a.state='committed'
                  AND r.sheet=e.sheet AND r.row_number=e.row_number
                  AND ((e.event_type='trade' AND r.classification IN ('trade','unparsed'))
                    OR (e.event_type='cash' AND r.classification='cash_movement'))
            ) ORDER BY e.id
        """)]
        missing_observations = [row[0] for row in con.execute("""
            SELECT o.id FROM event_observations o WHERE NOT EXISTS (
                SELECT 1 FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id
                WHERE a.source_id=o.source_id AND a.state='committed'
                  AND r.sheet=o.sheet AND r.row_number=o.row_number
                  AND ((o.event_type='trade' AND r.classification IN ('trade','unparsed'))
                    OR (o.event_type='cash' AND r.classification='cash_movement'))
            ) ORDER BY o.id
        """)]
        bindings: dict[tuple[str, str, str], list[int]] = {}
        for account_id, broker, kind, identity_hash in con.execute(
            "SELECT a.id,a.broker,a.kind,i.identity_hash FROM account_identities i "
            "JOIN accounts a ON a.id=i.account_id ORDER BY a.id"
        ):
            bindings.setdefault((broker, kind, identity_hash), []).append(account_id)
        duplicate_bindings = [account_ids for account_ids in bindings.values() if len(account_ids) > 1]
        conflicting_account_numbers = [row[0] for row in con.execute("""
            SELECT s.account_id FROM import_attempts a
            JOIN source_files s ON s.id=a.source_id
            WHERE a.state='committed'
            GROUP BY s.account_id
            HAVING COUNT(DISTINCT json_extract(a.preview_json,'$.account_number')) > 1
            ORDER BY s.account_id
        """)]
        sources = con.execute("""
            SELECT s.id,s.private_path,s.size,s.sha256 FROM source_files s
            WHERE EXISTS(SELECT 1 FROM import_attempts a WHERE a.source_id=s.id AND a.state='committed')
               OR EXISTS(SELECT 1 FROM ledger_events e WHERE e.source_id=s.id)
               OR EXISTS(SELECT 1 FROM event_observations o WHERE o.source_id=s.id)
            ORDER BY s.id
        """).fetchall()
        con.execute("ROLLBACK")
    files_root = (db_path.resolve().parent / "files").resolve()
    missing_files: list[int] = []
    changed_files: list[int] = []
    unsafe_files: list[int] = []
    for source_id, private_path, expected_size, expected_sha in sources:
        path = Path(private_path).resolve()
        if not path.is_relative_to(files_root):
            unsafe_files.append(source_id)
            continue
        if not path.is_file():
            missing_files.append(source_id)
            continue
        try:
            before = path.stat().st_size
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat().st_size
        except OSError:
            changed_files.append(source_id)
            continue
        if before != expected_size or after != expected_size or digest != expected_sha:
            changed_files.append(source_id)
    return {"schema_version": version, "ledger_events": events, "event_observations": observations,
            "source_files_checked": len(sources),
            "missing_event_row_ids": missing_events,
            "missing_observation_row_ids": missing_observations,
            "missing_source_file_ids": missing_files, "changed_source_file_ids": changed_files,
            "unsafe_source_file_ids": unsafe_files, "duplicate_account_binding_ids": duplicate_bindings,
            "conflicting_account_number_ids": conflicting_account_numbers,
            "status": "matched" if not any((missing_events, missing_observations, missing_files,
                                              changed_files, unsafe_files, duplicate_bindings,
                                              conflicting_account_numbers)) else "incomplete"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="path to existing private/ledger.sqlite3")
    args = parser.parse_args()
    try:
        result = audit(args.database)
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"Provenance audit unavailable: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
