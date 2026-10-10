"""Read-only provenance audit controls. SPDX-License-Identifier: GPL-3.0-or-later"""

import hashlib

from scripts.audit_source_provenance import audit
from svoibumagi.storage import Storage
from tests.source_fixture import source_row


def test_audit_finds_legacy_event_and_observation_without_source_rows(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    report = store.files / "synthetic.html"
    content = b"synthetic-report"
    report.write_bytes(content)
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (account, hashlib.sha256(content).hexdigest(), "synthetic", str(report), len(content), "sber", "2025-01-01"),
        ).lastrowid
        committed_attempt = source_row(con, source, "cash", 1, committed=False)
        source_row(con, source, "cash", 3, committed=False)  # Staged row is not committed provenance.
        con.execute("UPDATE import_attempts SET state='committed' WHERE id=?", (committed_attempt,))

        def event(row_number):
            event_id = con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "cash", row_number, "cash", None, f"fp-{row_number}",
                 '{"kind":"deposit","date":"2025-01-01","amount":"1","currency":"RUB"}', "2025-01-01"),
            ).lastrowid
            observation_id = con.execute(
                "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                (event_id, source, "cash", row_number, "cash"),
            ).lastrowid
            return event_id, observation_id

        valid_event, valid_observation = event(1)
        con.execute("DROP TRIGGER validate_ledger_source_row")
        con.execute("DROP TRIGGER validate_observation_source_row")
        missing_event, missing_observation = event(2)
        staged_event, staged_observation = event(3)

    result = audit(store.db_path)
    assert result == {"schema_version": 20, "ledger_events": 3, "event_observations": 3,
                      "source_files_checked": 1,
                      "missing_event_row_ids": [missing_event, staged_event],
                      "missing_observation_row_ids": [missing_observation, staged_observation],
                      "missing_source_file_ids": [], "changed_source_file_ids": [],
                      "unsafe_source_file_ids": [], "duplicate_account_binding_ids": [],
                      "conflicting_account_number_ids": [],
                      "status": "incomplete"}
    assert valid_event not in result["missing_event_row_ids"]
    assert valid_observation not in result["missing_observation_row_ids"]
    report.write_bytes(b"altered-report")
    assert audit(store.db_path)["changed_source_file_ids"] == [source]
    report.unlink()
    assert audit(store.db_path)["missing_source_file_ids"] == [source]
    assert not (tmp_path / "absent.sqlite3").exists()
