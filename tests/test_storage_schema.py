"""SQLite schema upgrade controls. SPDX-License-Identifier: GPL-3.0-or-later"""

import sqlite3
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal as D

import pytest

from svoibumagi.storage import Storage
from svoibumagi.exports import operations_csv
from svoibumagi.reports import Location, Trade
from tests.source_fixture import cash_payload, source_row, synthetic_identity, trade_payload


def test_only_empty_account_can_be_deleted_after_pending_source_is_removed(tmp_path, monkeypatch):
    from svoibumagi.reports import ParsedReport
    from svoibumagi.sber import ADAPTER_VERSION as SBER_ADAPTER_VERSION

    store = Storage(tmp_path / "private")
    empty = store.create_account("sber", "brokerage", "Unused")
    used = store.create_account("sber", "brokerage", "Used")
    assert store.deletable_account_ids() == {empty, used}
    store.add_manual_cash(used, date(2025, 1, 1), "deposit", D("10"), "Synthetic event")
    assert store.deletable_account_ids() == {empty}
    with pytest.raises(ValueError, match="has reports"):
        store.delete_empty_account(used)

    monkeypatch.setattr("svoibumagi.storage.parse_sber", lambda _: ParsedReport(
        "sber", SBER_ADAPTER_VERSION, date(2025, 1, 1), date(2025, 1, 31),
        account_key="a" * 64, account_kind="brokerage", account_number="SYN123"))
    monkeypatch.setattr("svoibumagi.storage._source_rows", lambda *_: [])
    source = tmp_path / "synthetic.html"
    source.write_text("<html>synthetic report</html>")
    attempt, _ = store.stage_file(empty, source)
    assert store.deletable_account_ids() == set()
    with pytest.raises(ValueError, match="has reports"):
        store.delete_empty_account(empty)
    assert store.delete_unconfirmed_attempt(attempt)
    assert store.deletable_account_ids() == {empty}
    store.delete_empty_account(empty)
    assert [row["id"] for row in store.list_accounts()] == [used]
    with pytest.raises(ValueError, match="Unknown account"):
        store.delete_empty_account(empty)


def test_account_currency_and_status_migrate_from_v19(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    db = root / "ledger.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, broker TEXT NOT NULL, "
                    "kind TEXT NOT NULL, name TEXT NOT NULL)")
        con.execute("INSERT INTO accounts(broker,kind,name) VALUES('sber','brokerage','Synthetic')")
        con.execute("PRAGMA user_version=19")
    store = Storage(root)
    assert [(account["name"], account["currency"], account["status"])
            for account in store.list_accounts()] == [("Synthetic", "RUB", "active")]
    with store._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE accounts SET currency='USD' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE accounts SET status='unknown' WHERE id=1")
    backups = list(root.glob("ledger.pre-v19-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 19
        assert con.execute("SELECT name FROM accounts").fetchone()[0] == "Synthetic"
        assert {row[1] for row in con.execute("PRAGMA table_info(accounts)")} == {
            "id", "broker", "kind", "name"}


def test_same_contract_cannot_bind_two_accounts_of_one_type(tmp_path, monkeypatch):
    from svoibumagi.reports import ParsedReport
    from svoibumagi.sber import ADAPTER_VERSION as SBER_ADAPTER_VERSION

    store = Storage(tmp_path / "private")
    first = store.create_account("sber", "brokerage", "First synthetic account")
    second = store.create_account("sber", "brokerage", "Second synthetic account")
    iis = store.create_account("sber", "iis", "Synthetic IIS")
    key = "a" * 64
    kind = ["brokerage"]

    def parse(_path):
        return ParsedReport("sber", SBER_ADAPTER_VERSION, date(2025, 1, 1), date(2025, 1, 31),
                            account_key=key, account_kind=kind[0])

    monkeypatch.setattr("svoibumagi.storage.parse_sber", parse)
    monkeypatch.setattr("svoibumagi.storage._source_rows", lambda *_: [])
    path = tmp_path / "report.html"
    path.write_text("<html>synthetic report</html>")
    attempt, _ = store.stage_file(first, path)
    first_review = store.review_attempt(attempt)
    decisions = {key: {"reason": "synthetic empty report"} for key in first_review["required_decisions"]}
    assert store.confirm_attempt(attempt, decisions) == {"inserted": 0, "observed_duplicates": 0}
    kind[0] = None  # Alfa reports may have an agreement ID without an account-type marker.
    second_attempt, _ = store.stage_file(second, path)
    assert any(item["key"] == "account_identity" for item in
               store.review_attempt(second_attempt)["blocking_conflicts"])
    with pytest.raises(ValueError, match="already bound to another account"):
        store.confirm_attempt(second_attempt)
    assert store.review_attempt(second_attempt)["state"] == "awaiting_review"
    with store._connect() as con:
        with pytest.raises(sqlite3.IntegrityError, match="another account of the same type"):
            con.execute("INSERT INTO account_identities(account_id,identity_hash,confirmed_at) VALUES(?,?,?)",
                        (second, key, "2025-01-01"))
        with pytest.raises(sqlite3.IntegrityError, match="confirmed account identity is immutable"):
            con.execute("UPDATE account_identities SET identity_hash=? WHERE account_id=?", ("c" * 64, first))
    kind[0] = "iis"
    iis_attempt, _ = store.stage_file(iis, path)
    iis_review = store.review_attempt(iis_attempt)
    decisions = {key: {"reason": "synthetic empty report"} for key in iis_review["required_decisions"]}
    assert store.confirm_attempt(iis_attempt, decisions) == {"inserted": 0, "observed_duplicates": 0}


def test_account_number_comes_only_from_committed_reports_and_conflicts_stay_visible(tmp_path, monkeypatch):
    from svoibumagi.reports import ParsedReport
    from svoibumagi.sber import ADAPTER_VERSION as SBER_ADAPTER_VERSION

    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    number = ["SYN123"]

    def parse(_path):
        return ParsedReport("sber", SBER_ADAPTER_VERSION, date(2025, 1, 1), date(2025, 1, 31),
                            account_key="a" * 64, account_kind="brokerage", account_number=number[0])

    monkeypatch.setattr("svoibumagi.storage.parse_sber", parse)
    monkeypatch.setattr("svoibumagi.storage._source_rows", lambda *_: [])
    for index in (1, 2):
        path = tmp_path / f"report-{index}.html"
        path.write_text(f"<html>synthetic report {index}</html>")
        attempt, _ = store.stage_file(account_id, path)
        if index == 1:
            assert store.list_accounts()[0]["account_number"] is None
        review = store.review_attempt(attempt)
        decisions = {key: {"reason": "synthetic empty report"} for key in review["required_decisions"]}
        store.confirm_attempt(attempt, decisions)
        if index == 1:
            assert store.list_accounts()[0]["account_number"] == "SYN123"
            number[0] = "SYN456"  # Simulate contradictory numbering in an older accepted source.
    account = store.list_accounts()[0]
    assert account["account_number"] is None
    assert account["account_number_conflict"] is True
    assert "committed reports disagree on the source account number" in store.financial_metrics()[0]["reasons"]
    from scripts.audit_source_provenance import audit
    assert audit(store.db_path)["conflicting_account_number_ids"] == [account_id]


def test_v17_to_v18_preserves_existing_duplicate_bindings_but_blocks_new_ones(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    first = store.create_account("sber", "brokerage", "First")
    second = store.create_account("sber", "brokerage", "Second")
    third = store.create_account("sber", "brokerage", "Third")
    key = "b" * 64
    with store._connect() as con:
        con.execute("DROP TRIGGER unique_account_identity_kind")
        for account in (first, second):
            con.execute("INSERT INTO account_identities(account_id,identity_hash,confirmed_at) VALUES(?,?,?)",
                        (account, key, "2025-01-01"))
        con.execute("PRAGMA user_version=17")
    migrated = Storage(root)
    with migrated._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT COUNT(*) FROM account_identities WHERE identity_hash=?", (key,)).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="another account of the same type"):
            con.execute("INSERT INTO account_identities(account_id,identity_hash,confirmed_at) VALUES(?,?,?)",
                        (third, key, "2025-01-01"))
    from scripts.audit_source_provenance import audit
    audit_result = audit(migrated.db_path)
    assert audit_result["duplicate_account_binding_ids"] == [[first, second]]
    assert audit_result["status"] == "incomplete"
    assert len(list(root.glob("ledger.pre-v17-*.bak"))) == 1


def test_new_ledger_events_and_observations_require_preserved_source_rows(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        def add_event(row_number):
            return con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "cash", row_number, "cash", None, f"fp-{row_number}",
                 cash_payload(), "2025-01-01"),
            ).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="ledger source row"):
            add_event(1)
        attempt = source_row(con, source, "cash", 1, committed=False)
        with pytest.raises(sqlite3.IntegrityError, match="ledger source row"):
            add_event(1)
        con.execute("UPDATE import_attempts SET state='committed' WHERE id=?", (attempt,))
        event = add_event(1)
        with pytest.raises(sqlite3.IntegrityError, match="observation source row"):
            con.execute(
                "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                (event, source, "cash", 2, "cash"),
            )
        con.execute(
            "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
            (event, source, "cash", 1, "cash"),
        )
        other_source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic-other", "synthetic-other", "/synthetic-other", 1,
                                      "sber", "2025-01-01"),
        ).lastrowid
        pending_observation = source_row(con, other_source, "cash", 1, committed=False)
        with pytest.raises(sqlite3.IntegrityError, match="observation source row"):
            con.execute(
                "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                (event, other_source, "cash", 1, "cash"),
            )
        con.execute("UPDATE import_attempts SET state='committed' WHERE id=?", (pending_observation,))
        con.execute(
            "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
            (event, other_source, "cash", 1, "cash"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="referenced source row"):
            con.execute("DELETE FROM source_rows WHERE attempt_id=?", (attempt,))
        with pytest.raises(sqlite3.IntegrityError, match="referenced source row"):
            con.execute("UPDATE source_rows SET row_number=2 WHERE attempt_id=?", (attempt,))
        assert con.execute("SELECT COUNT(*) FROM source_rows WHERE attempt_id=?", (attempt,)).fetchone()[0] == 1
    assert store.list_events(account)[0]["source_row_verified"] is True


def test_v16_rejects_malformed_new_ledger_payload_and_preserves_v15_rows(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "cash", 1)
        synthetic_identity(con, account)
        def insert(payload):
            return con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "cash", 1, "cash", None, "synthetic", payload, "2025-01-01"),
            ).lastrowid
        for payload in ("{", '{"kind":"deposit","amount":"1"}',
                        '{"kind":"deposit","date":"2025-01-01","amount":1,"currency":"RUB"}'):
            with pytest.raises(sqlite3.IntegrityError, match="ledger payload|cash payload"):
                insert(payload)
        source_row(con, source, "trade", 2, "trade")
        with pytest.raises(sqlite3.IntegrityError, match="trade payload"):
            con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "trade", 2, "trade", "deal-1", "trade-fp",
                 json.dumps({"isin": "RU000A000001", "quantity": 1}), "2025-01-01"),
            )
        assert con.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 0
        con.execute("DROP TRIGGER validate_ledger_payload")
        old_event = insert('{"amount":"1"}')
        con.execute("PRAGMA user_version=15")
    migrated = Storage(root)
    with migrated._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT payload_json FROM ledger_events WHERE id=?", (old_event,)).fetchone()[0] == '{"amount":"1"}'
        with pytest.raises(sqlite3.IntegrityError, match="cash payload"):
            con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "cash", 1, "cash", None, "new", '{"amount":"2"}', "2025-01-01"),
            )
    assert len(list(root.glob("ledger.pre-v15-*.bak"))) == 1


def test_v16_to_v17_preserves_pending_source_legacy_event_but_rejects_new_one(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "cash", 1, committed=False)
        con.execute("DROP TRIGGER validate_ledger_source_row")
        con.execute("DROP TRIGGER validate_observation_source_row")
        old_event = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "cash", 1, "cash", None, "old", cash_payload(), "2025-01-01"),
        ).lastrowid
        con.execute("PRAGMA user_version=16")
    migrated = Storage(root)
    with migrated._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT COUNT(*) FROM ledger_events WHERE id=?", (old_event,)).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="ledger source row"):
            con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "cash", 1, "cash", None, "new", cash_payload(), "2025-01-01"),
            )
    assert migrated.list_events(account)[0]["source_row_verified"] is False
    assert len(list(root.glob("ledger.pre-v16-*.bak"))) == 1


def test_v16_rejects_malformed_manual_event_payload(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        with pytest.raises(sqlite3.IntegrityError, match="manual cash payload"):
            con.execute("INSERT INTO manual_events(account_id,event_type,payload_json,reason,created_at) VALUES(?,?,?,?,?)",
                        (account, "cash", '{"amount":"1"}', "synthetic", "2025-01-01"))
        assert con.execute("SELECT COUNT(*) FROM manual_events").fetchone()[0] == 0
    store.add_manual_cash(account, date(2025, 1, 1), "deposit", D("1"), "synthetic")


def test_v14_to_v15_preserves_legacy_event_without_stored_row(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "legacy", "legacy", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        con.execute("DROP TRIGGER validate_ledger_source_row")
        event = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "old", 1, "cash", None, "legacy", cash_payload(), "2025-01-01"),
        ).lastrowid
        con.execute("PRAGMA user_version=14")
    Storage(root)
    assert store.list_events(account)[0]["source_row_verified"] is False
    with store._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT source_id FROM ledger_events WHERE id=?", (event,)).fetchone()[0] == source
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
        with pytest.raises(sqlite3.IntegrityError, match="ledger source row"):
            con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account, source, "old", 2, "cash", None, "new", cash_payload("2", day="2025-01-02"), "2025-01-02"),
            )
    backups = list(root.glob("ledger.pre-v14-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 14
        assert con.execute("SELECT id FROM ledger_events WHERE id=?", (event,)).fetchone()[0] == event


def test_alfa_trade_time_comes_from_preserved_row_without_changing_event_identity(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("alfa", "brokerage", "Synthetic")
    trade = Trade(Location("Завершенные сделки", 2), "deal-1", date(2025, 1, 1),
                  date(2025, 1, 1), "RU000A000001", "Synthetic", D("1"), D("100"),
                  D("100"), D("0"), "RUB", execution_time="07:44:39",
                  execution_time_required=True)
    preview = {"trades": [json.loads(json.dumps(asdict(trade), default=str))],
               "cash_movements": [], "summary_trades": []}
    normalized = Storage._event_items(preview)[0]
    assert "execution_time" not in json.loads(normalized["payload"])
    assert "execution_time_required" not in json.loads(normalized["payload"])
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "alfa", "2025-01-01"),
        ).lastrowid
        attempt = source_row(con, source, trade.location.sheet, trade.location.row, "trade", committed=False)
        raw = [""] * 8
        raw[7] = "01.01.2025\n 07:44:39"
        con.execute("UPDATE source_rows SET raw_json=? WHERE attempt_id=?", (json.dumps(raw), attempt))
        con.execute("UPDATE import_attempts SET state='committed' WHERE id=?", (attempt,))
        con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, trade.location.sheet, trade.location.row, "trade", "deal-1",
             normalized["fingerprint"], normalized["payload"], "2025-01-01"),
        )
    event = store.list_events(account)[0]
    assert event["source_row_verified"] is True
    assert event["execution_time"] == "07:44:39"
    assert event["execution_time_required"] is True
    assert "07:44:39" in operations_csv([event]).decode("utf-8-sig")


def test_v13_to_v14_preserves_sources_and_locks_provenance(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "cash", 1)
        synthetic_identity(con, account)
        event = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (account, source, "cash", 1, "cash", None, "synthetic",
                                          cash_payload(), "2025-01-01"),
        ).lastrowid
        observation = con.execute(
            "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
            (event, source, "cash", 1, "cash"),
        ).lastrowid
        for name in ("validate_source_file", "protect_source_file_identity", "protect_source_file_delete",
                     "protect_account_identity", "protect_import_attempt_fields",
                     "validate_import_attempt_state", "protect_committed_attempt_delete",
                     "protect_source_row_update", "protect_source_row_delete",
                     "validate_instrument_event", "protect_instrument_event_update", "protect_instrument_event_delete",
                     "protect_event_observation_update", "protect_event_observation_delete",
                     "protect_audit_decision_update", "protect_audit_decision_delete"):
            con.execute(f"DROP TRIGGER {name}")
        con.execute("PRAGMA user_version=13")
    upgraded = Storage(root)
    with upgraded._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT source_id FROM ledger_events WHERE id=?", (event,)).fetchone()[0] == source
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            con.execute("UPDATE source_files SET account_id=? WHERE id=?", (account + 1, source))
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            con.execute("UPDATE source_files SET detected_broker='alfa' WHERE id=?", (source,))
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            con.execute("UPDATE source_files SET original_name='changed' WHERE id=?", (source,))
        with pytest.raises(sqlite3.IntegrityError, match="committed source files"):
            con.execute("DELETE FROM source_files WHERE id=?", (source,))
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            con.execute("UPDATE event_observations SET row_number=2 WHERE id=?", (observation,))
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            con.execute("DELETE FROM event_observations WHERE id=?", (observation,))
        with pytest.raises(sqlite3.IntegrityError, match="broker and kind"):
            con.execute("UPDATE accounts SET broker='alfa' WHERE id=?", (account,))
        attempt = con.execute(
            "INSERT INTO import_attempts(source_id,state,adapter_version,period_start,period_end,preview_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (source, "awaiting_review", "synthetic", "2025-01-01", "2025-01-31",
                                   "{}", "2025-01-31"),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="source and preview"):
            con.execute("UPDATE import_attempts SET period_end='2025-02-01' WHERE id=?", (attempt,))
        con.execute("UPDATE import_attempts SET state='rejected' WHERE id=?", (attempt,))
        with pytest.raises(sqlite3.IntegrityError, match="state transition"):
            con.execute("UPDATE import_attempts SET state='awaiting_review' WHERE id=?", (attempt,))
        decision = con.execute("INSERT INTO audit_decisions(attempt_id,action,reason,created_at) VALUES(?,?,?,?)",
                               (attempt, "reject", "synthetic", "2025-01-31")).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="audit decisions"):
            con.execute("UPDATE audit_decisions SET reason='changed' WHERE id=?", (decision,))
        instrument = con.execute("INSERT INTO instruments(isin,display_name,created_at) VALUES(?,?,?)",
                                 ("RU000A000001", "Synthetic", "2025-01-01")).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="instrument link"):
            con.execute("INSERT INTO instrument_events(event_id,instrument_id) VALUES(?,?)", (event, instrument))
        con.execute("UPDATE accounts SET name='Renamed' WHERE id=?", (account,))
        con.execute("UPDATE source_files SET private_path='/restored/synthetic' WHERE id=?", (source,))
    copies = list(root.glob("ledger.pre-v13-*.bak"))
    assert len(copies) == 1
    with sqlite3.connect(copies[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 13
        assert con.execute("SELECT private_path FROM source_files WHERE id=?", (source,)).fetchone()[0] == "/synthetic"
    Storage(root)
    assert len(list(root.glob("ledger.pre-v13-*.bak"))) == 1


def test_v12_to_v14_adds_immutable_security_transfers_with_pre_migration_copy(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    store.add_manual_cash(account, date(2025, 1, 1), "deposit", D("10"), "kept")
    with store._connect() as con:
        con.execute("DROP TRIGGER protect_security_transfer_update")
        con.execute("DROP TRIGGER protect_security_transfer_delete")
        con.execute("DROP TABLE security_transfers")
        con.execute("PRAGMA user_version=12")
    migrated = Storage(root)
    assert migrated.list_events(account)[0]["payload"]["amount"] == "10"
    with migrated._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
        assert con.execute("SELECT name FROM sqlite_master WHERE name='security_transfers'").fetchone()
    copies = list(root.glob("ledger.pre-v12-*.bak"))
    assert len(copies) == 1
    with sqlite3.connect(copies[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 12
        assert con.execute("SELECT name FROM sqlite_master WHERE name='security_transfers'").fetchone() is None
    Storage(root)
    assert len(list(root.glob("ledger.pre-v12-*.bak"))) == 1


def test_v11_manual_events_migrate_to_v14_with_backup_and_principal_link_guard(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    cash_id = store.add_manual_cash(account, date(2025, 2, 1), "deposit", D("10"), "verified")
    with store._connect() as con:
        con.execute("DROP INDEX manual_principal_cash_source")
        con.execute("DROP INDEX manual_principal_instrument_date")
        con.execute("PRAGMA user_version=11")
    reopened = Storage(root)
    assert reopened.list_events(account)[0]["id"] == f"M{cash_id}"
    with reopened._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT name FROM sqlite_master WHERE name='manual_principal_cash_source'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='manual_principal_instrument_date'").fetchone()
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
    backups = list(root.glob("ledger.pre-v11-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 11
        assert con.execute("SELECT COUNT(*) FROM manual_events").fetchone()[0] == 1
    Storage(root)
    assert len(list(root.glob("ledger.pre-v11-*.bak"))) == 1


def test_existing_ledger_is_preserved_when_new_tables_are_added(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    with sqlite3.connect(store.db_path) as con:
        con.execute("DROP TRIGGER link_trade_instrument")
        con.execute("DROP TABLE instrument_events")
        con.execute("DROP TABLE instruments")
        con.execute("DROP TABLE cash_classifications")
        con.execute("DROP TABLE account_history")
        con.execute("PRAGMA user_version=1")
    upgraded = Storage(root)
    assert upgraded.list_accounts()[0]["id"] == account_id
    with sqlite3.connect(upgraded.db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT name FROM sqlite_master WHERE name='cash_classifications'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='instruments'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='source_rows'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='account_identities'").fetchone()
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
    backup_paths = list(root.glob("ledger.pre-v1-*.bak"))
    assert len(backup_paths) == 1
    with sqlite3.connect(backup_paths[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert con.execute("SELECT name FROM accounts WHERE id=?", (account_id,)).fetchone()[0] == "Synthetic"


def test_newer_database_is_not_downgraded(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    with sqlite3.connect(store.db_path) as con:
        con.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="newer"):
        Storage(root)
    with sqlite3.connect(store.db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 99


def test_instrument_links_are_backfilled_from_existing_trades(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    accounts = [store.create_account("sber", "brokerage", "A"), store.create_account("alfa", "iis", "B")]
    payload = trade_payload()
    with store._connect() as con:
        for account_id in accounts:
            broker = "sber" if account_id == accounts[0] else "alfa"
            source = con.execute(
                "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (account_id, f"synthetic-{account_id}", "synthetic", "/synthetic", 1, broker, "2025-01-01")
            ).lastrowid
            source_row(con, source, "synthetic", 1, "trade")
            synthetic_identity(con, account_id)
            con.execute("INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (account_id, source, "synthetic", 1, "trade", f"deal-{account_id}", f"fp-{account_id}", payload, "2025-01-01"))
        assert con.execute("SELECT COUNT(*) FROM instrument_events").fetchone()[0] == 2
        con.execute("DROP TRIGGER link_trade_instrument")
        con.execute("DROP TABLE instrument_events")
        con.execute("DROP TABLE instruments")
        con.execute("PRAGMA user_version=3")
    upgraded = Storage(root)
    instruments = upgraded.list_instruments()
    assert len(instruments) == 1 and instruments[0]["isin"] == "RU000A000001"
    assert {event["instrument_id"] for event in upgraded.list_events()} == {instruments[0]["id"]}
    assert {event["account_id"] for event in upgraded.list_events()} == set(accounts)
    with upgraded._connect() as con:
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None


def test_trade_without_instrument_is_rejected_by_database(tmp_path):
    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account_id, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01")
        ).lastrowid
        source_row(con, source, "synthetic", 1, "trade")
        with pytest.raises(sqlite3.IntegrityError, match="instrument"):
            con.execute("INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (account_id, source, "synthetic", 1, "trade", "deal-1", "fp", trade_payload("", "Missing ISIN"), "2025-01-01"))


def test_committed_ledger_event_cannot_be_edited_or_deleted(tmp_path):
    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account_id, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01")
        ).lastrowid
        source_row(con, source, "synthetic", 1)
        event_id = con.execute("INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?)",
                               (account_id, source, "synthetic", 1, "cash", None, "fp", cash_payload(), "2025-01-01")).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("UPDATE ledger_events SET payload_json='{}' WHERE id=?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("DELETE FROM ledger_events WHERE id=?", (event_id,))
        assert con.execute("SELECT COUNT(*) FROM ledger_events WHERE id=?", (event_id,)).fetchone()[0] == 1


def test_ledger_sources_and_observations_cannot_cross_accounts(tmp_path):
    store = Storage(tmp_path / "private")
    left = store.create_account("sber", "brokerage", "Left")
    right = store.create_account("sber", "brokerage", "Right")
    other_broker = store.create_account("alfa", "brokerage", "Alfa")
    with store._connect() as con:
        def source(account_id, broker, tag):
            source_id = con.execute(
                "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (account_id, tag, tag, "/synthetic", 1, broker, "2025-01-01"),
            ).lastrowid
            source_row(con, source_id, "synthetic", 1)
            return source_id

        left_source = source(left, "sber", "left")
        right_source = source(right, "sber", "right")
        with pytest.raises(sqlite3.IntegrityError, match="source file account"):
            source(other_broker, "sber", "broker-mismatch")
        source(other_broker, "alfa", "other-broker")

        def insert_event(account_id, source_id):
            return con.execute(
                "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account_id, source_id, "synthetic", 1, "cash", None, "fp", cash_payload(), "2025-01-01"),
            ).lastrowid

        with pytest.raises(sqlite3.IntegrityError, match="ledger source account"):
            insert_event(left, right_source)
        with pytest.raises(sqlite3.IntegrityError, match="ledger source account"):
            insert_event(other_broker, left_source)
        event_id = insert_event(left, left_source)
        with pytest.raises(sqlite3.IntegrityError, match="observation"):
            con.execute(
                "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                (event_id, right_source, "synthetic", 1, "cash"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="observation"):
            con.execute(
                "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                (event_id, left_source, "synthetic", 1, "trade"),
            )
        con.execute(
            "INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
            (event_id, left_source, "synthetic", 1, "cash"),
        )


def test_v10_observations_migrate_and_allow_two_cash_components_on_one_row(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("alfa", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "alfa", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "cash", 7)
        synthetic_identity(con, account)
        gross = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "cash", 7, "cash", None, "gross", cash_payload("100", "dividend"), "2025-01-01"),
        ).lastrowid
        con.execute("INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                    (gross, source, "cash", 7, "cash"))
        con.execute("DROP TRIGGER validate_event_observation")
        con.execute("DROP TRIGGER protect_source_file_delete")
        con.execute("DROP TRIGGER protect_referenced_source_row_update")
        con.execute("DROP TRIGGER protect_last_referenced_source_row_delete")
        con.execute("DROP TRIGGER validate_observation_source_row")
        con.execute("DROP INDEX event_observation_per_file")
        con.execute("ALTER TABLE event_observations RENAME TO event_observations_v11")
        con.execute("CREATE TABLE event_observations (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES ledger_events(id), "
                    "source_id INTEGER NOT NULL REFERENCES source_files(id), sheet TEXT NOT NULL, row_number INTEGER NOT NULL, "
                    "event_type TEXT NOT NULL, UNIQUE(source_id,sheet,row_number,event_type))")
        con.execute("INSERT INTO event_observations(id,event_id,source_id,sheet,row_number,event_type) "
                    "SELECT id,event_id,source_id,sheet,row_number,event_type FROM event_observations_v11")
        con.execute("DROP TABLE event_observations_v11")
        con.execute("PRAGMA user_version=10")
    upgraded = Storage(root)
    with upgraded._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT event_id,component FROM event_observations").fetchall() == [(gross, "")]
        tax = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "cash", 7, "cash", None, "tax", cash_payload("-13", "tax_withheld", component="dividend_tax"), "2025-01-01"),
        ).lastrowid
        con.execute("INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type,component) VALUES(?,?,?,?,?,?)",
                    (tax, source, "cash", 7, "cash", "dividend_tax"))
        assert con.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0] == 2
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
    assert len(list(root.glob("ledger.pre-v10-*.bak"))) == 1


def test_migration_rejects_existing_cross_account_event(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    left = store.create_account("sber", "brokerage", "Left")
    right = store.create_account("sber", "brokerage", "Right")
    with store._connect() as con:
        source_id = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (right, "right", "right", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        source_row(con, source_id, "synthetic", 1)
        con.execute("DROP TRIGGER validate_ledger_source")
        con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (left, source_id, "synthetic", 1, "cash", None, "fp", cash_payload(), "2025-01-01"),
        )
        con.execute("PRAGMA user_version=6")
    with pytest.raises(ValueError, match="another account"):
        Storage(root)
    with store._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6


def test_v6_migration_preserves_valid_events_and_installs_source_checks(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "synthetic", 1)
        synthetic_identity(con, account)
        event = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "synthetic", 1, "cash", None, "fp", cash_payload(), "2025-01-01"),
        ).lastrowid
        con.execute("DROP TRIGGER validate_ledger_source")
        con.execute("DROP TRIGGER validate_event_observation")
        con.execute("PRAGMA user_version=6")
    upgraded = Storage(root)
    with upgraded._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT source_id FROM ledger_events WHERE id=?", (event,)).fetchone()[0] == source
        assert con.execute("SELECT name FROM sqlite_master WHERE name='validate_ledger_source'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='validate_event_observation'").fetchone()
    backups = list(root.glob("ledger.pre-v6-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6
        assert con.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 1


def test_v9_source_rows_migrate_to_cash_flow_subtotals_without_losing_data(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("alfa", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "alfa", "2025-01-01"),
        ).lastrowid
        attempt = con.execute(
            "INSERT INTO import_attempts(source_id,state,adapter_version,period_start,period_end,preview_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (source, "awaiting_review", "synthetic", "2025-01-01", "2025-01-31", "{}", "2025-01-31"),
        ).lastrowid
        con.execute("DROP TABLE source_rows")
        con.execute("CREATE TABLE source_rows (id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL REFERENCES import_attempts(id), "
                    "sheet TEXT NOT NULL, row_number INTEGER NOT NULL CHECK (row_number>0), raw_json TEXT NOT NULL, "
                    "classification TEXT NOT NULL CHECK (classification IN "
                    "('trade','cash_movement','position_snapshot','cash_snapshot','unparsed','excluded')), "
                    "warning TEXT NOT NULL DEFAULT '')")
        con.execute("INSERT INTO source_rows(attempt_id,sheet,row_number,raw_json,classification,warning) "
                    "VALUES(?,?,?,?,?,?)", (attempt, "synthetic", 7, '["kept"]', "cash_movement", ""))
        con.execute("PRAGMA user_version=9")
    upgraded = Storage(root)
    with upgraded._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("SELECT raw_json FROM source_rows WHERE attempt_id=?", (attempt,)).fetchone()[0] == '["kept"]'
        con.execute("INSERT INTO source_rows(attempt_id,sheet,row_number,raw_json,classification,warning) "
                    "VALUES(?,?,?,?,?,?)", (attempt, "synthetic", 8, '["subtotal"]', "cash_flow_subtotal", ""))
        assert con.execute("PRAGMA foreign_key_check").fetchone() is None
    assert len(list(root.glob("ledger.pre-v9-*.bak"))) == 1
