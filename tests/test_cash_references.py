"""Stable broker references for cash rows. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import datetime, timezone
import json

from svoibumagi.storage import Storage
from tests.source_fixture import source_row


def test_legacy_unkeyed_cash_event_is_found_by_trade_reference(tmp_path):
    store = Storage(tmp_path / "private")
    left = store.create_account("alfa", "brokerage", "First")
    right = store.create_account("alfa", "iis", "Second")
    payload = {"kind": "trade_cash", "broker_trade_id": "deal-1", "date": "2025-01-01",
               "amount": "-10", "currency": "RUB"}
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (left, "synthetic", "synthetic", "/synthetic", 1, "alfa", now)
        ).lastrowid
        source_row(con, source, "cash", 1)
        old_event = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (left, source, "cash", 1, "cash", None, "legacy-fingerprint", json.dumps(payload), now)
        ).lastrowid
        candidate = {"kind": "cash", "broker_id": "cash:trade_cash:deal-1",
                     "cash_reference": ("trade_cash", "deal-1"), "fingerprint": "new-fingerprint"}
        assert store._existing_events(con, left, candidate)[0][0] == old_event
        assert store._existing_events(con, right, candidate) == []


def test_cash_reference_requires_unique_kind_and_reference_in_report():
    movements = [
        {"location": {"sheet": "cash", "row": 1}, "kind": "trade_cash", "broker_trade_id": "deal-1",
         "date": "2025-01-01", "amount": "-10", "currency": "RUB"},
        {"location": {"sheet": "cash", "row": 2}, "kind": "trade_fee", "broker_trade_id": "deal-1",
         "date": "2025-01-01", "amount": "-1", "currency": "RUB"},
    ]
    items = Storage._event_items({"trades": [], "cash_movements": movements})
    assert [item["broker_id"] for item in items] == ["cash:trade_cash:deal-1", "cash:trade_fee:deal-1"]


def test_legacy_unkeyed_cash_decimal_spelling_is_an_ambiguous_match(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", now)
        ).lastrowid
        source_row(con, source, "cash", 1)
        old = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (account, source, "cash", 1, "cash", None, "old-fingerprint",
                                    json.dumps({"date": "2025-01-01", "kind": "deposit", "amount": "10.0", "currency": "RUB",
                                                "broker_trade_id": None}), now)
        ).lastrowid
        item = Storage._event_items({"trades": [], "cash_movements": [{
            "location": {"sheet": "cash", "row": 2}, "date": "2025-01-01", "kind": "deposit",
            "amount": "10", "currency": "RUB", "broker_trade_id": None,
        }]})[0]
        assert store._existing_events(con, account, item)[0][0] == old
