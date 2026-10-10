"""Synthetic manual-correction provenance controls. SPDX-License-Identifier: GPL-3.0-or-later"""

import csv
from datetime import date
from decimal import Decimal as D
from io import StringIO
import sqlite3

import pytest
from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password
from svoibumagi.backup import create_backup, restore_backup
from svoibumagi.exports import operations_csv
from svoibumagi.metrics import evaluate_account
from svoibumagi.storage import Storage, _canonical_decimal
from tests.source_fixture import source_row
from svoibumagi.web import create_app


def test_manual_events_have_immutable_reason_and_survive_backup(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    cash_id = store.add_manual_cash(account, date(2025, 1, 2), "deposit", D("100"), "verified bank transfer")
    trade_id = store.add_manual_trade(account, date(2025, 1, 3), date(2025, 1, 4), "RU000A000001",
                                      "Synthetic", D("1"), D("95"), D("95"), D("1"), "verified contract note")
    assert cash_id == 1 and trade_id == 2
    events = store.list_events(account)
    assert {item["id"] for item in events} == {"M1", "M2"}
    assert all(item["source_type"] == "manual" and item["source_reason"] for item in events)
    assert next(item for item in events if item["id"] == "M2")["instrument_id"] is not None
    with pytest.raises(ValueError, match="already exists"):
        store.add_manual_cash(account, date(2025, 1, 2), "deposit", D("100"), "verified bank transfer")
    with pytest.raises(ValueError, match="already exists"):
        store.add_manual_cash(account, date(2025, 1, 2), "deposit", D("100.00"), "verified bank transfer")
    with pytest.raises(ValueError, match="already exists"):
        store.add_manual_trade(account, date(2025, 1, 3), date(2025, 1, 4), "RU000A000001",
                               "Renamed synthetic", D("1.0"), D("95.00"), D("95.0"), D("1.00"),
                               "verified contract note")
    assert len(store.list_events(account)) == 2
    with pytest.raises(ValueError, match="conflicts"):
        store.add_manual_cash(account, date(2025, 1, 2), "withdrawal", D("100"), "wrong sign")
    with pytest.raises(ValueError, match="requires a reason"):
        store.add_manual_cash(account, date(2025, 1, 2), "deposit", D("1"), " ")
    with pytest.raises(ValueError, match="Invalid manual"):
        store.add_manual_cash(account, date(2025, 1, 2), "deposit", D("NaN"), "invalid number")
    with pytest.raises(ValueError, match="does not match"):
        store.add_manual_trade(account, date(2025, 1, 3), date(2025, 1, 4), "RU000A000001",
                               "Synthetic", D("1"), D("95"), D("96"), D("1"), "mismatched amount")
    with store._connect() as con:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("UPDATE manual_events SET reason='changed' WHERE id=?", (cash_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("DELETE FROM manual_events WHERE id=?", (trade_id,))
    rows = list(csv.reader(StringIO(operations_csv(events).decode("utf-8-sig"))))
    assert {row[rows[0].index("source_type")] for row in rows[1:]} == {"manual"}
    assert {row[rows[0].index("manual_reason")] for row in rows[1:]} == {
        "verified bank transfer", "verified contract note"}
    backup = tmp_path / "backup.zip"
    create_backup(store, backup)
    restored = restore_backup(backup, tmp_path / "restored")
    assert restored.list_events(account) == events


def test_decimal_event_key_keeps_all_digits_without_context_rounding():
    large = "123456789012345678901234567890.000"
    assert _canonical_decimal(large) == "123456789012345678901234567890"
    assert _canonical_decimal("1E+3") == _canonical_decimal("1000.00") == "1000"


def test_manual_event_outside_report_dates_blocks_verified_metrics():
    account = {"id": 1, "broker": "sber", "kind": "brokerage", "name": "Synthetic"}
    reports = [{"attempt_id": 1, "period_start": "2025-01-01", "period_end": "2025-01-31",
                "unparsed": [], "checks": [], "positions": [],
                "cash_snapshots": [{"start_amount": "0", "end_amount": "0", "currency": "RUB"}]}]
    events = [{"id": "M1", "event_type": "cash", "source_type": "manual", "sheet": "manual", "row_number": None,
               "payload": {"kind": "deposit", "date": "2025-02-01", "amount": "1", "currency": "RUB"}}]
    result = evaluate_account(account, events, reports, {}, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert "manual correction is outside committed report coverage" in result["reasons"]


def test_manual_principal_actions_link_cash_and_replay_cost_basis(tmp_path):
    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic bond")
    other = store.create_account("alfa", "brokerage", "Other")
    day = date(2025, 1, 1)
    store.add_manual_trade(account_id, day, day, "RU000A000001", "Synthetic bond",
                           D("10"), D("95"), D("950"), D("0"), "verified purchase")
    store.add_manual_cash(account_id, day, "deposit", D("950"), "verified funding")
    store.add_manual_cash(account_id, day, "trade_cash", D("-950"), "verified settlement")
    first_cash = store.add_manual_cash(account_id, date(2025, 2, 1), "principal_return", D("200"),
                                       "verified partial principal")
    with pytest.raises(ValueError, match="same account"):
        store.add_manual_principal_action(other, date(2025, 2, 1), "RU000A000001", "Synthetic bond",
                                          D("0.2"), f"M{first_cash}", "wrong account")
    with pytest.raises(ValueError, match="same date"):
        store.add_manual_principal_action(account_id, date(2025, 2, 2), "RU000A000001", "Synthetic bond",
                                          D("0.2"), f"M{first_cash}", "wrong date")
    with pytest.raises(ValueError, match="fraction"):
        store.add_manual_principal_action(account_id, date(2025, 2, 1), "RU000A000001", "Synthetic bond",
                                          D("1.2"), f"M{first_cash}", "wrong fraction")
    pending = store.list_events(account_id)
    report = [{"attempt_id": 1, "period_start": "2025-01-01", "period_end": "2026-01-01",
               "unparsed": [], "checks": [], "positions": [],
               "cash_snapshots": [{"start_amount": "0", "end_amount": "1000", "currency": "RUB"}]}]
    account = next(item for item in store.list_accounts() if item["id"] == account_id)
    incomplete = evaluate_account(account, pending, report, {}, day)
    assert "principal return has no matching amortization or redemption action" in incomplete["reasons"]
    first_action = store.add_manual_principal_action(account_id, date(2025, 2, 1), "RU000A000001",
                                                     "Synthetic bond", D("0.2"), f"M{first_cash}",
                                                     "verified nominal schedule")
    with pytest.raises(ValueError, match="already exists"):
        store.add_manual_principal_action(account_id, date(2025, 2, 1), "RU000A000001",
                                          "Synthetic bond", D("0.2"), f"M{first_cash}", "repeat")
    final_cash = store.add_manual_cash(account_id, date(2025, 6, 1), "principal_return", D("800"),
                                       "verified redemption payment")
    final_action = store.add_manual_principal_action(account_id, date(2025, 6, 1), "RU000A000001",
                                                     "Synthetic bond", D("1"), f"M{final_cash}",
                                                     "verified full redemption")
    events = store.list_events(account_id)
    assert next(item for item in events if item["id"] == f"M{first_cash}")["payload"]["principal_action_id"] == f"M{first_action}"
    assert next(item for item in events if item["id"] == f"M{final_action}")["instrument_id"] is not None
    result = evaluate_account(account, events, report, {}, day)
    assert result["status"] == "available", result["reasons"]
    assert result["cash"] == D("1000")
    assert result["cost_basis"] == D("0")
    assert result["realized"] == D("50")
    assert result["xirr"] is not None
    early = {**report[0], "period_end": "2025-03-31",
             "positions": [{"isin": "RU000A000001", "start_quantity": "0", "end_quantity": "10",
                            "name": "Synthetic bond", "currency": "RUB"}],
             "cash_snapshots": [{"start_amount": "0", "end_amount": "200", "currency": "RUB"}]}
    late = {**report[0], "attempt_id": 2, "period_start": "2025-04-01",
            "cash_snapshots": [{"start_amount": "200", "end_amount": "1000", "currency": "RUB"}]}
    intermediate = evaluate_account(account, events, [early, late], {}, day)
    assert intermediate["status"] == "available", intermediate["reasons"]
    assert intermediate["gaps"] == []
    rows = list(csv.reader(StringIO(operations_csv(events).decode("utf-8-sig"))))
    action_rows = [row for row in rows[1:] if row[0] in {f"M{first_action}", f"M{final_action}"}]
    assert {row[rows[0].index("principal_fraction")] for row in action_rows} == {"0.2", "1"}
    assert {row[rows[0].index("linked_cash_event_id")] for row in action_rows} == {f"M{first_cash}", f"M{final_cash}"}
    backup = tmp_path / "principal-backup.zip"
    create_backup(store, backup)
    restored = restore_backup(backup, tmp_path / "principal-restored")
    assert restored.list_events(account_id) == events


def test_principal_action_can_classify_a_broker_cash_row_without_copying_money(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("alfa", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (account, "synthetic-source", "synthetic.xls", "/synthetic", 1, "alfa", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "cash", 7)
        cash_id = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "cash", 7, "cash", None, "synthetic-cash",
             '{"kind":"transfer_unclassified","date":"2025-02-01","amount":"200","currency":"RUB"}',
             "2025-01-01"),
        ).lastrowid
    action_id = store.add_manual_principal_action(account, date(2025, 2, 1), "RU000A000001",
                                                  "Synthetic bond", D("0.2"), str(cash_id),
                                                  "verified principal statement")
    events = store.list_events(account)
    linked = next(item for item in events if item["id"] == cash_id)
    assert linked["payload"]["kind"] == "principal_return"
    assert linked["payload"]["source_kind"] == "transfer_unclassified"
    assert linked["payload"]["principal_action_id"] == f"M{action_id}"
    assert sum(D(item["payload"]["amount"]) for item in events if item["event_type"] == "cash") == D("200")
    with pytest.raises(ValueError, match="linked to a principal action"):
        store.classify_cash_event(cash_id, "deposit", "wrong reclassification")


def test_manual_cash_web_form_requires_auth_and_csrf(tmp_path):
    data = tmp_path / "private"
    store = Storage(data)
    account = store.create_account("sber", "brokerage", "Synthetic")
    client = TestClient(create_app(data, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    form = {"account_id": str(account), "date": "2025-01-02", "kind": "deposit", "amount": "100",
            "reason": "verified bank transfer"}
    assert client.post("/operations/manual/cash", data=form).status_code == 401
    client.get("/login")
    login_csrf = client.cookies.get("sb_login_csrf")
    assert client.post("/login", data={"csrf": login_csrf, "password": "synthetic-password-123"}).status_code == 303
    assert client.post("/operations/manual/cash", data=form).status_code == 403
    csrf = store.get_session_csrf(client.cookies.get("sb_session"))
    assert client.post("/operations/manual/cash", data={**form, "csrf": csrf}).status_code == 303
    trade = {"account_id": str(account), "traded_at": "2025-01-03", "settled_at": "2025-01-04",
             "isin": "RU000A000001", "name": "Synthetic", "quantity": "1", "price": "95",
             "amount": "95", "commission": "1", "reason": "=verified contract note"}
    assert client.post("/operations/manual/trade", data={**trade, "csrf": csrf}).status_code == 303
    page = client.get("/operations")
    assert "verified bank transfer" in page.text
    assert 'href="/operations/M1/source"' not in page.text
    export = client.get("/export/operations.csv")
    assert "verified bank transfer" in export.text
    rows = list(csv.reader(StringIO(export.content.decode("utf-8-sig"))))
    assert any(row[rows[0].index("manual_reason")] == "'=verified contract note" for row in rows[1:])
    principal_cash = {"account_id": str(account), "date": "2025-02-01", "kind": "principal_return",
                      "amount": "20", "reason": "verified principal payment", "csrf": csrf}
    assert client.post("/operations/manual/cash", data=principal_cash).status_code == 303
    action = {"account_id": str(account), "date": "2025-02-01", "isin": "RU000A000001",
              "name": "Synthetic", "principal_fraction": "0.2", "cash_event_id": "M3",
              "reason": "verified nominal schedule"}
    assert client.post("/operations/manual/principal", data=action).status_code == 403
    assert client.post("/operations/manual/principal", data={**action, "csrf": csrf}).status_code == 303
    assert "M3" in client.get("/operations").text
    assert "0.2" in client.get("/export/operations.csv").text
