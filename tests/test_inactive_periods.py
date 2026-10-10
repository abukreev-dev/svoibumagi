"""Owner-confirmed idle periods, with synthetic reports only. SPDX-License-Identifier: GPL-3.0-or-later"""

from copy import deepcopy
from datetime import date
import sqlite3

import pytest
from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password
from svoibumagi.backup import create_backup, restore_backup
from svoibumagi.metrics import evaluate_account, evaluate_combined
from svoibumagi.storage import Storage
from svoibumagi.web import TEMPLATES, create_app, pending_idle_gaps
from tests.test_metrics import _fixture


def _split_fixture():
    account, events, reports, prices = _fixture()
    early = deepcopy(reports[0])
    early["period_end"] = "2025-01-31"
    late = deepcopy(reports[0])
    late["attempt_id"] = 2
    late["period_start"] = "2025-03-01"
    late["period_end"] = "2025-03-31"
    late["positions"][0]["start_quantity"] = "1"
    prices["RU000A000001"]["as_of"] = "2025-03-31"
    events[0]["payload"]["accrued_interest"] = "0"
    return account, events, [early, late], prices


def _attestation(start="2025-02-01", end="2025-02-28"):
    return {"start_date": start, "end_date": end, "revoked_at": None}


def test_idle_confirmation_closes_only_its_dates_and_can_enable_xirr():
    account, events, reports, prices = _split_fixture()
    no_confirmation = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert no_confirmation["gaps"] == [{"start": "2025-02-01", "end": "2025-02-28", "days": 28}]
    assert no_confirmation["confirmable_gaps"] == no_confirmation["gaps"]
    partial = evaluate_account(account, events, reports, prices, date(2025, 1, 1),
                               inactive_periods=[_attestation(end="2025-02-20")])
    assert partial["gaps"] == [{"start": "2025-02-21", "end": "2025-02-28", "days": 8}]
    assert partial["confirmable_gaps"] == partial["gaps"]
    complete = evaluate_account(account, events, reports, prices, date(2025, 1, 1),
                                inactive_periods=[_attestation()])
    assert complete["gaps"] == []
    assert complete["status"] == complete["xirr_status"] == "available"
    assert complete["xirr"] is not None


def test_home_offers_exact_unconfirmed_gap_once():
    account, events, reports, prices = _split_fixture()
    metric = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    candidates = pending_idle_gaps([metric], {})
    assert candidates[account["id"]] == [{"start": "2025-02-01", "end": "2025-02-28", "days": 28}]
    html = TEMPLATES.get_template("home.html").render(
        logged_in=True, csrf="synthetic", accounts=[account], metrics=[metric],
        combined=evaluate_combined([metric]), estimate={"positions": []}, snapshots=[],
        attempts=[], event_count=0, today="2025-03-31", pending_idle_gaps=candidates)
    assert 'name="start_date" value="2025-02-01"' in html
    assert 'name="end_date" value="2025-02-28"' in html
    assert "Подтвердить эти даты" in html
    active = [{**_attestation(), "revoked_at": None}]
    assert pending_idle_gaps([metric], {account["id"]: active})[account["id"]] == []
    active[0]["revoked_at"] = "2025-04-01"
    assert pending_idle_gaps([metric], {account["id"]: active})[account["id"]] == candidates[account["id"]]


def test_idle_confirmation_is_ignored_on_conflicting_rows_or_balances():
    account, events, reports, prices = _split_fixture()
    cash_in_gap = {"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                   "payload": {"kind": "deposit", "date": "2025-02-10", "amount": "0",
                               "currency": "RUB"}}
    with_event = evaluate_account(account, events + [cash_in_gap], reports, prices, date(2025, 1, 1),
                                  inactive_periods=[_attestation()])
    assert len(with_event["gaps"]) == 1 and with_event["status"] == "unavailable"
    assert with_event["confirmable_gaps"] == []
    assert any("противоречит проведённой операции" in reason for reason in with_event["reasons"])
    middle = deepcopy(reports[0])
    middle["attempt_id"] = 3
    middle["period_start"] = "2025-02-01"
    middle["period_end"] = "2025-02-28"
    middle["positions"][0]["start_quantity"] = "1"
    report_conflict = evaluate_account(account, events + [cash_in_gap], [reports[0], middle, reports[1]],
                                       prices, date(2025, 1, 1), inactive_periods=[_attestation()])
    assert report_conflict["gaps"] == []
    assert report_conflict["status"] == "unavailable"
    assert any("противоречит проведённой операции" in reason for reason in report_conflict["reasons"])
    changed = deepcopy(reports)
    changed[1]["cash_snapshots"][0]["start_amount"] = "1"
    with_changed_boundary = evaluate_account(account, events, changed, prices, date(2025, 1, 1),
                                             inactive_periods=[_attestation()])
    assert len(with_changed_boundary["gaps"]) == 1
    assert with_changed_boundary["confirmable_gaps"] == []
    unidentified = deepcopy(reports)
    unidentified[0]["positions"].append({"isin": None, "start_quantity": "1", "end_quantity": "1",
                                            "name": "Unknown synthetic", "currency": "RUB"})
    with_unidentified = evaluate_account(account, events, unidentified, prices, date(2025, 1, 1),
                                         inactive_periods=[_attestation()])
    assert len(with_unidentified["gaps"]) == 1
    assert with_unidentified["confirmable_gaps"] == []
    absent_positions = deepcopy(reports)
    absent_positions[0]["positions"] = []
    absent_positions[1]["positions"] = []
    with_absent_positions = evaluate_account(account, events, absent_positions, prices, date(2025, 1, 1),
                                             inactive_periods=[_attestation()])
    assert len(with_absent_positions["gaps"]) == 1
    assert with_absent_positions["confirmable_gaps"] == []
    changed_position_currency = deepcopy(reports)
    changed_position_currency[1]["positions"][0]["currency"] = "USD"
    with_changed_currency = evaluate_account(account, events, changed_position_currency, prices,
                                             date(2025, 1, 1), inactive_periods=[_attestation()])
    assert len(with_changed_currency["gaps"]) == 1
    assert with_changed_currency["confirmable_gaps"] == []
    unresolved = deepcopy(reports)
    unresolved[0]["unparsed"] = [{"reason": "unresolved synthetic row"}]
    with_unresolved_row = evaluate_account(account, events, unresolved, prices, date(2025, 1, 1),
                                           inactive_periods=[_attestation()])
    assert len(with_unresolved_row["gaps"]) == 1
    assert with_unresolved_row["status"] == "unavailable"
    dated = deepcopy(reports)
    dated[1]["unparsed"] = [{"location": {"sheet": "synthetic", "row": 7},
                              "reason": "synthetic amount needs classification"}]
    dated[1]["cash_movements"] = [{"location": {"sheet": "synthetic", "row": 7},
                                   "date": "2025-03-10", "amount": "0"}]
    outside_gap = evaluate_account(account, events, dated, prices, date(2025, 1, 1),
                                   inactive_periods=[_attestation()])
    assert outside_gap["gaps"] == []
    assert outside_gap["status"] == "unavailable"
    unrelated_check = deepcopy(reports)
    unrelated_check[1]["checks"].append({"kind": "position", "status": "unavailable",
                                          "reason": "synthetic missing terminal position"})
    with_unrelated_check = evaluate_account(account, events, unrelated_check, prices,
                                            date(2025, 1, 1), inactive_periods=[_attestation()])
    assert with_unrelated_check["gaps"] == []
    assert with_unrelated_check["status"] == "unavailable"
    dated[1]["cash_movements"][0]["date"] = "2025-02-10"
    unresolved_inside_gap = evaluate_account(account, events, dated, prices, date(2025, 1, 1),
                                             inactive_periods=[_attestation()])
    assert len(unresolved_inside_gap["gaps"]) == 1
    pending = deepcopy(reports)
    pending[0]["summary_trades"] = [{"traded_at": "2025-01-31", "settled_at": "2025-02-02"}]
    with_pending_trade = evaluate_account(account, events, pending, prices, date(2025, 1, 1),
                                          inactive_periods=[_attestation()])
    assert len(with_pending_trade["gaps"]) == 1
    revoked = evaluate_account(account, events, reports, prices, date(2025, 1, 1),
                               inactive_periods=[{**_attestation(), "revoked_at": "2025-04-01"}])
    assert len(revoked["gaps"]) == 1


def test_storage_and_web_record_and_revoke_owner_confirmation(tmp_path, monkeypatch):
    root = tmp_path / "private"
    store = Storage(root)
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    account, events, reports, prices = _split_fixture()
    store.confirm_account_history(account_id, date(2025, 1, 1), "Synthetic opening")
    monkeypatch.setattr(store, "committed_reports", lambda _: reports)
    monkeypatch.setattr(store, "list_events", lambda _=None: events)
    monkeypatch.setattr(store, "latest_prices", lambda _: prices)
    assert store.financial_metrics()[0]["gaps"]
    period_id = store.confirm_inactive_period(account_id, date(2025, 2, 1), date(2025, 2, 28),
                                               "Synthetic owner confirmation")
    with pytest.raises(ValueError, match="overlaps"):
        store.confirm_inactive_period(account_id, date(2025, 2, 15), date(2025, 3, 1), "duplicate")
    assert store.list_inactive_periods(account_id, active_only=True)[0]["confirmed_by"] == "account_owner"
    assert store.financial_metrics()[0]["status"] == "available"
    store.revoke_inactive_period(account_id, period_id)
    assert store.financial_metrics()[0]["gaps"]
    assert not store.list_inactive_periods(account_id, active_only=True)
    with pytest.raises(ValueError, match="Unknown active"):
        store.revoke_inactive_period(account_id, period_id)
    with store._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    client = TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303
    csrf = client.get("/").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    assert client.post(f"/accounts/{account_id}/inactive", data={"csrf": csrf,
                       "start_date": "2025-02-01", "end_date": "2025-02-28",
                       "reason": "Synthetic web confirmation"}).status_code == 303
    active = store.list_inactive_periods(account_id, active_only=True)
    assert len(active) == 1
    assert "Synthetic web confirmation" in client.get("/").text
    assert client.post(f"/accounts/{account_id}/inactive/{active[0]['id']}/revoke",
                       data={"csrf": csrf}).status_code == 303
    assert store.list_inactive_periods(account_id, active_only=True) == []


def test_v18_migration_and_backup_preserve_inactive_confirmation(tmp_path):
    root = tmp_path / "data"
    initial = Storage(root)
    account_id = initial.create_account("sber", "brokerage", "Synthetic")
    with sqlite3.connect(initial.db_path) as con:
        con.execute("DROP TABLE inactive_periods")
        con.execute("PRAGMA user_version=18")
    migrated = Storage(root)
    with migrated._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 20
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(list(root.glob("ledger.pre-v18-*.bak"))) == 1
    migrated.confirm_inactive_period(account_id, date(2025, 2, 1), date(2025, 2, 28),
                                     "Synthetic owner confirmation")
    archive = tmp_path / "backup.zip"
    create_backup(migrated, archive)
    restored = restore_backup(archive, tmp_path / "restored")
    assert [(item["start_date"], item["end_date"], item["reason"])
            for item in restored.list_inactive_periods(account_id, active_only=True)] == [
                ("2025-02-01", "2025-02-28", "Synthetic owner confirmation")]
