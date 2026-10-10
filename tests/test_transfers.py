"""Synthetic classification controls. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import datetime, timezone
import hashlib
import json

import pytest

from svoibumagi.storage import Storage
from tests.source_fixture import source_row


def _cash_event(store: Storage, account_id: int, amount: str) -> int:
    """Insert a minimal synthetic committed source to exercise ledger constraints."""
    payload = {"date": "2025-06-01", "kind": "transfer_unclassified", "amount": amount, "currency": "RUB"}
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(f"synthetic-{account_id}-{amount}".encode()).hexdigest()
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account_id, digest, "synthetic.html", "/synthetic", 1, "sber", now)
        ).lastrowid
        source_row(con, source, "synthetic", 1)
        return con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account_id, source, "synthetic", 1, "cash", None, digest, json.dumps(payload), now),
        ).lastrowid


def test_internal_cash_transfer_is_symmetric_and_atomic(tmp_path):
    store = Storage(tmp_path / "private")
    left = store.create_account("sber", "brokerage", "First")
    right = store.create_account("sber", "iis", "Second")
    outgoing = _cash_event(store, left, "-100")
    incoming = _cash_event(store, right, "100")
    store.classify_cash_event(outgoing, "internal_out", "Between my accounts", incoming)
    events = {event["id"]: event for event in store.list_events()}
    assert events[outgoing]["payload"]["kind"] == "internal_out"
    assert events[incoming]["payload"]["kind"] == "internal_in"
    assert events[outgoing]["payload"]["paired_event_id"] == incoming
    assert events[incoming]["payload"]["paired_event_id"] == outgoing
    assert events[outgoing]["payload"]["source_kind"] == "transfer_unclassified"
    with pytest.raises(ValueError, match="independently"):
        store.classify_cash_event(outgoing, "withdrawal", "Wrong")
    assert store.list_events()[0]["payload"]["kind"] == "internal_in"


def test_invalid_pairs_do_not_change_existing_classification(tmp_path):
    store = Storage(tmp_path / "private")
    left = store.create_account("sber", "brokerage", "First")
    right = store.create_account("sber", "iis", "Second")
    outgoing = _cash_event(store, left, "-100")
    wrong_amount = _cash_event(store, right, "99")
    with pytest.raises(ValueError, match="opposite amount"):
        store.classify_cash_event(outgoing, "internal_out", "Wrong pair", wrong_amount)
    assert all(event["classified_kind"] is None for event in store.list_events())
    store.classify_cash_event(outgoing, "withdrawal", "Sent outside")
    with pytest.raises(ValueError, match="direction"):
        store.classify_cash_event(outgoing, "deposit", "Wrong sign")
    assert next(event for event in store.list_events() if event["id"] == outgoing)["payload"]["kind"] == "withdrawal"
