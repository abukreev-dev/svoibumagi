"""Dated broker values remain distinct from market unit prices. SPDX-License-Identifier: GPL-3.0-or-later"""

import csv
from datetime import date
from decimal import Decimal
from io import StringIO
import json
import sqlite3

from svoibumagi.exports import valuations_csv
from svoibumagi.storage import Storage
from tests.test_metrics import _fixture
from svoibumagi.metrics import evaluate_account


def test_old_sber_and_alfa_source_rows_supply_reported_end_values():
    with sqlite3.connect(":memory:") as con:
        con.execute("CREATE TABLE source_rows(attempt_id INTEGER,sheet TEXT,row_number INTEGER,"
                    "raw_json TEXT,classification TEXT)")
        sber = [""] * 18
        sber[1], sber[2] = "RU000A000001", "RUB"
        sber[8], sber[11], sber[12] = "2", "100.25", "1.25"
        alfa = [""] * 18
        alfa[6] = "Synthetic Alfa"
        alfa[15], alfa[17] = "3", "450.50"
        con.executemany("INSERT INTO source_rows VALUES(?,?,?,?,?)", [
            (1, "table:0", 3, json.dumps(sber), "position_snapshot"),
            (2, "Динамика позиций", 7, json.dumps(alfa), "position_snapshot")])
        left = {"broker": "sber", "positions": [{"location": {"sheet": "table:0", "row": 3},
                                                  "isin": "RU000A000001", "end_quantity": "2", "currency": "RUB"}]}
        right = {"broker": "alfa", "positions": [{"location": {"sheet": "Динамика позиций", "row": 7},
                                                    "name": "Synthetic Alfa", "end_quantity": "3", "currency": "RUB"}]}
        Storage._attach_reported_values(con, 1, left)
        Storage._attach_reported_values(con, 2, right)
        assert Decimal(left["positions"][0]["reported_value_rub"]) == Decimal("101.50")
        assert Decimal(right["positions"][0]["reported_value_rub"]) == Decimal("450.50")

        changed = {"broker": "sber", "positions": [{"location": {"sheet": "table:0", "row": 3},
                                                     "isin": "RU000A000001", "end_quantity": "4", "currency": "RUB"}]}
        Storage._attach_reported_values(con, 1, changed)
        assert "reported_value_rub" not in changed["positions"][0]
        con.execute("INSERT INTO source_rows VALUES(?,?,?,?,?)",
                    (1, "table:0", 3, json.dumps(sber), "position_snapshot"))
        Storage._attach_reported_values(con, 1, left)
        assert "reported_value_rub" not in left["positions"][0]


def test_broker_end_value_enables_terminal_valuation_but_exact_manual_price_overrides():
    account, events, reports, prices = _fixture()
    reports[0]["positions"][0]["reported_value_rub"] = "125.50"
    broker = evaluate_account(account, events, reports, {}, date(2025, 1, 1))
    assert broker["status"] == "available"
    assert broker["terminal"] == Decimal("125.50")
    manual = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert manual["status"] == "available"
    assert manual["terminal"] == Decimal("110")


def test_estimate_uses_report_value_only_on_its_exact_date(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    account = next(item for item in store.list_accounts() if item["id"] == account_id)
    snapshot = {"account": account, "period_end": "2025-01-31", "positions": [{
        "isin": "RU000A000001", "name": "Synthetic", "end_quantity": "2", "currency": "RUB",
        "reported_value_rub": "221.25"}], "unparsed": [], "checks": [{"status": "matched"}]}
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: [snapshot])
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {})
    exact = store.portfolio_estimate(date(2025, 1, 31))
    assert exact["positions"][0]["value"] == Decimal("221.25")
    assert exact["positions"][0]["price_status"] == "broker_report"
    row = next(csv.DictReader(StringIO(valuations_csv(exact).decode("utf-8-sig"))))
    assert (row["price_rub"], row["price_source"], row["method"], row["status"]) == (
        "", "broker_report", "broker_report_end_value", "broker_report_value")

    later = store.portfolio_estimate(date(2025, 2, 1))
    assert later["positions"][0]["value"] is None
    assert later["positions"][0]["price_status"] == "missing"

    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {
        "RU000A000001": {"amount": "115", "as_of": "2025-01-31", "source": "manual"}})
    overridden = store.portfolio_estimate(date(2025, 1, 31))
    assert overridden["positions"][0]["value"] == Decimal("230")
    assert overridden["positions"][0]["price_status"] == "fresh"


def test_two_account_estimate_keeps_unknown_value_out_of_known_subtotal(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    first_id = store.create_account("sber", "brokerage", "First synthetic account")
    second_id = store.create_account("alfa", "brokerage", "Second synthetic account")
    accounts = {item["id"]: item for item in store.list_accounts()}
    snapshots = [
        {"account": accounts[first_id], "period_end": "2025-01-31", "positions": [
            {"isin": "RU000A000001", "name": "Priced", "end_quantity": "2", "currency": "RUB",
             "reported_value_rub": "221.25"}], "unparsed": [], "checks": [{"status": "matched"}]},
        {"account": accounts[second_id], "period_end": "2025-01-31", "positions": [
            {"isin": "RU000A000002", "name": "Unknown", "end_quantity": "3", "currency": "RUB"},
            {"isin": "US0000000001", "name": "Sold out", "end_quantity": "0", "currency": "USD"}],
         "unparsed": [], "checks": [{"status": "matched"}]},
    ]
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: snapshots)
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {})

    estimate = store.portfolio_estimate(date(2025, 1, 31))
    assert estimate["known_securities_subtotal"] == Decimal("221.25")
    assert estimate["incomplete"] is True
    assert estimate["missing_prices"] == 1
    assert estimate["conversion_required"] == 0
    assert [row["price_status"] for row in estimate["positions"]] == [
        "broker_report", "missing", "zero_position"]
    rows = list(csv.DictReader(StringIO(valuations_csv(estimate).decode("utf-8-sig"))))
    assert [(row["account_id"], row["value_rub"], row["status"]) for row in rows] == [
        (str(first_id), "221.25", "broker_report_value"),
        (str(second_id), "", "unavailable"),
        (str(second_id), "0", "zero_position"),
    ]
