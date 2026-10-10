"""Synthetic cross-account security transfer acceptance. SPDX-License-Identifier: GPL-3.0-or-later"""

import csv
from datetime import date
from decimal import Decimal as D
from io import StringIO
import sqlite3

import pytest
from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password
from svoibumagi.backup import create_backup, restore_backup
from svoibumagi.exports import metrics_csv, operations_csv
from svoibumagi.metrics import evaluate_combined
from svoibumagi.storage import Storage
from svoibumagi.web import create_app


ISIN = "RU000A000001"


def _setup(store):
    source = store.create_account("sber", "brokerage", "Source")
    destination = store.create_account("alfa", "brokerage", "Destination")
    first = date(2025, 1, 1)
    store.add_manual_trade(source, first, first, ISIN, "Synthetic", D("10"), D("100"),
                           D("1000"), D("0"), "verified purchase")
    store.add_manual_cash(source, first, "deposit", D("1000"), "verified funding")
    store.add_manual_cash(source, first, "trade_cash", D("-1000"), "verified settlement")
    return source, destination


def test_transfer_is_paired_immutable_and_preserves_cost_through_backup(tmp_path):
    store = Storage(tmp_path / "private")
    source, destination = _setup(store)
    day = date(2025, 3, 1)
    with pytest.raises(ValueError, match="two existing"):
        store.add_security_transfer(source, 999, day, ISIN, "Synthetic", D("4"), "same owner")
    with pytest.raises(ValueError, match="confirmed lots"):
        store.add_security_transfer(source, destination, day, ISIN, "Synthetic", D("11"), "same owner")
    with pytest.raises(ValueError, match="Invalid"):
        store.add_security_transfer(source, source, day, ISIN, "Synthetic", D("4"), "same owner")
    transfer_id = store.add_security_transfer(source, destination, day, ISIN, "Synthetic", D("4"),
                                              "verified same-owner transfer")
    with pytest.raises(ValueError, match="already exists"):
        store.add_security_transfer(source, destination, day, ISIN, "Synthetic", D("4"), "duplicate")
    arms = [item for item in store.list_events() if item["event_type"] == "security_transfer"]
    assert {(item["id"], item["account_id"], item["payload"]["quantity"]) for item in arms} == {
        (f"T{transfer_id}:out", source, "-4"), (f"T{transfer_id}:in", destination, "4")}
    assert {item["payload"]["paired_event_id"] for item in arms} == {
        f"T{transfer_id}:out", f"T{transfer_id}:in"}
    rows = list(csv.reader(StringIO(operations_csv(arms).decode("utf-8-sig"))))
    assert {row[rows[0].index("peer_account_id")] for row in rows[1:]} == {str(source), str(destination)}
    replay = store._portfolio_lot_replay()
    assert replay[source][0][ISIN][0].cost == D("600")
    assert replay[destination][0][ISIN][0].cost == D("400")
    assert replay[destination][0][ISIN][0].acquired_at == date(2025, 1, 1)
    before_transfer = store._portfolio_lot_replay(as_of=date(2025, 2, 28))
    assert before_transfer[source][0][ISIN][0].cost == D("1000")
    assert before_transfer[destination][0] == {}
    with store._connect() as con:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("UPDATE security_transfers SET quantity='5' WHERE id=?", (transfer_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("DELETE FROM security_transfers WHERE id=?", (transfer_id,))
    archive = tmp_path / "backup.zip"
    create_backup(store, archive)
    restored = restore_backup(archive, tmp_path / "restored")
    assert [item for item in restored.list_events() if item["event_type"] == "security_transfer"] == arms


def test_in_kind_transfer_is_account_boundary_flow_but_not_external_portfolio_flow(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    source, destination = _setup(store)
    store.add_security_transfer(source, destination, date(2025, 3, 1), ISIN, "Synthetic", D("4"),
                                "verified same-owner transfer")
    store.add_price(ISIN, D("100"), date(2025, 3, 1))
    store.add_price(ISIN, D("100"), date(2025, 4, 30))
    for account_id in (source, destination):
        store.confirm_account_history(account_id, date(2025, 1, 1), "verified opening")
    reports = {account_id: [{"attempt_id": account_id, "period_start": "2025-01-01",
                             "period_end": "2025-04-30", "unparsed": [], "checks": [],
                             "positions": [{"isin": ISIN, "name": "Synthetic", "start_quantity": "0",
                                            "end_quantity": str(quantity), "currency": "RUB"}],
                             "cash_snapshots": [{"start_amount": "0", "end_amount": "0", "currency": "RUB"}]}]
               for account_id, quantity in ((source, 6), (destination, 4))}
    monkeypatch.setattr(store, "committed_reports", lambda account_id: reports[account_id])
    metrics = store.financial_metrics()
    assert [item["status"] for item in metrics] == ["available", "available"]
    assert [item["cost_basis"] for item in metrics] == [D("600"), D("400")]
    assert [item["xirr"] for item in metrics] == [D("0"), D("0")]
    assert [item["in_kind_transfer_flows"] for item in metrics] == [
        [(date(2025, 3, 1), D("400"))], [(date(2025, 3, 1), D("-400"))]]
    assert [item["external_flows"] for item in metrics] == [[(date(2025, 1, 1), D("-1000"))], []]
    assert evaluate_combined(metrics)["xirr"] == D("0")
    with store._connect() as con:
        con.execute("DELETE FROM prices WHERE as_of='2025-03-01'")
    incomplete = store.financial_metrics()
    assert all(item["status"] == "unavailable" for item in incomplete)
    assert all(item["xirr"] is None and item["combined_xirr_ready"] for item in incomplete)
    assert all("security transfer has no confirmed RUB market price on its date" in item["reasons"]
               for item in incomplete)
    combined = evaluate_combined(incomplete)
    assert combined["status"] == "unavailable" and combined["xirr_status"] == "available"
    assert combined["xirr"] == D("0")
    rows = list(csv.reader(StringIO(metrics_csv(incomplete, combined).decode("utf-8-sig"))))
    assert all(row[rows[0].index("in_kind_transfer_flow_rub")] == "" for row in rows[1:])
    assert rows[-1][rows[0].index("xirr_annual")] == "0"


def test_transfer_web_requires_login_csrf_and_shows_paired_source(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    source, destination = _setup(store)
    client = TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    form = {"source_account_id": str(source), "destination_account_id": str(destination),
            "date": "2025-03-01", "isin": ISIN, "name": "Synthetic", "quantity": "4",
            "reason": "verified same-owner transfer"}
    assert client.post("/operations/manual/security-transfer", data=form).status_code == 401
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303
    assert client.post("/operations/manual/security-transfer", data=form).status_code == 403
    csrf = store.get_session_csrf(client.cookies.get("sb_session"))
    saved = client.post("/operations/manual/security-transfer", data={**form, "csrf": csrf})
    assert saved.status_code == 303
    page = client.get("/operations").text
    assert "T1:out" in page and "T1:in" in page and "другой счёт" in page
