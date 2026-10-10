"""Minimal broker source rows for direct SQLite controls.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import sqlite3
import json


def cash_payload(amount: str = "1", kind: str = "deposit", *, day: str = "2025-01-01",
                 component: str = "") -> str:
    payload = {"kind": kind, "date": day, "amount": amount, "currency": "RUB"}
    if component:
        payload["component"] = component
    return json.dumps(payload)


def trade_payload(isin: str = "RU000A000001", name: str = "Synthetic") -> str:
    return json.dumps({"traded_at": "2025-01-01", "settled_at": "2025-01-01", "isin": isin,
                       "name": name, "quantity": "1", "price": "1", "amount": "1",
                       "commission": "0", "currency": "RUB"})


def source_row(con: sqlite3.Connection, source_id: int, sheet: str, row_number: int,
               classification: str = "cash_movement", *, committed: bool = True) -> int:
    attempt = con.execute(
        "INSERT INTO import_attempts(source_id,state,adapter_version,period_start,period_end,preview_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (source_id, "awaiting_review", "synthetic", "1900-01-01", "1900-01-01", "{}", "2025-01-01"),
    ).lastrowid
    con.execute(
        "INSERT INTO source_rows(attempt_id,sheet,row_number,raw_json,classification,warning) "
        "VALUES(?,?,?,?,?,?)", (attempt, sheet, row_number, '["synthetic"]', classification, ""),
    )
    if committed:
        con.execute("UPDATE import_attempts SET state='committed' WHERE id=?", (attempt,))
    return int(attempt)


def synthetic_identity(con: sqlite3.Connection, account_id: int) -> None:
    """Skip real-file identity backfill for synthetic migration fixtures."""
    con.execute(
        "INSERT OR IGNORE INTO account_identities(account_id,identity_hash,confirmed_at) VALUES(?,?,?)",
        (account_id, "0" * 64, "2025-01-01"),
    )
