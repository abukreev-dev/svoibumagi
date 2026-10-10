"""Private local storage and non-mutating import preview. SPDX-License-Identifier: GPL-3.0-or-later"""

from dataclasses import asdict
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .alfa import ADAPTER_VERSION as ALFA_ADAPTER_VERSION, CASH_SECTION_REASON, CASH_SUMMARY_REASON, EMPTY_NONTRADE_SHEET_REASON, EMPTY_TRADE_SHEET_REASON, HEADER_REASON, ISIN_CONTINUATION_REASON, NONTRADE_METADATA_REASON, POSITION_FOOTNOTE_REASON, POSITION_TOTAL_REASON, TAX_HEADER_REASON, TRADE_FOOTNOTE_REASON, TOTAL_REASON as ALFA_TOTAL_REASON, UnsupportedReport, _execution_time, _get, _num, _sheet_rows, parse_alfa
from .prices import MAX_PRICE_AGE_DAYS, MoexQuote, MoexSecurity, price_age_days
from .reconcile import reconcile
from .sber import ADAPTER_VERSION as SBER_ADAPTER_VERSION, OVERVIEW_REASON, SERVICE_REASON, TOTAL_REASON, _cells, _n, parse_sber
from .source_match import raw_equivalent


MAX_UPLOAD_BYTES = 20_000_000


def _source_rows(path: Path, broker: str, report) -> list[tuple[str, int, str, str, str]]:
    """Keep classified source cells in the private database for review."""
    classes: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for collection, category in ((report.trades, "trade"), (report.cash_movements, "cash_movement"),
                                 (report.positions, "position_snapshot"), (report.cash_snapshots, "cash_snapshot"),
                                 (report.cash_flow_subtotals, "cash_flow_subtotal"),
                                 (report.unparsed, "unparsed"), (report.excluded, "excluded")):
        for item in collection:
            classes.setdefault((item.location.sheet, item.location.row), []).append(
                (category, getattr(item, "reason", "")))
    if broker == "sber":
        from bs4 import BeautifulSoup

        tables = BeautifulSoup(path.read_bytes(), "html.parser").find_all("table")
        def cells(sheet: str, row: int):
            table = tables[int(sheet.removeprefix("table:"))]
            return _cells(table.find_all("tr")[row - 1])
    else:
        sheets, _ = _sheet_rows(path)
        def cells(sheet: str, row: int):
            return sheets[sheet][row - 1]
    result = []
    for (sheet, row), classifications in classes.items():
        raw = json.dumps(cells(sheet, row), default=_json_default, ensure_ascii=False)
        for category, reason in dict.fromkeys(classifications):
            result.append((sheet, row, raw, category, reason))
    return result


def _trade_preview(trade) -> dict:
    item = asdict(trade)
    if item["execution_time"] is None:
        item.pop("execution_time")
    if not item["execution_time_required"]:
        item.pop("execution_time_required")
    return item


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def _canonical_decimal(value: object) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite event amount")
    if number == 0:
        return "0"
    parts = number.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    normalized = Decimal((parts.sign, tuple(digits), exponent))
    return format(normalized, "f") if -18 <= exponent <= 18 else str(normalized)


def _canonical_event_payload(kind: str, payload: dict) -> dict:
    payload = dict(payload)
    if kind == "cash" and payload.get("withheld_tax") is None:
        payload.pop("withheld_tax", None)
    if kind == "cash" and not payload.get("component"):
        payload.pop("component", None)
    if kind == "trade" and payload.get("accrued_interest") is None:
        payload.pop("accrued_interest", None)
    fields = ("quantity", "price", "amount", "commission", "accrued_interest") if kind == "trade" else ("amount", "withheld_tax")
    for field in fields:
        if field in payload:
            payload[field] = _canonical_decimal(payload[field])
    return payload


def _detect(path: Path) -> tuple[str, str]:
    with path.open("rb") as stream:
        first = stream.read(4096)
    if first.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "alfa", ".xls"
    if first.startswith(b"PK\x03\x04"):
        return "alfa", ".xlsx"
    if b"<html" in first.lower() or b"<!doctype html" in first.lower() or b"<table" in first.lower() and b"\xd0\x9e\xd1\x82\xd1\x87\xd0\xb5\xd1\x82" in first:
        return "sber", ".html"
    raise UnsupportedReport("Unknown report container")


class Storage:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.files = root / "files"
        self.files.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.files, 0o700)
        self.db_path = root / "ledger.sqlite3"
        existing_db = self.db_path.exists()
        with self._connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 20:
                raise ValueError(f"Database schema version {version} is newer than this application")
            if existing_db and version < 20:
                if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Existing database failed integrity check before migration")
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_events'").fetchone():
                    if con.execute(
                        "SELECT 1 FROM ledger_events e JOIN source_files s ON s.id=e.source_id "
                        "JOIN accounts a ON a.id=e.account_id "
                        "WHERE s.account_id<>e.account_id OR s.detected_broker<>a.broker LIMIT 1"
                    ).fetchone():
                        raise ValueError("Existing ledger contains an event linked to another account or broker")
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_observations'").fetchone():
                    if con.execute(
                        "SELECT 1 FROM event_observations o JOIN ledger_events e ON e.id=o.event_id "
                        "JOIN source_files s ON s.id=o.source_id "
                        "WHERE s.account_id<>e.account_id OR o.event_type<>e.event_type LIMIT 1"
                    ).fetchone():
                        raise ValueError("Existing observations contain an account or event-type mismatch")
                    if con.execute(
                        "SELECT 1 FROM event_observations GROUP BY event_id,source_id HAVING COUNT(*)>1 LIMIT 1"
                    ).fetchone():
                        raise ValueError("Existing observations map one event to multiple rows in one file")
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instrument_events'").fetchone():
                    if con.execute(
                        "SELECT 1 FROM instrument_events ie JOIN ledger_events e ON e.id=ie.event_id "
                        "JOIN instruments i ON i.id=ie.instrument_id "
                        "WHERE e.event_type<>'trade' OR i.isin<>json_extract(e.payload_json,'$.isin') LIMIT 1"
                    ).fetchone():
                        raise ValueError("Existing instrument link does not match its trade")
                backup_path = root / f"ledger.pre-v{version}-{secrets.token_hex(4)}.bak"
                fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
                try:
                    with sqlite3.connect(backup_path) as backup:
                        con.backup(backup)
                except Exception:
                    backup_path.unlink(missing_ok=True)
                    raise
            account_columns = {row[1] for row in con.execute("PRAGMA table_info(accounts)")}
            if existing_db and version >= 20 and not {"currency", "status"} <= account_columns:
                raise ValueError("Current account schema is missing required fields")
            account_upgrade_sql = (
                ("ALTER TABLE accounts ADD COLUMN currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency='RUB');"
                 if account_columns and "currency" not in account_columns else "")
                + ("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                   "CHECK (status IN ('active','closed'));"
                   if account_columns and "status" not in account_columns else "")
            )
            source_schema = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_rows'"
            ).fetchone()
            if source_schema and "cash_flow_subtotal" not in source_schema[0]:
                con.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE source_rows RENAME TO source_rows_before_v10;
                CREATE TABLE source_rows (
                    id INTEGER PRIMARY KEY,
                    attempt_id INTEGER NOT NULL REFERENCES import_attempts(id),
                    sheet TEXT NOT NULL,
                    row_number INTEGER NOT NULL CHECK (row_number > 0),
                    raw_json TEXT NOT NULL,
                    classification TEXT NOT NULL CHECK (classification IN
                        ('trade','cash_movement','position_snapshot','cash_snapshot','cash_flow_subtotal','unparsed','excluded')),
                    warning TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO source_rows(id,attempt_id,sheet,row_number,raw_json,classification,warning)
                    SELECT id,attempt_id,sheet,row_number,raw_json,classification,warning FROM source_rows_before_v10;
                DROP TABLE source_rows_before_v10;
                CREATE INDEX source_rows_attempt ON source_rows(attempt_id,sheet,row_number);
                COMMIT;
                """)
            observation_schema = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='event_observations'"
            ).fetchone()
            if observation_schema and "component" not in observation_schema[0]:
                con.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE event_observations RENAME TO event_observations_before_v11;
                CREATE TABLE event_observations (
                    id INTEGER PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES ledger_events(id),
                    source_id INTEGER NOT NULL REFERENCES source_files(id),
                    sheet TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type IN ('trade', 'cash')),
                    component TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_id, sheet, row_number, event_type, component)
                );
                INSERT INTO event_observations(id,event_id,source_id,sheet,row_number,event_type,component)
                    SELECT id,event_id,source_id,sheet,row_number,event_type,'' FROM event_observations_before_v11;
                DROP TABLE event_observations_before_v11;
                CREATE UNIQUE INDEX event_observation_per_file ON event_observations(event_id,source_id);
                COMMIT;
                """)
            replace_source_triggers = (
                "DROP TRIGGER IF EXISTS validate_ledger_source_row;"
                "DROP TRIGGER IF EXISTS validate_observation_source_row;"
                if version < 17 else ""
            )
            con.executescript("""
            BEGIN IMMEDIATE;
            """ + replace_source_triggers + """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                broker TEXT NOT NULL CHECK (broker IN ('sber', 'alfa')),
                kind TEXT NOT NULL CHECK (kind IN ('brokerage', 'iis')),
                name TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency='RUB'),
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed'))
            );
            """ + account_upgrade_sql + """
            CREATE TABLE IF NOT EXISTS account_identities (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
                identity_hash TEXT NOT NULL CHECK (length(identity_hash)=64),
                confirmed_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS unique_account_identity_kind BEFORE INSERT ON account_identities
            WHEN EXISTS(
                SELECT 1 FROM account_identities other
                JOIN accounts old_account ON old_account.id=other.account_id
                JOIN accounts new_account ON new_account.id=NEW.account_id
                WHERE other.identity_hash=NEW.identity_hash
                    AND old_account.broker=new_account.broker AND old_account.kind=new_account.kind
                    AND other.account_id<>NEW.account_id
            )
            BEGIN SELECT RAISE(ABORT,'contract is bound to another account of the same type'); END;
            CREATE TRIGGER IF NOT EXISTS protect_account_identity_record BEFORE UPDATE ON account_identities
            BEGIN SELECT RAISE(ABORT,'confirmed account identity is immutable'); END;
            CREATE TABLE IF NOT EXISTS instruments (
                id INTEGER PRIMARY KEY,
                isin TEXT NOT NULL UNIQUE CHECK (length(isin)=12),
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_history (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
                from_date TEXT NOT NULL,
                reason TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inactive_periods (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                start_date TEXT NOT NULL CHECK (start_date GLOB '????-??-??'),
                end_date TEXT NOT NULL CHECK (end_date GLOB '????-??-??' AND end_date>=start_date),
                reason TEXT NOT NULL CHECK (length(trim(reason))>0),
                confirmed_by TEXT NOT NULL CHECK (confirmed_by='account_owner'),
                confirmed_at TEXT NOT NULL,
                revoked_at TEXT,
                CHECK (revoked_at IS NULL OR revoked_at>=confirmed_at)
            );
            CREATE INDEX IF NOT EXISTS inactive_periods_account ON inactive_periods(account_id,start_date,end_date);
            CREATE TABLE IF NOT EXISTS source_files (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                sha256 TEXT NOT NULL,
                original_name TEXT NOT NULL,
                private_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                detected_broker TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, sha256)
            );
            CREATE TRIGGER IF NOT EXISTS validate_source_file BEFORE INSERT ON source_files
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM accounts WHERE id=NEW.account_id AND broker=NEW.detected_broker
                ) THEN RAISE(ABORT,'source file account or broker mismatch') END;
            END;
            CREATE TRIGGER IF NOT EXISTS protect_source_file_identity BEFORE UPDATE OF
                account_id,sha256,original_name,size,detected_broker,created_at ON source_files
            BEGIN SELECT RAISE(ABORT,'source file identity is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_source_file_delete BEFORE DELETE ON source_files
            WHEN EXISTS(SELECT 1 FROM import_attempts WHERE source_id=OLD.id AND state='committed')
                OR EXISTS(SELECT 1 FROM ledger_events WHERE source_id=OLD.id)
                OR EXISTS(SELECT 1 FROM event_observations WHERE source_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'committed source files are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_account_identity BEFORE UPDATE OF broker,kind ON accounts
            WHEN EXISTS(SELECT 1 FROM source_files WHERE account_id=OLD.id)
                OR EXISTS(SELECT 1 FROM account_identities WHERE account_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'account broker and kind are immutable after import'); END;
            CREATE TABLE IF NOT EXISTS import_attempts (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES source_files(id),
                state TEXT NOT NULL CHECK (state IN ('awaiting_review', 'committed', 'rejected')),
                adapter_version TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS protect_import_attempt_fields BEFORE UPDATE OF
                source_id,adapter_version,period_start,period_end,preview_json,created_at ON import_attempts
            BEGIN SELECT RAISE(ABORT,'import attempt source and preview are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS validate_import_attempt_state BEFORE UPDATE OF state ON import_attempts
            WHEN OLD.state<>'awaiting_review' OR NEW.state NOT IN ('committed','rejected')
            BEGIN SELECT RAISE(ABORT,'import attempt state transition is invalid'); END;
            CREATE TRIGGER IF NOT EXISTS protect_committed_attempt_delete BEFORE DELETE ON import_attempts
            WHEN OLD.state='committed'
            BEGIN SELECT RAISE(ABORT,'committed import attempts are immutable'); END;
            CREATE TABLE IF NOT EXISTS source_rows (
                id INTEGER PRIMARY KEY,
                attempt_id INTEGER NOT NULL REFERENCES import_attempts(id),
                sheet TEXT NOT NULL,
                row_number INTEGER NOT NULL CHECK (row_number > 0),
                raw_json TEXT NOT NULL,
                classification TEXT NOT NULL CHECK (classification IN ('trade','cash_movement','position_snapshot','cash_snapshot','cash_flow_subtotal','unparsed','excluded')),
                warning TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS source_rows_attempt ON source_rows(attempt_id,sheet,row_number);
            CREATE TRIGGER IF NOT EXISTS protect_source_row_update BEFORE UPDATE ON source_rows
            WHEN EXISTS(SELECT 1 FROM import_attempts WHERE id=OLD.attempt_id AND state='committed')
            BEGIN SELECT RAISE(ABORT,'source rows are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_source_row_delete BEFORE DELETE ON source_rows
            WHEN EXISTS(SELECT 1 FROM import_attempts WHERE id=OLD.attempt_id AND state='committed')
            BEGIN SELECT RAISE(ABORT,'source rows are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_referenced_source_row_update BEFORE UPDATE ON source_rows
            WHEN EXISTS(
                SELECT 1 FROM import_attempts a JOIN ledger_events e ON e.source_id=a.source_id
                WHERE a.id=OLD.attempt_id AND e.sheet=OLD.sheet AND e.row_number=OLD.row_number
            ) OR EXISTS(
                SELECT 1 FROM import_attempts a JOIN event_observations o ON o.source_id=a.source_id
                WHERE a.id=OLD.attempt_id AND o.sheet=OLD.sheet AND o.row_number=OLD.row_number
            )
            BEGIN SELECT RAISE(ABORT,'referenced source row is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_last_referenced_source_row_delete BEFORE DELETE ON source_rows
            WHEN NOT EXISTS(
                SELECT 1 FROM source_rows other JOIN import_attempts a ON a.id=other.attempt_id
                JOIN import_attempts old_attempt ON old_attempt.id=OLD.attempt_id
                WHERE other.id<>OLD.id AND a.source_id=old_attempt.source_id
                    AND other.sheet=OLD.sheet AND other.row_number=OLD.row_number
                    AND other.classification=OLD.classification
            ) AND (
                EXISTS(
                    SELECT 1 FROM import_attempts a JOIN ledger_events e ON e.source_id=a.source_id
                    WHERE a.id=OLD.attempt_id AND e.sheet=OLD.sheet AND e.row_number=OLD.row_number
                        AND ((e.event_type='trade' AND OLD.classification IN ('trade','unparsed'))
                            OR (e.event_type='cash' AND OLD.classification='cash_movement'))
                ) OR EXISTS(
                    SELECT 1 FROM import_attempts a JOIN event_observations o ON o.source_id=a.source_id
                    WHERE a.id=OLD.attempt_id AND o.sheet=OLD.sheet AND o.row_number=OLD.row_number
                        AND ((o.event_type='trade' AND OLD.classification IN ('trade','unparsed'))
                            OR (o.event_type='cash' AND OLD.classification='cash_movement'))
                )
            )
            BEGIN SELECT RAISE(ABORT,'referenced source row cannot be deleted'); END;
            CREATE TABLE IF NOT EXISTS ledger_events (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                source_id INTEGER NOT NULL REFERENCES source_files(id),
                sheet TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('trade', 'cash')),
                broker_event_id TEXT,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS event_broker_id ON ledger_events(account_id, event_type, broker_event_id)
                WHERE broker_event_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS event_fingerprint ON ledger_events(account_id, fingerprint);
            CREATE TRIGGER IF NOT EXISTS validate_ledger_source BEFORE INSERT ON ledger_events
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM source_files s JOIN accounts a ON a.id=NEW.account_id
                    WHERE s.id=NEW.source_id AND s.account_id=NEW.account_id AND s.detected_broker=a.broker
                ) THEN RAISE(ABORT,'ledger source account or broker mismatch') END;
            END;
            CREATE TRIGGER IF NOT EXISTS validate_ledger_source_row BEFORE INSERT ON ledger_events
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id
                    WHERE a.source_id=NEW.source_id AND a.state='committed'
                        AND r.sheet=NEW.sheet AND r.row_number=NEW.row_number
                        AND ((NEW.event_type='trade' AND r.classification IN ('trade','unparsed'))
                            OR (NEW.event_type='cash' AND r.classification='cash_movement'))
                ) THEN RAISE(ABORT,'ledger source row is unavailable') END;
            END;
            CREATE TRIGGER IF NOT EXISTS validate_ledger_payload BEFORE INSERT ON ledger_events
            BEGIN
                SELECT CASE WHEN json_valid(NEW.payload_json)=0
                    THEN RAISE(ABORT,'ledger payload is not JSON') END;
                SELECT CASE WHEN NEW.event_type='trade' AND (
                    COALESCE(json_type(NEW.payload_json,'$.traded_at'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.settled_at'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.isin'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.name'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.quantity'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.price'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.amount'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.commission'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.currency'),'')<>'text'
                ) THEN RAISE(ABORT,'trade payload has missing or invalid fields') END;
                SELECT CASE WHEN NEW.event_type='cash' AND (
                    COALESCE(json_type(NEW.payload_json,'$.date'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.kind'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.amount'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.currency'),'')<>'text'
                ) THEN RAISE(ABORT,'cash payload has missing or invalid fields') END;
            END;
            CREATE TABLE IF NOT EXISTS instrument_events (
                event_id INTEGER PRIMARY KEY REFERENCES ledger_events(id),
                instrument_id INTEGER NOT NULL REFERENCES instruments(id)
            );
            CREATE TRIGGER IF NOT EXISTS validate_instrument_event BEFORE INSERT ON instrument_events
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM ledger_events e JOIN instruments i ON i.id=NEW.instrument_id
                    WHERE e.id=NEW.event_id AND e.event_type='trade'
                        AND i.isin=json_extract(e.payload_json,'$.isin')
                ) THEN RAISE(ABORT,'instrument link does not match trade') END;
            END;
            CREATE TRIGGER IF NOT EXISTS protect_instrument_event_update BEFORE UPDATE ON instrument_events
            BEGIN SELECT RAISE(ABORT,'instrument links are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_instrument_event_delete BEFORE DELETE ON instrument_events
            BEGIN SELECT RAISE(ABORT,'instrument links are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS link_trade_instrument AFTER INSERT ON ledger_events
            WHEN NEW.event_type='trade'
            BEGIN
                INSERT OR IGNORE INTO instruments(isin,display_name,created_at)
                    VALUES(json_extract(NEW.payload_json,'$.isin'),json_extract(NEW.payload_json,'$.name'),NEW.created_at);
                INSERT INTO instrument_events(event_id,instrument_id)
                    SELECT NEW.id,id FROM instruments WHERE isin=json_extract(NEW.payload_json,'$.isin');
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM instrument_events WHERE event_id=NEW.id)
                    THEN RAISE(ABORT,'trade has no valid instrument') END;
            END;
            CREATE TRIGGER IF NOT EXISTS protect_ledger_event_update BEFORE UPDATE ON ledger_events
            BEGIN SELECT RAISE(ABORT,'ledger events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_ledger_event_delete BEFORE DELETE ON ledger_events
            BEGIN SELECT RAISE(ABORT,'ledger events are immutable'); END;
            CREATE TABLE IF NOT EXISTS manual_events (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                event_type TEXT NOT NULL CHECK (event_type IN ('trade','cash')),
                payload_json TEXT NOT NULL,
                reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
                created_at TEXT NOT NULL,
                UNIQUE(account_id,event_type,payload_json,reason)
            );
            CREATE TRIGGER IF NOT EXISTS validate_manual_payload BEFORE INSERT ON manual_events
            BEGIN
                SELECT CASE WHEN json_valid(NEW.payload_json)=0
                    THEN RAISE(ABORT,'manual payload is not JSON') END;
                SELECT CASE WHEN NEW.event_type='trade' AND (
                    COALESCE(json_type(NEW.payload_json,'$.traded_at'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.settled_at'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.isin'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.name'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.quantity'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.price'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.amount'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.commission'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.currency'),'')<>'text'
                ) THEN RAISE(ABORT,'manual trade payload has missing or invalid fields') END;
                SELECT CASE WHEN NEW.event_type='cash' AND (
                    COALESCE(json_type(NEW.payload_json,'$.date'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.kind'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.amount'),'')<>'text' OR
                    COALESCE(json_type(NEW.payload_json,'$.currency'),'')<>'text'
                ) THEN RAISE(ABORT,'manual cash payload has missing or invalid fields') END;
            END;
            CREATE UNIQUE INDEX IF NOT EXISTS manual_principal_cash_source
                ON manual_events(json_extract(payload_json,'$.cash_event_id'))
                WHERE event_type='cash' AND json_extract(payload_json,'$.kind')='principal_action';
            CREATE UNIQUE INDEX IF NOT EXISTS manual_principal_instrument_date
                ON manual_events(account_id,json_extract(payload_json,'$.isin'),json_extract(payload_json,'$.date'))
                WHERE event_type='cash' AND json_extract(payload_json,'$.kind')='principal_action';
            CREATE TRIGGER IF NOT EXISTS protect_manual_event_update BEFORE UPDATE ON manual_events
            BEGIN SELECT RAISE(ABORT,'manual events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_manual_event_delete BEFORE DELETE ON manual_events
            BEGIN SELECT RAISE(ABORT,'manual events are immutable'); END;
            CREATE TABLE IF NOT EXISTS security_transfers (
                id INTEGER PRIMARY KEY,
                source_account_id INTEGER NOT NULL REFERENCES accounts(id),
                destination_account_id INTEGER NOT NULL REFERENCES accounts(id),
                isin TEXT NOT NULL CHECK (length(isin)=12),
                display_name TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                quantity TEXT NOT NULL,
                reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
                created_at TEXT NOT NULL,
                CHECK (source_account_id<>destination_account_id),
                UNIQUE(source_account_id,destination_account_id,isin,effective_date)
            );
            CREATE TRIGGER IF NOT EXISTS protect_security_transfer_update BEFORE UPDATE ON security_transfers
            BEGIN SELECT RAISE(ABORT,'security transfers are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_security_transfer_delete BEFORE DELETE ON security_transfers
            BEGIN SELECT RAISE(ABORT,'security transfers are immutable'); END;
            CREATE TABLE IF NOT EXISTS event_observations (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES ledger_events(id),
                source_id INTEGER NOT NULL REFERENCES source_files(id),
                sheet TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('trade', 'cash')),
                component TEXT NOT NULL DEFAULT '',
                UNIQUE(source_id, sheet, row_number, event_type, component)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS event_observation_per_file ON event_observations(event_id,source_id);
            CREATE TRIGGER IF NOT EXISTS validate_event_observation BEFORE INSERT ON event_observations
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM ledger_events e JOIN source_files s ON s.id=NEW.source_id
                    JOIN accounts a ON a.id=e.account_id
                    WHERE e.id=NEW.event_id AND s.account_id=e.account_id
                        AND s.detected_broker=a.broker AND NEW.event_type=e.event_type
                        AND NEW.component=COALESCE(json_extract(e.payload_json,'$.component'),'')
                ) THEN RAISE(ABORT,'observation account or event-type mismatch') END;
            END;
            CREATE TRIGGER IF NOT EXISTS validate_observation_source_row BEFORE INSERT ON event_observations
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id
                    WHERE a.source_id=NEW.source_id AND a.state='committed'
                        AND r.sheet=NEW.sheet AND r.row_number=NEW.row_number
                        AND ((NEW.event_type='trade' AND r.classification IN ('trade','unparsed'))
                            OR (NEW.event_type='cash' AND r.classification='cash_movement'))
                ) THEN RAISE(ABORT,'observation source row is unavailable') END;
            END;
            CREATE TRIGGER IF NOT EXISTS protect_event_observation_update BEFORE UPDATE ON event_observations
            BEGIN SELECT RAISE(ABORT,'event observations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_event_observation_delete BEFORE DELETE ON event_observations
            BEGIN SELECT RAISE(ABORT,'event observations are immutable'); END;
            CREATE TABLE IF NOT EXISTS audit_decisions (
                id INTEGER PRIMARY KEY,
                attempt_id INTEGER NOT NULL REFERENCES import_attempts(id),
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS protect_audit_decision_update BEFORE UPDATE ON audit_decisions
            BEGIN SELECT RAISE(ABORT,'audit decisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS protect_audit_decision_delete BEFORE DELETE ON audit_decisions
            WHEN EXISTS(SELECT 1 FROM import_attempts WHERE id=OLD.attempt_id AND state='committed')
            BEGIN SELECT RAISE(ABORT,'audit decisions are immutable'); END;
            CREATE TABLE IF NOT EXISTS owner_sessions (
                token_hash TEXT PRIMARY KEY,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY,
                isin TEXT NOT NULL,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL CHECK (currency='RUB'),
                source TEXT NOT NULL CHECK (source IN ('manual', 'moex')),
                as_of TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS price_lookup ON prices(isin,as_of DESC,id DESC);
            CREATE TABLE IF NOT EXISTS cash_classifications (
                event_id INTEGER PRIMARY KEY REFERENCES ledger_events(id),
                kind TEXT NOT NULL CHECK (kind IN ('deposit','withdrawal','internal_in','internal_out')),
                paired_event_id INTEGER REFERENCES ledger_events(id),
                reason TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                CHECK ((kind IN ('internal_in','internal_out')) = (paired_event_id IS NOT NULL))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS cash_pair_unique ON cash_classifications(paired_event_id) WHERE paired_event_id IS NOT NULL;
            INSERT OR IGNORE INTO instruments(isin,display_name,created_at)
                SELECT json_extract(payload_json,'$.isin'),json_extract(payload_json,'$.name'),created_at
                FROM ledger_events WHERE event_type='trade';
            INSERT OR IGNORE INTO instrument_events(event_id,instrument_id)
                SELECT e.id,i.id FROM ledger_events e JOIN instruments i
                ON i.isin=json_extract(e.payload_json,'$.isin') WHERE e.event_type='trade';
            PRAGMA user_version = 20;
            COMMIT;
            """)
        os.chmod(self.db_path, 0o600)
        self._backfill_account_identities()

    def _backfill_account_identities(self) -> None:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT s.account_id,s.private_path,s.detected_broker FROM source_files s "
                "JOIN import_attempts a ON a.source_id=s.id WHERE a.state='committed' "
                "AND NOT EXISTS(SELECT 1 FROM account_identities i WHERE i.account_id=s.account_id)"
            ).fetchall()
        by_account: dict[int, set[str]] = {}
        for account_id, private_path, broker in rows:
            report = (parse_sber if broker == "sber" else parse_alfa)(Path(private_path))
            if report.account_kind:
                with self._connect() as con:
                    kind = con.execute("SELECT kind FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
                if kind != report.account_kind:
                    raise ValueError("Existing account kind differs from its report contract")
            if report.account_key:
                by_account.setdefault(account_id, set()).add(report.account_key)
        if any(len(keys) > 1 for keys in by_account.values()):
            raise ValueError("Existing account contains reports from multiple contracts")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.executemany(
                "INSERT OR IGNORE INTO account_identities(account_id,identity_hash,confirmed_at) VALUES(?,?,?)",
                ((account_id, next(iter(keys)), now) for account_id, keys in by_account.items()),
            )

    def _connect(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA foreign_keys = ON")
        return con

    @contextmanager
    def _file_lock(self):
        fd = os.open(self.root / ".files.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def create_account(self, broker: str, kind: str, name: str) -> int:
        if broker not in {"sber", "alfa"} or kind not in {"brokerage", "iis"} or not name.strip():
            raise ValueError("Invalid account")
        with self._connect() as con:
            cur = con.execute("INSERT INTO accounts(broker,kind,name) VALUES(?,?,?)", (broker, kind, name.strip()))
            return int(cur.lastrowid)

    def delete_empty_account(self, account_id: int) -> None:
        """Remove an unused account; foreign keys preserve every source and decision."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Account has reports, attempts, confirmations or events") from exc
            if cur.rowcount != 1:
                raise ValueError("Unknown account")

    def deletable_account_ids(self) -> set[int]:
        """Report accounts without inbound references under the current SQLite schema."""
        with self._connect() as con:
            candidates = {row[0] for row in con.execute("SELECT id FROM accounts")}
            tables = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            for table in tables:
                quoted_table = '"' + table.replace('"', '""') + '"'
                for fk in con.execute(f"PRAGMA foreign_key_list({quoted_table})"):
                    if fk[2] != "accounts" or fk[4] != "id":
                        continue
                    quoted_column = '"' + fk[3].replace('"', '""') + '"'
                    candidates.difference_update(row[0] for row in con.execute(
                        f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
                        f"WHERE {quoted_column} IS NOT NULL"
                    ))
                    if not candidates:
                        return candidates
            return candidates

    def list_accounts(self) -> list[dict]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            accounts = [dict(row) for row in con.execute("SELECT id,broker,kind,name,currency,status FROM accounts ORDER BY id")]
            numbers: dict[int, set[str]] = {}
            for row in con.execute(
                "SELECT s.account_id,json_extract(a.preview_json,'$.account_number') AS account_number "
                "FROM import_attempts a "
                "JOIN source_files s ON s.id=a.source_id WHERE a.state='committed'"
            ):
                number = row["account_number"]
                if number:
                    numbers.setdefault(row["account_id"], set()).add(number)
            for account in accounts:
                found = numbers.get(account["id"], set())
                account["account_number"] = next(iter(found)) if len(found) == 1 else None
                account["account_number_conflict"] = len(found) > 1
            return accounts

    def list_instruments(self) -> list[dict]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute("SELECT id,isin,display_name FROM instruments ORDER BY isin")]

    def create_session(self, hours: int = 12) -> tuple[str, str]:
        from datetime import timedelta

        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        with self._connect() as con:
            con.execute("DELETE FROM owner_sessions WHERE expires_at < ?", (now.isoformat(),))
            con.execute("INSERT INTO owner_sessions(token_hash,csrf_token,created_at,expires_at) VALUES(?,?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(), csrf, now.isoformat(), (now + timedelta(hours=hours)).isoformat()))
        return token, csrf

    def get_session_csrf(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._connect() as con:
            row = con.execute("SELECT csrf_token,expires_at FROM owner_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row and row[1] > datetime.now(timezone.utc).isoformat():
            return row[0]
        return None

    def delete_session(self, token: str | None) -> None:
        if token:
            with self._connect() as con:
                con.execute("DELETE FROM owner_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def list_attempts(self, account_id: int | None = None, *, limit: int | None = None, offset: int = 0) -> list[dict]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            query = "SELECT a.id,a.state,a.adapter_version,a.period_start,a.period_end,a.created_at,s.account_id,s.original_name,c.name AS account_name,json_extract(a.preview_json,'$.account_number') AS account_number FROM import_attempts a JOIN source_files s ON s.id=a.source_id JOIN accounts c ON c.id=s.account_id"
            params: tuple = ()
            if account_id is not None:
                query += " WHERE s.account_id=?"
                params = (account_id,)
            query += " ORDER BY a.id DESC"
            if limit is not None:
                if limit < 1 or offset < 0:
                    raise ValueError("Invalid attempt page")
                query += " LIMIT ? OFFSET ?"
                params += (limit, offset)
            return [dict(row) for row in con.execute(query, params)]

    def count_attempts(self, account_id: int | None = None) -> int:
        with self._connect() as con:
            if account_id is None:
                return int(con.execute("SELECT count(*) FROM import_attempts").fetchone()[0])
            return int(con.execute("SELECT count(*) FROM import_attempts a JOIN source_files s ON s.id=a.source_id "
                                   "WHERE s.account_id=?", (account_id,)).fetchone()[0])

    def list_events(self, account_id: int | None = None) -> list[dict]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            query = "SELECT e.id,e.account_id,e.event_type,e.payload_json,e.sheet,e.row_number,e.created_at,s.original_name,s.sha256,s.detected_broker,c.kind AS classified_kind,c.paired_event_id,ie.instrument_id, " \
                    "EXISTS(SELECT 1 FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id " \
                    "WHERE a.source_id=e.source_id AND a.state='committed' " \
                    "AND r.sheet=e.sheet AND r.row_number=e.row_number " \
                    "AND ((e.event_type='trade' AND r.classification IN ('trade','unparsed')) " \
                    "OR (e.event_type='cash' AND r.classification='cash_movement'))) AS source_row_verified, " \
                    "(SELECT r.raw_json FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id " \
                    "WHERE a.source_id=e.source_id AND a.state='committed' AND r.sheet=e.sheet AND r.row_number=e.row_number " \
                    "AND ((e.event_type='trade' AND r.classification IN ('trade','unparsed')) " \
                    "OR (e.event_type='cash' AND r.classification='cash_movement')) ORDER BY a.id LIMIT 1) AS source_raw_json " \
                    "FROM ledger_events e JOIN source_files s ON s.id=e.source_id LEFT JOIN cash_classifications c ON c.event_id=e.id " \
                    "LEFT JOIN instrument_events ie ON ie.event_id=e.id"
            params: tuple = ()
            if account_id is not None:
                query += " WHERE e.account_id=?"
                params = (account_id,)
            query += " ORDER BY e.id DESC"
            result = []
            for row in con.execute(query, params):
                item = dict(row)
                item["source_row_verified"] = bool(item["source_row_verified"])
                raw_json = item.pop("source_raw_json")
                if item["detected_broker"] == "alfa" and item["event_type"] == "trade":
                    item["execution_time_required"] = True
                    try:
                        item["execution_time"] = _execution_time(json.loads(raw_json)[7]) if raw_json else None
                    except (ValueError, IndexError, KeyError, TypeError):
                        item["execution_time"] = None
                item.pop("detected_broker")
                item["payload"] = json.loads(item.pop("payload_json"))
                item["source_type"] = "broker"
                item["source_reason"] = ""
                if item["classified_kind"]:
                    item["payload"]["source_kind"] = item["payload"]["kind"]
                    item["payload"]["kind"] = item["classified_kind"]
                    item["payload"]["paired_event_id"] = item["paired_event_id"]
                result.append(item)
            manual_query = "SELECT m.id,m.account_id,m.event_type,m.payload_json,m.reason,m.created_at,i.id AS instrument_id " \
                           "FROM manual_events m LEFT JOIN instruments i ON i.isin=json_extract(m.payload_json,'$.isin')"
            if account_id is not None:
                manual_query += " WHERE m.account_id=?"
            for row in con.execute(manual_query, params):
                item = dict(row)
                item.update({"id": f"M{item['id']}", "payload": json.loads(item.pop("payload_json")),
                             "source_type": "manual", "source_reason": item.pop("reason"),
                             "original_name": "Ручная корректировка", "sheet": "manual", "row_number": None,
                             "sha256": None, "classified_kind": None, "paired_event_id": None})
                result.append(item)
            transfer_query = (
                "SELECT t.id,t.source_account_id,t.destination_account_id,t.isin,t.display_name,"
                "t.effective_date,t.quantity,t.reason,t.created_at,i.id AS instrument_id "
                "FROM security_transfers t LEFT JOIN instruments i ON i.isin=t.isin"
            )
            transfer_params: tuple = ()
            if account_id is not None:
                transfer_query += " WHERE t.source_account_id=? OR t.destination_account_id=?"
                transfer_params = (account_id, account_id)
            for row in con.execute(transfer_query, transfer_params):
                transfer = dict(row)
                for direction, owner, peer, sign in (
                    ("out", transfer["source_account_id"], transfer["destination_account_id"], -1),
                    ("in", transfer["destination_account_id"], transfer["source_account_id"], 1),
                ):
                    if account_id is not None and owner != account_id:
                        continue
                    result.append({
                        "id": f"T{transfer['id']}:{direction}", "account_id": owner,
                        "event_type": "security_transfer", "instrument_id": transfer["instrument_id"],
                        "payload": {"date": transfer["effective_date"], "isin": transfer["isin"],
                                    "name": transfer["display_name"],
                                    "quantity": str(Decimal(transfer["quantity"]) * sign),
                                    "peer_account_id": peer, "transfer_id": transfer["id"],
                                    "paired_event_id": f"T{transfer['id']}:{'in' if direction == 'out' else 'out'}"},
                        "source_type": "manual", "source_reason": transfer["reason"],
                        "original_name": "Ручной перевод бумаги", "sheet": "manual",
                        "row_number": None, "sha256": None, "classified_kind": None,
                        "created_at": transfer["created_at"],
                    })
            by_id = {str(item["id"]): item for item in result}
            for item in result:
                if item["event_type"] != "cash" or item["payload"].get("kind") != "principal_action":
                    continue
                linked = by_id.get(str(item["payload"].get("cash_event_id")))
                if linked and linked["account_id"] == item["account_id"] and linked["event_type"] == "cash":
                    linked["payload"]["source_kind"] = linked["payload"]["kind"]
                    linked["payload"]["kind"] = "principal_return"
                    linked["payload"]["principal_action_id"] = item["id"]
            return sorted(result, key=lambda item: (item["created_at"], str(item["id"])), reverse=True)

    @staticmethod
    def _manual_number(value: Decimal, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
        number = Decimal(value)
        if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
            raise ValueError("Invalid manual event amount or quantity")
        return number

    @classmethod
    def _manual_event_key(cls, event_type: str, payload: dict) -> str:
        economic = dict(payload)
        for field in ("amount", "quantity", "price", "commission", "principal_fraction"):
            if field in economic:
                economic[field] = _canonical_decimal(economic[field])
        if event_type == "trade":
            economic.pop("name", None)  # ISIN, dates and economics identify the correction.
        return json.dumps(economic, ensure_ascii=False, sort_keys=True)

    def _add_manual_event(self, account_id: int, event_type: str, payload: dict, reason: str) -> int:
        if not reason.strip():
            raise ValueError("Manual correction requires a reason")
        clean_reason = reason.strip()
        event_key = self._manual_event_key(event_type, payload)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")  # Serialize semantic duplicate checks across concurrent requests.
            if con.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise ValueError("Unknown account")
            for existing in con.execute("SELECT payload_json FROM manual_events "
                                        "WHERE account_id=? AND event_type=? AND reason=?",
                                        (account_id, event_type, clean_reason)):
                try:
                    old_key = self._manual_event_key(event_type, json.loads(existing[0]))
                except (ValueError, TypeError, KeyError, InvalidOperation, json.JSONDecodeError):
                    continue  # Legacy malformed rows are audited separately.
                if old_key == event_key:
                    raise ValueError("Manual correction already exists")
            now = datetime.now(timezone.utc).isoformat()
            try:
                cur = con.execute("INSERT INTO manual_events(account_id,event_type,payload_json,reason,created_at) VALUES(?,?,?,?,?)",
                                  (account_id, event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), clean_reason, now))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Manual correction already exists or conflicts with a linked source") from exc
            if event_type == "trade" or payload.get("kind") == "principal_action":
                con.execute("INSERT OR IGNORE INTO instruments(isin,display_name,created_at) VALUES(?,?,?)",
                            (payload["isin"], payload["name"], now))
            return int(cur.lastrowid)

    def add_manual_cash(self, account_id: int, when: date, kind: str, amount: Decimal, reason: str) -> int:
        allowed = {"deposit", "withdrawal", "coupon", "dividend", "dividend_net", "tax_withheld", "principal_return",
                   "trade_cash", "trade_fee", "accrued_interest"}
        if kind not in allowed or not isinstance(when, date):
            raise ValueError("Invalid manual cash event")
        value = self._manual_number(amount)
        if value == 0 or (kind in {"deposit", "coupon", "dividend", "dividend_net", "principal_return"} and value < 0) \
                or (kind in {"withdrawal", "tax_withheld", "trade_fee"} and value > 0):
            raise ValueError("Manual cash amount conflicts with event kind")
        return self._add_manual_event(account_id, "cash", {"date": when.isoformat(), "kind": kind,
                                                            "amount": str(value), "currency": "RUB"}, reason)

    def add_manual_trade(self, account_id: int, traded_at: date, settled_at: date, isin: str, name: str,
                         quantity: Decimal, price: Decimal, amount: Decimal, commission: Decimal, reason: str) -> int:
        if (not isinstance(traded_at, date) or not isinstance(settled_at, date) or traded_at > settled_at
                or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) or not name.strip()):
            raise ValueError("Invalid manual trade identity or dates")
        qty = self._manual_number(quantity)
        unit_price = self._manual_number(price, positive=True)
        total = self._manual_number(amount, positive=True)
        fee = self._manual_number(commission, nonnegative=True)
        if qty == 0:
            raise ValueError("Manual trade quantity cannot be zero")
        if abs(abs(qty) * unit_price - total) > Decimal("0.01"):
            raise ValueError("Manual trade amount does not match quantity and RUB price")
        payload = {"broker_id": None, "traded_at": traded_at.isoformat(), "settled_at": settled_at.isoformat(),
                   "isin": isin, "name": name.strip(), "quantity": str(qty), "price": str(unit_price),
                   "amount": str(total), "commission": str(fee), "currency": "RUB"}
        return self._add_manual_event(account_id, "trade", payload, reason)

    def add_manual_principal_action(self, account_id: int, when: date, isin: str, name: str,
                                    fraction: Decimal, cash_event_id: str, reason: str) -> int:
        if (not isinstance(when, date) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin)
                or not name.strip()):
            raise ValueError("Invalid principal action identity or date")
        part = self._manual_number(fraction, positive=True)
        if part > 1:
            raise ValueError("Principal fraction cannot exceed one")
        reference = str(cash_event_id).strip()
        with self._connect() as con:
            if reference.startswith("M") and reference[1:].isdigit():
                row = con.execute("SELECT account_id,event_type,payload_json FROM manual_events WHERE id=?",
                                  (int(reference[1:]),)).fetchone()
                allowed_kind = "principal_return"
            elif reference.isdigit():
                row = con.execute("SELECT e.account_id,e.event_type,e.payload_json FROM ledger_events e "
                                  "LEFT JOIN cash_classifications c ON c.event_id=e.id "
                                  "WHERE e.id=? AND c.event_id IS NULL", (int(reference),)).fetchone()
                allowed_kind = "transfer_unclassified"
            else:
                row = None
                allowed_kind = ""
            if not row or row[0] != account_id or row[1] != "cash":
                raise ValueError("Principal action needs an unclassified broker cash row or manual principal return in the same account")
            cash = json.loads(row[2])
            if (cash.get("kind") != allowed_kind or cash.get("currency") != "RUB"
                    or cash.get("date") != when.isoformat() or Decimal(cash.get("amount", "0")) <= 0):
                raise ValueError("Linked cash event must be a positive RUB principal return on the same date")
            if con.execute("SELECT 1 FROM manual_events WHERE account_id=? AND event_type='cash' "
                           "AND json_extract(payload_json,'$.kind')='principal_action' "
                           "AND json_extract(payload_json,'$.date')=? "
                           "AND json_extract(payload_json,'$.isin')=? LIMIT 1",
                           (account_id, when.isoformat(), isin)).fetchone():
                raise ValueError("Principal action for this instrument and date already exists")
        payload = {"kind": "principal_action", "action": "redemption" if part == 1 else "amortization",
                   "date": when.isoformat(), "isin": isin, "name": name.strip(),
                   "principal_fraction": str(part), "cash_event_id": reference,
                   "amount": "0", "currency": "RUB"}
        return self._add_manual_event(account_id, "cash", payload, reason)

    @staticmethod
    def _event_effective_date(event: dict) -> date:
        payload = event["payload"]
        return date.fromisoformat(payload["settled_at"] if event["event_type"] == "trade" else payload["date"])

    def _portfolio_lot_replay(self, proposed=None, *, as_of: date | None = None):
        from .calculations import SecurityTransfer, replay_portfolio_lots
        from .metrics import _trade, principal_actions_from_events

        accounts = self.list_accounts()
        events = {account["id"]: [event for event in self.list_events(account["id"])
                                  if as_of is None or self._event_effective_date(event) <= as_of]
                  for account in accounts}
        trades = {account_id: [_trade(event) for event in items if event["event_type"] == "trade"]
                  for account_id, items in events.items()}
        actions = {}
        for account_id, items in events.items():
            actions[account_id], issues = principal_actions_from_events(items, account_id)
            if issues:
                raise ValueError("Unresolved principal action prevents security transfer")
        with self._connect() as con:
            transfers = [SecurityTransfer(identifier, date.fromisoformat(day), source, destination, isin,
                                          Decimal(quantity))
                         for identifier, day, source, destination, isin, quantity in con.execute(
                "SELECT id,effective_date,source_account_id,destination_account_id,isin,quantity "
                "FROM security_transfers ORDER BY effective_date,id"
            ) if as_of is None or date.fromisoformat(day) <= as_of]
        if proposed is not None:
            transfers.append(proposed)
        return replay_portfolio_lots(trades, actions, transfers)

    def add_security_transfer(self, source_account_id: int, destination_account_id: int, when: date,
                              isin: str, name: str, quantity: Decimal, reason: str) -> int:
        from .calculations import IncompleteHistory, SecurityTransfer

        if (source_account_id == destination_account_id or not isinstance(when, date)
                or when > datetime.now(ZoneInfo("Europe/Moscow")).date()
                or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin)
                or not name.strip() or not reason.strip()):
            raise ValueError("Invalid security transfer accounts, identity, date or reason")
        qty = self._manual_number(quantity, positive=True)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            accounts = {row[0] for row in con.execute("SELECT id FROM accounts WHERE id IN (?,?)",
                                                      (source_account_id, destination_account_id))}
            if accounts != {source_account_id, destination_account_id}:
                raise ValueError("Security transfer needs two existing accounts")
            if con.execute("SELECT 1 FROM security_transfers WHERE source_account_id=? "
                           "AND destination_account_id=? AND isin=? AND effective_date=? LIMIT 1",
                           (source_account_id, destination_account_id, isin, when.isoformat())).fetchone():
                raise ValueError("Security transfer already exists for these accounts, instrument and date")
            candidate = SecurityTransfer(-1, when, source_account_id, destination_account_id, isin, qty)
            try:
                self._portfolio_lot_replay(candidate)
            except IncompleteHistory as exc:
                raise ValueError(f"Security transfer has no confirmed lots or order: {exc}") from exc
            now = datetime.now(timezone.utc).isoformat()
            cur = con.execute(
                "INSERT INTO security_transfers(source_account_id,destination_account_id,isin,display_name,"
                "effective_date,quantity,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (source_account_id, destination_account_id, isin, name.strip(), when.isoformat(),
                 str(qty), reason.strip(), now),
            )
            con.execute("INSERT OR IGNORE INTO instruments(isin,display_name,created_at) VALUES(?,?,?)",
                        (isin, name.strip(), now))
            con.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValueError("Security transfer already exists or violates account constraints") from exc
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def classify_cash_event(self, event_id: int, kind: str, reason: str, paired_event_id: int | None = None) -> None:
        if kind not in {"deposit", "withdrawal", "internal_in", "internal_out"} or not reason.strip():
            raise ValueError("A valid classification and reason are required")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM manual_events WHERE event_type='cash' "
                           "AND json_extract(payload_json,'$.kind')='principal_action' "
                           "AND json_extract(payload_json,'$.cash_event_id')=? LIMIT 1",
                           (str(event_id),)).fetchone():
                raise ValueError("Cash event is linked to a principal action")

            def read(identifier: int):
                row = con.execute("SELECT account_id,event_type,payload_json FROM ledger_events WHERE id=?", (identifier,)).fetchone()
                if not row or row[1] != "cash":
                    raise ValueError("Cash event not found")
                payload = json.loads(row[2])
                if payload.get("kind") != "transfer_unclassified":
                    raise ValueError("Only unclassified transfers can be classified")
                return row[0], payload

            account_id, payload = read(event_id)
            amount = Decimal(payload["amount"])
            if ((kind in {"deposit", "internal_in"} and amount <= 0)
                    or (kind in {"withdrawal", "internal_out"} and amount >= 0)):
                raise ValueError("Classification conflicts with cash direction")
            existing_pair = con.execute("SELECT paired_event_id FROM cash_classifications WHERE event_id=?", (event_id,)).fetchone()
            if existing_pair and existing_pair[0] is not None:
                raise ValueError("Paired transfer cannot be changed independently")
            now = datetime.now(timezone.utc).isoformat()
            if kind.startswith("internal_"):
                if paired_event_id is None or paired_event_id == event_id:
                    raise ValueError("Internal transfer needs a distinct paired event")
                other_account, other = read(paired_event_id)
                if other_account == account_id or other["currency"] != payload["currency"] or Decimal(other["amount"]) != -amount:
                    raise ValueError("Paired transfer must have opposite amount in another account")
                if con.execute("SELECT 1 FROM cash_classifications WHERE event_id=? OR paired_event_id=?", (paired_event_id, paired_event_id)).fetchone():
                    raise ValueError("Paired event is already classified")
                other_kind = "internal_out" if kind == "internal_in" else "internal_in"
                con.execute("INSERT INTO cash_classifications(event_id,kind,paired_event_id,reason,decided_at) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(event_id) DO UPDATE SET kind=excluded.kind,paired_event_id=excluded.paired_event_id,reason=excluded.reason,decided_at=excluded.decided_at",
                            (event_id, kind, paired_event_id, reason.strip(), now))
                con.execute("INSERT INTO cash_classifications(event_id,kind,paired_event_id,reason,decided_at) VALUES(?,?,?,?,?)",
                            (paired_event_id, other_kind, event_id, reason.strip(), now))
            else:
                if paired_event_id is not None:
                    raise ValueError("External cash movement cannot have a pair")
                con.execute("INSERT INTO cash_classifications(event_id,kind,paired_event_id,reason,decided_at) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(event_id) DO UPDATE SET kind=excluded.kind,paired_event_id=NULL,reason=excluded.reason,decided_at=excluded.decided_at",
                            (event_id, kind, None, reason.strip(), now))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def latest_snapshots(self, as_of: date | None = None) -> list[dict]:
        result = []
        with self._connect() as con:
            for account in self.list_accounts():
                cutoff = " AND a.period_end<=?" if as_of is not None else ""
                row = con.execute("SELECT a.id,a.preview_json,a.source_id FROM import_attempts a JOIN source_files s ON s.id=a.source_id "
                                  "WHERE s.account_id=? AND a.state='committed'" + cutoff +
                                  " ORDER BY a.period_end DESC,a.id DESC LIMIT 1",
                                  (account["id"], as_of.isoformat()) if as_of is not None else (account["id"],)).fetchone()
                if row:
                    preview = self._effective_preview(con, row[0], row[2], account["id"], json.loads(row[1]))
                    self._attach_reported_values(con, row[0], preview)
                    result.append({"account": account, "attempt_id": row[0], "period_end": preview["period_end"],
                                   "positions": preview["positions"], "cash_snapshots": preview["cash_snapshots"],
                                   "checks": preview["checks"], "unparsed": preview["unparsed"]})
        return result

    @staticmethod
    def _attach_reported_values(con: sqlite3.Connection, attempt_id: int, preview: dict) -> None:
        """Read dated broker end values from immutable source rows, including older imports."""
        raw_rows = {}
        for sheet, row_number, raw_json in con.execute(
                "SELECT sheet,row_number,raw_json FROM source_rows "
                "WHERE attempt_id=? AND classification='position_snapshot'", (attempt_id,)):
            key = (sheet, row_number)
            if key in raw_rows:
                raw_rows[key] = None  # Ambiguous source row cannot supply a value.
            else:
                raw_rows[key] = raw_json
        for position in preview.get("positions", []):
            position.pop("reported_value_rub", None)
            location = position.get("location", {})
            raw_json = raw_rows.get((location.get("sheet"), location.get("row")))
            if not raw_json or position.get("currency", "RUB") != "RUB":
                continue
            try:
                raw = json.loads(raw_json)
                quantity = Decimal(position["end_quantity"])
                if preview["broker"] == "sber":
                    if (len(raw) != 18 or str(raw[1]).strip() != position.get("isin")
                            or str(raw[2]).strip() != "RUB" or _n(raw[8]) != quantity):
                        continue
                    value = _n(raw[11]) + _n(raw[12])  # End market value plus accrued interest.
                elif preview["broker"] == "alfa":
                    if (str(_get(raw, 6)).strip() != position.get("name")
                            or _num(_get(raw, 15)) != quantity):
                        continue
                    value = _num(_get(raw, 17))  # Broker's end-of-period RUB valuation.
                else:
                    continue
                if not value.is_finite() or value < 0 or (quantity != 0 and value == 0):
                    continue
                position["reported_value_rub"] = str(value)
            except (ValueError, TypeError, IndexError, KeyError, InvalidOperation, json.JSONDecodeError):
                continue

    @staticmethod
    def _effective_preview(con: sqlite3.Connection, attempt_id: int, source_id: int,
                           account_id: int, preview: dict) -> dict:
        """Resolve a summary warning only when its recorded action has ledger evidence."""
        summaries = preview.get("summary_trades", [])
        if not summaries:
            return preview
        decisions = {row[0] for row in con.execute(
            "SELECT action FROM audit_decisions WHERE attempt_id=? AND action LIKE 'summary:%'", (attempt_id,))}
        detailed = [(event_id, json.loads(payload)) for event_id, payload in con.execute(
            "SELECT id,payload_json FROM ledger_events WHERE account_id=? AND source_id<>? "
            "AND event_type='trade' AND broker_event_id IS NOT NULL", (account_id, source_id))]
        resolved = set()
        used_details = set()
        for summary in summaries:
            loc = summary["location"]
            prefix = f"summary:{loc['sheet']}:{loc['row']}"
            if prefix + ":promote" in decisions:
                if con.execute(
                    "SELECT 1 FROM ledger_events WHERE account_id=? AND source_id=? AND sheet=? "
                    "AND row_number=? AND event_type='trade' "
                    "AND json_extract(payload_json,'$.aggregate_from_report')=1 LIMIT 1",
                    (account_id, source_id, loc["sheet"], loc["row"]),
                ).fetchone():
                    resolved.add((loc["sheet"], loc["row"]))
            elif prefix + ":control_only" in decisions:
                candidates = [(event_id, item) for event_id, item in detailed
                              if event_id not in used_details
                              if item["isin"] == summary["isin"]
                              and item["traded_at"] == summary["traded_at"]
                              and item["settled_at"] == summary["settled_at"]
                              and (Decimal(item["quantity"]) > 0) == (Decimal(summary["quantity"]) > 0)
                              and not item.get("aggregate_from_report")]
                if candidates and all(
                    sum((Decimal(item.get(field, "0")) for _, item in candidates), Decimal(0)) == Decimal(summary.get(field, "0"))
                    for field in ("quantity", "amount", "commission", "accrued_interest")
                ):
                    resolved.add((loc["sheet"], loc["row"]))
                    used_details.update(event_id for event_id, _ in candidates)
        if not resolved:
            return preview
        effective = dict(preview)
        effective["unparsed"] = [item for item in preview["unparsed"]
                                 if (item["location"]["sheet"], item["location"]["row"]) not in resolved
                                 or "trade without broker ID" not in item["reason"]]
        effective["checks"] = [item for item in preview["checks"] if item["kind"] != "completeness"]
        if effective["unparsed"]:
            effective["checks"].append({"kind": "completeness", "key": "unparsed", "calculated": None,
                                        "reported": None, "difference": None, "status": "unavailable",
                                        "reason": f"{len(effective['unparsed'])} source rows need review"})
        return effective

    def confirm_account_history(self, account_id: int, from_date: date, reason: str) -> None:
        if not reason.strip():
            raise ValueError("A reason for complete history is required")
        with self._connect() as con:
            if con.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise ValueError("Unknown account")
            con.execute("INSERT INTO account_history(account_id,from_date,reason,confirmed_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(account_id) DO UPDATE SET from_date=excluded.from_date,reason=excluded.reason,confirmed_at=excluded.confirmed_at",
                        (account_id, from_date.isoformat(), reason.strip(), datetime.now(timezone.utc).isoformat()))

    def confirm_inactive_period(self, account_id: int, start: date, end: date, reason: str) -> int:
        if end < start or not reason.strip():
            raise ValueError("An ordered period and reason are required")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise ValueError("Unknown account")
            if con.execute("SELECT 1 FROM inactive_periods WHERE account_id=? AND revoked_at IS NULL "
                           "AND start_date<=? AND end_date>=?", (account_id, end.isoformat(), start.isoformat())).fetchone():
                raise ValueError("An active inactive-period confirmation already overlaps these dates")
            cur = con.execute("INSERT INTO inactive_periods(account_id,start_date,end_date,reason,confirmed_by,confirmed_at) "
                              "VALUES(?,?,?,?,?,?)", (account_id, start.isoformat(), end.isoformat(),
                              reason.strip(), "account_owner", datetime.now(timezone.utc).isoformat()))
            return int(cur.lastrowid)

    def revoke_inactive_period(self, account_id: int, period_id: int) -> None:
        with self._connect() as con:
            cur = con.execute("UPDATE inactive_periods SET revoked_at=? WHERE id=? AND account_id=? AND revoked_at IS NULL",
                              (datetime.now(timezone.utc).isoformat(), period_id, account_id))
            if cur.rowcount != 1:
                raise ValueError("Unknown active inactive-period confirmation")

    def list_inactive_periods(self, account_id: int, *, active_only: bool = False) -> list[dict]:
        with self._connect() as con:
            if con.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise ValueError("Unknown account")
            rows = con.execute("SELECT id,start_date,end_date,reason,confirmed_by,confirmed_at,revoked_at "
                               "FROM inactive_periods WHERE account_id=? "
                               + ("AND revoked_at IS NULL " if active_only else "") + "ORDER BY start_date,id",
                               (account_id,)).fetchall()
        return [dict(zip(("id", "start_date", "end_date", "reason", "confirmed_by", "confirmed_at", "revoked_at"), row))
                for row in rows]

    def committed_reports(self, account_id: int) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT a.id,a.preview_json,a.source_id FROM import_attempts a JOIN source_files s ON s.id=a.source_id "
                               "WHERE s.account_id=? AND a.state='committed' ORDER BY a.period_start,a.period_end,a.id", (account_id,)).fetchall()
            result = []
            for attempt_id, payload, source_id in rows:
                item = self._effective_preview(con, attempt_id, source_id, account_id, json.loads(payload))
                self._attach_reported_values(con, attempt_id, item)
                item["attempt_id"] = attempt_id
                result.append(item)
        return result

    def financial_metrics(self, as_of: date | None = None) -> list[dict]:
        from .metrics import evaluate_account, recorded_income

        results = []
        with self._connect() as con:
            has_transfers = con.execute(
                "SELECT 1 FROM security_transfers WHERE ? IS NULL OR effective_date<=? LIMIT 1",
                (as_of.isoformat() if as_of else None, as_of.isoformat() if as_of else None),
            ).fetchone() is not None
        accounting = {}
        accounting_error = None
        if has_transfers:
            try:
                accounting = self._portfolio_lot_replay(as_of=as_of)
            except ValueError as exc:
                accounting_error = str(exc)
        transfer_prices = {}
        if has_transfers:
            with self._connect() as con:
                transfers = con.execute("SELECT id,isin,effective_date FROM security_transfers "
                                        "WHERE ? IS NULL OR effective_date<=?",
                                        (as_of.isoformat() if as_of else None,
                                         as_of.isoformat() if as_of else None)).fetchall()
            for transfer_id, isin, day in transfers:
                quote = self.latest_prices(date.fromisoformat(day)).get(isin)
                if quote:
                    transfer_prices[transfer_id] = quote
        for account in self.list_accounts():
            reports = [report for report in self.committed_reports(account["id"])
                       if as_of is None or date.fromisoformat(report["period_end"]) <= as_of]
            last_date = date.fromisoformat(max(r["period_end"] for r in reports)) if reports else None
            events = [event for event in self.list_events(account["id"])
                      if as_of is None or self._event_effective_date(event) <= as_of]
            with self._connect() as con:
                row = con.execute("SELECT from_date FROM account_history WHERE account_id=?", (account["id"],)).fetchone()
            history_from = date.fromisoformat(row[0]) if row else None
            inactive_periods = self.list_inactive_periods(account["id"], active_only=True)
            if as_of is not None and last_date != as_of:
                missing = evaluate_account(account, events, [], {}, history_from)
                missing["as_of"] = as_of
                missing["reasons"] = [f"нет подтверждённого отчёта с конечным остатком на {as_of.isoformat()}"]
                missing["income"] = recorded_income(events, as_of)
                results.append(missing)
            else:
                results.append(evaluate_account(account, events, reports,
                                                self.latest_prices(last_date), history_from,
                                                accounting.get(account["id"]) if has_transfers else None,
                                                accounting_error, transfer_prices, inactive_periods))
        return results

    def event_source(self, event_id: int) -> tuple[Path, str] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT s.private_path,s.original_name,s.sha256,s.size,s.detected_broker "
                "FROM ledger_events e JOIN source_files s ON s.id=e.source_id WHERE e.id=?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        private_path, original_name, digest, size, broker = row
        path = Path(private_path)
        suffixes = {"sber": {".html"}, "alfa": {".xls", ".xlsx"}}
        if (not re.fullmatch(r"[0-9a-f]{64}", digest) or broker not in suffixes
                or path.is_symlink() or path.parent.resolve() != self.files.resolve()
                or path.name not in {f"{digest}{suffix}" for suffix in suffixes[broker]}
                or not path.is_file() or path.stat().st_size != size):
            raise ValueError("Stored source failed integrity check")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Stored source failed integrity check")
        return path, original_name

    def add_price(self, isin: str, amount: Decimal, as_of: date, *, source: str = "manual", note: str = "") -> int:
        import re

        if (not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) or not amount.is_finite()
                or amount <= 0 or source not in {"manual", "moex"}
                or as_of > datetime.now(ZoneInfo("Europe/Moscow")).date()):
            raise ValueError("Invalid RUB price, ISIN, source or date")
        with self._connect() as con:
            cur = con.execute("INSERT INTO prices(isin,amount,currency,source,as_of,fetched_at,note) VALUES(?,?,?,?,?,?,?)",
                              (isin, str(amount), "RUB", source, as_of.isoformat(), datetime.now(timezone.utc).isoformat(), note.strip()))
            return int(cur.lastrowid)

    def add_moex_prices_batch(self, values: list[tuple[MoexSecurity, MoexQuote]]) -> int:
        """Save a fully fetched dated quote set in one transaction."""
        if not values or len(values) > 100 or len({security.isin for security, _ in values}) != len(values):
            raise ValueError("Price batch must contain 1–100 different instruments")
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        rows = []
        for security, quote in values:
            if (quote.isin != security.isin or quote.secid != security.secid
                    or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", quote.isin)
                    or not quote.amount.is_finite() or quote.amount <= 0 or quote.as_of > today):
                raise ValueError("Price batch contains an invalid or mismatched RUB quote")
            method = ("CLOSE RUB" if security.kind == "share" else
                      "dirty RUB = CLOSE percent * FACEVALUE / 100 + ACCINT")
            rows.append((quote.isin, str(quote.amount), "RUB", "moex", quote.as_of.isoformat(),
                         f"{security.board} {security.secid}; {method}; {quote.url}"))
        inserted = 0
        with self._connect() as con:
            now = datetime.now(timezone.utc).isoformat()
            for isin, amount, currency, source, as_of, note in rows:
                if con.execute("SELECT 1 FROM prices WHERE isin=? AND amount=? AND source=? "
                               "AND as_of=? AND note=? LIMIT 1",
                               (isin, amount, source, as_of, note)).fetchone():
                    continue
                con.execute("INSERT INTO prices(isin,amount,currency,source,as_of,fetched_at,note) "
                            "VALUES(?,?,?,?,?,?,?)", (isin, amount, currency, source, as_of, now, note))
                inserted += 1
        return inserted

    def latest_prices(self, as_of: date | None = None) -> dict[str, dict]:
        cutoff = (as_of or datetime.now(ZoneInfo("Europe/Moscow")).date()).isoformat()
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT isin,amount,currency,source,as_of,fetched_at,note FROM prices WHERE as_of<=? "
                               "ORDER BY isin,as_of DESC,CASE source WHEN 'manual' THEN 0 ELSE 1 END,id DESC", (cutoff,)).fetchall()
        prices = {}
        for row in rows:
            prices.setdefault(row["isin"], dict(row))
        return prices

    def portfolio_estimate(self, as_of: date | None = None) -> dict:
        valuation_date = as_of
        if valuation_date is None:
            latest = self.latest_snapshots()
            valuation_date = max((date.fromisoformat(item["period_end"]) for item in latest), default=None)
        snapshots = self.latest_snapshots(valuation_date) if valuation_date is not None else []
        prices = self.latest_prices(valuation_date) if valuation_date is not None else {}
        positions = []
        subtotal = Decimal(0)
        report_dates = sorted({snapshot["period_end"] for snapshot in snapshots})
        mixed_report_dates = len(report_dates) > 1
        reported_account_ids = {snapshot["account"]["id"] for snapshot in snapshots}
        unreported_accounts = [account for account in self.list_accounts() if account["id"] not in reported_account_ids]
        missing_accounts = len(unreported_accounts)
        stale_position_accounts = sum(snapshot["period_end"] != valuation_date.isoformat() for snapshot in snapshots)
        incomplete = not snapshots or mixed_report_dates or missing_accounts > 0 or stale_position_accounts > 0
        for snapshot in snapshots:
            account_incomplete = (bool(snapshot["unparsed"]) or not snapshot["checks"]
                                  or any(check["status"] != "matched" for check in snapshot["checks"]))
            incomplete |= account_incomplete
            for position in snapshot["positions"]:
                isin = position["isin"]
                price = prices.get(isin) if isin else None
                quantity = Decimal(position["end_quantity"])
                currency = position.get("currency", "RUB")
                age = price_age_days(price["as_of"], valuation_date) if price else None
                reported_value = position.get("reported_value_rub")
                if quantity == 0:
                    value = Decimal(0)
                    price_status = "zero_position"
                elif currency != "RUB":
                    value = None
                    price_status = "currency_conversion_required"
                elif (snapshot["period_end"] == valuation_date.isoformat() and reported_value is not None
                      and not (price and price.get("source") == "manual"
                               and price["as_of"] == valuation_date.isoformat())):
                    value = Decimal(reported_value)
                    price_status = "broker_report"
                    price = None  # The source gives a position value, not a unit quote.
                    age = None
                elif price and age is not None and 0 <= age <= MAX_PRICE_AGE_DAYS:
                    value = quantity * Decimal(price["amount"])
                    price_status = "fresh"
                else:
                    value = None
                    price_status = "stale" if price else "missing"
                if value is None:
                    incomplete = True
                if value is not None:
                    subtotal += value
                positions.append({"account": snapshot["account"], "name": position["name"], "isin": isin,
                                  "quantity": quantity, "currency": currency, "price": price, "price_status": price_status,
                                  "price_age_days": age, "value": value, "report_date": snapshot["period_end"],
                                  "account_incomplete": account_incomplete})
        return {"positions": positions, "known_securities_subtotal": subtotal, "incomplete": incomplete,
                "no_reports": not snapshots,
                "as_of": valuation_date.isoformat() if valuation_date is not None else None,
                "report_dates": report_dates, "mixed_report_dates": mixed_report_dates,
                "missing_accounts": missing_accounts, "unreported_accounts": unreported_accounts,
                "stale_position_accounts": stale_position_accounts,
                "missing_prices": sum(p["price_status"] == "missing" for p in positions),
                "stale_prices": sum(p["price_status"] == "stale" for p in positions),
                "conversion_required": sum(p["price_status"] == "currency_conversion_required" for p in positions),
                "max_price_age_days": MAX_PRICE_AGE_DAYS}

    def stage_file(self, account_id: int, path: Path, *, original_name: str | None = None) -> tuple[int, dict]:
        with self._file_lock():
            return self._stage_file_locked(account_id, path, original_name=original_name)

    def _stage_file_locked(self, account_id: int, path: Path, *, original_name: str | None = None) -> tuple[int, dict]:
        size = path.stat().st_size
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise ValueError("Report size outside allowed range")
        broker, suffix = _detect(path)
        with self._connect() as con:
            account = con.execute("SELECT broker,kind FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None or account[0] != broker:
                raise ValueError("Report broker does not match account")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        target = self.files / f"{digest}{suffix}"
        created = False
        if not target.exists():
            try:
                with target.open("xb") as out, path.open("rb") as src:
                    shutil.copyfileobj(src, out)
                created = True
                os.chmod(target, 0o600)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                if created:
                    target.unlink(missing_ok=True)
                raise ValueError("Source file changed during staging")
        try:
            report = (parse_sber if broker == "sber" else parse_alfa)(target)
            if report.account_kind and report.account_kind != account[1]:
                raise ValueError("Report contract kind does not match the selected account")
            source_rows = _source_rows(target, broker, report)
            with self._connect() as con:
                bound = con.execute("SELECT identity_hash FROM account_identities WHERE account_id=?", (account_id,)).fetchone()
            if bound and report.account_key != bound[0]:
                raise ValueError("Report contract does not match the selected account")
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise
        checks = reconcile(report)
        preview = {
            "broker": broker,
            "variant": report.variant,
            "account_key": report.account_key,
            "account_kind": report.account_kind,
            "account_number": report.account_number,
            "warnings": report.warnings,
            "period_start": report.period_start,
            "period_end": report.period_end,
            "trades": [_trade_preview(item) for item in report.trades],
            "summary_trades": [_trade_preview(item) for item in report.summary_trades],
            "cash_movements": [asdict(item) for item in report.cash_movements],
            "positions": [asdict(item) for item in report.positions],
            "cash_snapshots": [asdict(item) for item in report.cash_snapshots],
            "cash_flow_subtotals": [asdict(item) for item in report.cash_flow_subtotals],
            "source_totals": [asdict(item) for item in report.source_totals],
            "unparsed": [asdict(item) for item in report.unparsed],
            "excluded": [asdict(item) for item in report.excluded],
            "checks": [asdict(item) for item in checks],
        }
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as con:
                row = con.execute("SELECT id FROM source_files WHERE account_id=? AND sha256=?", (account_id, digest)).fetchone()
                if row:
                    source_id = row[0]
                else:
                    cur = con.execute("INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) VALUES(?,?,?,?,?,?,?)",
                                      (account_id, digest, Path((original_name or path.name).replace("\\", "/")).name[:255], str(target), size, broker, now))
                    source_id = cur.lastrowid
                cur = con.execute("INSERT INTO import_attempts(source_id,state,adapter_version,period_start,period_end,preview_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                  (source_id, "awaiting_review", report.variant, report.period_start.isoformat(), report.period_end.isoformat(),
                                   json.dumps(preview, default=_json_default, ensure_ascii=False), now))
                attempt_id = int(cur.lastrowid)
                con.executemany("INSERT INTO source_rows(attempt_id,sheet,row_number,raw_json,classification,warning) VALUES(?,?,?,?,?,?)",
                                ((attempt_id, *item) for item in source_rows))
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise
        return attempt_id, preview

    @staticmethod
    def _event_items(preview: dict, *, promoted_summaries: set[tuple[str, int]] | None = None) -> list[dict]:
        items: list[dict] = []
        cash_references = Counter(
            (item["kind"], item["broker_trade_id"])
            for item in preview["cash_movements"]
            if item.get("broker_trade_id") and item["kind"] in {"trade_cash", "trade_fee", "accrued_interest"}
        )
        for kind, collection in (("trade", "trades"), ("cash", "cash_movements"),
                                 ("trade", "summary_trades")):
            if collection == "summary_trades" and not promoted_summaries:
                continue
            for item in preview.get(collection, []):
                location = item["location"]
                if collection == "summary_trades" and (location["sheet"], location["row"]) not in promoted_summaries:
                    continue
                payload = _canonical_event_payload(kind, {key: value for key, value in item.items()
                                                          if key not in {"location", "execution_time", "execution_time_required"}})
                if collection == "summary_trades":
                    payload["aggregate_from_report"] = True
                canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                fingerprint = hashlib.sha256((kind + ":" + canonical).encode()).hexdigest()
                cash_reference = None
                if kind == "cash" and item.get("broker_trade_id"):
                    reference = (item["kind"], item["broker_trade_id"])
                    if cash_references[reference] == 1:
                        cash_reference = reference
                event_id = (str(item["broker_id"]) if kind == "trade" and item["broker_id"] else
                            f"cash:{cash_reference[0]}:{cash_reference[1]}" if cash_reference else None)
                component = payload.get("component", "") if kind == "cash" else ""
                items.append({"kind": kind, "sheet": location["sheet"], "row": location["row"],
                              "payload": canonical, "fingerprint": fingerprint, "broker_id": event_id,
                              "cash_reference": cash_reference, "component": component,
                              "decision_key": (f"event:{kind}:{location['sheet']}:{location['row']}"
                                               + (f":{component}" if component else ""))})
        return items

    @staticmethod
    def _existing_events(con: sqlite3.Connection, account_id: int, item: dict) -> list[tuple]:
        if item["broker_id"] is not None:
            existing = con.execute("SELECT id,payload_json,source_id FROM ledger_events WHERE account_id=? AND event_type=? AND broker_event_id=? ORDER BY id",
                                   (account_id, item["kind"], item["broker_id"])).fetchall()
            if not existing and item["cash_reference"]:
                # Previous schema stored Alfa cash references only inside payload_json.
                cash_kind, reference = item["cash_reference"]
                existing = con.execute("SELECT id,payload_json,source_id FROM ledger_events WHERE account_id=? AND event_type='cash' "
                                       "AND json_extract(payload_json,'$.kind')=? AND json_extract(payload_json,'$.broker_trade_id')=? ORDER BY id",
                                       (account_id, cash_kind, reference)).fetchall()
            return existing
        existing = con.execute("SELECT id,payload_json,source_id FROM ledger_events WHERE account_id=? AND event_type=? AND fingerprint=? ORDER BY id",
                               (account_id, item["kind"], item["fingerprint"])).fetchall()
        if existing or item["kind"] != "cash":
            return existing
        # Older ledgers can contain the same Decimal as 10.0 instead of 10.
        # Narrow the fallback before comparing the complete normalized payload.
        payload = json.loads(item["payload"])
        possible = con.execute(
            "SELECT id,payload_json,source_id FROM ledger_events WHERE account_id=? AND event_type='cash' "
            "AND broker_event_id IS NULL AND json_extract(payload_json,'$.kind')=? "
            "AND json_extract(payload_json,'$.date')=? AND json_extract(payload_json,'$.currency')=? ORDER BY id",
            (account_id, payload["kind"], payload["date"], payload["currency"]),
        ).fetchall()
        return [old for old in possible if Storage._same_event_payload("cash", old[1], item["payload"])]

    @staticmethod
    def _same_event_payload(kind: str, old_json: str, new_json: str) -> bool:
        if old_json == new_json:
            return True
        old = _canonical_event_payload(kind, json.loads(old_json))
        new = _canonical_event_payload(kind, json.loads(new_json))
        if kind == "trade":
            # A report can rename the same ISIN without changing the economic trade.
            # Keep the original text in each source preview.
            old.pop("name", None)
            new.pop("name", None)
        return old == new

    def _reclassified_sber_source_conflicts(self, con: sqlite3.Connection, account_id: int,
                                            attempt_id: int, item: dict) -> list[int]:
        """Find older semantics for the same source cells in another report.

        A changed kind has a changed fingerprint, so normal duplicate matching
        cannot find the former event in an overlapping report.
        """
        if item["kind"] != "cash" or item["component"]:
            return []
        payload = json.loads(item["payload"])
        if payload.get("kind") not in {"coupon", "tax_withheld", "dividend"}:
            return []
        current = con.execute(
            "SELECT raw_json FROM source_rows WHERE attempt_id=? AND sheet=? AND row_number=? "
            "AND classification='cash_movement' LIMIT 1",
            (attempt_id, item["sheet"], item["row"]),
        ).fetchone()
        if current is None:
            raise ValueError("Current Sber cash source row is unavailable")
        current_cells = json.loads(current[0])
        candidates = con.execute(
            "SELECT e.id,e.payload_json,s.private_path,s.sha256,e.sheet,e.row_number, "
            "(SELECT r.raw_json FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id "
            "WHERE a.source_id=e.source_id AND r.sheet=e.sheet AND r.row_number=e.row_number "
            "AND r.classification='cash_movement' ORDER BY a.id LIMIT 1) FROM ledger_events e "
            "JOIN source_files s ON s.id=e.source_id "
            "WHERE e.account_id=? AND e.event_type='cash' AND s.detected_broker='sber' "
            "AND json_extract(e.payload_json,'$.date')=?",
            (account_id, payload["date"]),
        ).fetchall()
        conflicts = []
        for event_id, old_payload, private_path, digest, sheet, row_number, old_raw in candidates:
            if json.loads(old_payload).get("component", "") != item["component"]:
                continue
            if old_raw is None:
                # Imports made before source-cell storage still retain their
                # private original file. Read only its recorded row and fail
                # closed if the original cannot be trusted.
                path = Path(private_path)
                if (path.is_symlink() or path.parent.resolve() != self.files.resolve()
                        or path.name != f"{digest}.html" or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise ValueError("Legacy Sber source path is unsafe")
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                        raise ValueError("Legacy Sber source has changed")
                from bs4 import BeautifulSoup
                tables = BeautifulSoup(path.read_bytes(), "html.parser").find_all("table")
                try:
                    old_cells = _cells(tables[int(sheet.removeprefix("table:"))].find_all("tr")[row_number - 1])
                except (ValueError, IndexError) as exc:
                    raise ValueError("Legacy Sber source row is unavailable") from exc
            else:
                old_cells = json.loads(old_raw)
            if (raw_equivalent(current_cells, old_cells)
                    and not self._same_event_payload("cash", old_payload, item["payload"])):
                conflicts.append(event_id)
        return sorted(set(conflicts))

    @staticmethod
    def _required_review(preview: dict) -> set[str]:
        required = {f"unparsed:{item['location']['sheet']}:{item['location']['row']}" for item in preview["unparsed"]}
        required.update(f"summary:{item['location']['sheet']}:{item['location']['row']}"
                        for item in preview.get("summary_trades", []))
        required.update(f"check:{check['kind']}:{check['key']}" for check in preview["checks"]
                        if check["status"] != "matched" and check["kind"] != "completeness")
        return required

    @staticmethod
    def _cross_account_trade_ids(con: sqlite3.Connection, account_id: int, broker: str, item: dict) -> list[int]:
        if item["kind"] != "trade" or item["broker_id"] is None:
            return []
        return [event_id for (event_id,) in con.execute(
            "SELECT e.id FROM ledger_events e JOIN accounts a ON a.id=e.account_id "
            "WHERE e.account_id<>? AND a.broker=? AND e.event_type='trade' "
            "AND e.broker_event_id=? AND e.fingerprint=? ORDER BY e.id",
            (account_id, broker, item["broker_id"], item["fingerprint"]),
        )]

    @staticmethod
    def _other_account_with_identity(con: sqlite3.Connection, account_id: int, broker: str,
                                     kind: str, identity_hash: str) -> bool:
        return con.execute(
            "SELECT 1 FROM account_identities i JOIN accounts a ON a.id=i.account_id "
            "WHERE i.account_id<>? AND a.broker=? AND a.kind=? AND i.identity_hash=? LIMIT 1",
            (account_id, broker, kind, identity_hash),
        ).fetchone() is not None

    def review_attempt(self, attempt_id: int) -> dict:
        with self._connect() as con:
            row = con.execute("SELECT a.preview_json,a.state,s.account_id,s.id,a.adapter_version FROM import_attempts a JOIN source_files s ON s.id=a.source_id WHERE a.id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown import attempt")
            preview = json.loads(row[0])
            selected_account = con.execute("SELECT id,name,broker,kind FROM accounts WHERE id=?",
                                           (row[2],)).fetchone()
            if selected_account is None:
                raise ValueError("Import attempt has no selected account")
            requires_restage = (preview.get("broker") == "sber" and row[4] != SBER_ADAPTER_VERSION or
                                preview.get("broker") == "alfa" and row[4] != ALFA_ADAPTER_VERSION)
            ambiguities = []
            candidates = []
            event_items = self._event_items(preview)
            for item in event_items:
                existing = self._existing_events(con, row[2], item)
                cross_account = self._cross_account_trade_ids(con, row[2], preview["broker"], item)
                reclassified = (self._reclassified_sber_source_conflicts(con, row[2], attempt_id, item)
                                if preview["broker"] == "sber" else [])
                if cross_account:
                    status = "conflict"
                    ambiguities.append({"key": item["decision_key"],
                                        "reason": "trade already recorded under another account; verify contract identity",
                                        "existing_ids": cross_account, "blocking": True})
                elif reclassified:
                    status = "conflict"
                    ambiguities.append({"key": item["decision_key"], "reason": "source cells already have a different cash event",
                                        "existing_ids": reclassified})
                elif existing and not all(self._same_event_payload(item["kind"], old[1], item["payload"]) for old in existing):
                    status = "conflict"
                    ambiguities.append({"key": item["decision_key"], "reason": "same broker ID has different payload", "existing_ids": [old[0] for old in existing]})
                elif len(existing) > 1 and item["broker_id"] is not None:
                    status = "ambiguous"
                    ambiguities.append({"key": item["decision_key"], "reason": "broker reference matches multiple existing events", "existing_ids": [old[0] for old in existing]})
                elif existing and item["broker_id"] is None and any(old[2] != row[3] for old in existing):
                    status = "ambiguous"
                    ambiguities.append({"key": item["decision_key"], "reason": "unkeyed event also appears in another file", "existing_ids": [old[0] for old in existing]})
                elif existing:
                    status = "duplicate"
                else:
                    status = "new"
                candidates.append({"key": item["decision_key"], "status": status,
                                   "existing_ids": cross_account or reclassified or [old[0] for old in existing]})
            for ambiguity in ambiguities:
                options = []
                sheet, row_number = next((item["sheet"], item["row"]) for item in event_items
                                         if item["decision_key"] == ambiguity["key"])
                current_raw_row = con.execute(
                    "SELECT raw_json FROM source_rows WHERE attempt_id=? AND sheet=? AND row_number=? "
                    "AND classification IN ('cash_movement','trade') LIMIT 1",
                    (attempt_id, sheet, row_number),
                ).fetchone()
                for event_id in ambiguity["existing_ids"]:
                    source = con.execute(
                        "SELECT e.id,s.original_name,e.sheet,e.row_number,e.source_id FROM ledger_events e "
                        "JOIN source_files s ON s.id=e.source_id WHERE e.id=?", (event_id,)
                    ).fetchone()
                    old_raw_row = con.execute(
                        "SELECT r.raw_json FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id "
                        "WHERE a.source_id=? AND r.sheet=? AND r.row_number=? "
                        "AND r.classification IN ('cash_movement','trade') ORDER BY a.id LIMIT 1",
                        (source[4], source[2], source[3]),
                    ).fetchone()
                    raw_match = (raw_equivalent(json.loads(current_raw_row[0]), json.loads(old_raw_row[0]))
                                 if current_raw_row and old_raw_row else None)
                    options.append({"id": source[0], "file": source[1], "sheet": source[2], "row": source[3],
                                    "raw_match": raw_match,
                                    "same_location": (sheet, row_number) == (source[2], source[3])})
                options.sort(key=lambda option: (option["raw_match"] is True,
                                                 option["same_location"], -option["id"]), reverse=True)
                ambiguity["existing_options"] = options
            instrument_reference_reason = "instrument reference used for ISIN mapping; no ledger event"
            instrument_references = [item for item in preview["excluded"]
                                     if item["reason"] in {instrument_reference_reason, ISIN_CONTINUATION_REASON}]
            structural_rows = [item for item in preview["excluded"]
                               if item["reason"] in {SERVICE_REASON, HEADER_REASON, POSITION_FOOTNOTE_REASON,
                                                     TAX_HEADER_REASON, TRADE_FOOTNOTE_REASON, EMPTY_TRADE_SHEET_REASON,
                                                     CASH_SECTION_REASON, NONTRADE_METADATA_REASON,
                                                     EMPTY_NONTRADE_SHEET_REASON}]
            total_rows = [item for item in preview["excluded"]
                          if item["reason"] in {TOTAL_REASON, OVERVIEW_REASON, ALFA_TOTAL_REASON,
                                                POSITION_TOTAL_REASON, CASH_SUMMARY_REASON}]
            other_excluded = [item for item in preview["excluded"]
                              if item["reason"] not in {instrument_reference_reason, ISIN_CONTINUATION_REASON,
                                                        SERVICE_REASON, HEADER_REASON, POSITION_FOOTNOTE_REASON,
                                                        TAX_HEADER_REASON, TRADE_FOOTNOTE_REASON, EMPTY_TRADE_SHEET_REASON,
                                                        CASH_SECTION_REASON, NONTRADE_METADATA_REASON,
                                                        EMPTY_NONTRADE_SHEET_REASON, TOTAL_REASON, OVERVIEW_REASON,
                                                        ALFA_TOTAL_REASON, POSITION_TOTAL_REASON, CASH_SUMMARY_REASON}]
            diagnostic_locations = {(item["location"]["sheet"], item["location"]["row"])
                                    for item in preview["unparsed"] + other_excluded}
            by_decision_key = {item["decision_key"]: (item["sheet"], item["row"]) for item in event_items}
            diagnostic_locations.update(by_decision_key[item["key"]] for item in ambiguities)
            diagnostic_rows = [dict(sheet=sheet, row=row_number, raw=json.loads(raw_json),
                                    classification=classification, warning=warning)
                               for sheet, row_number, raw_json, classification, warning in con.execute(
                                   "SELECT sheet,row_number,raw_json,classification,warning FROM source_rows WHERE attempt_id=? ORDER BY id", (attempt_id,))
                               if (sheet, row_number) in diagnostic_locations]
            reference_rows = [dict(sheet=sheet, row=row_number, raw=json.loads(raw_json))
                              for sheet, row_number, raw_json in con.execute(
                                  "SELECT sheet,row_number,raw_json FROM source_rows "
                                  "WHERE attempt_id=? AND classification='excluded' AND warning IN (?,?) ORDER BY id",
                                  (attempt_id, instrument_reference_reason, ISIN_CONTINUATION_REASON))]
            account_conflicts = []
            account_kind = con.execute("SELECT kind FROM accounts WHERE id=?", (row[2],)).fetchone()[0]
            if (preview.get("account_key") and
                    self._other_account_with_identity(con, row[2], preview["broker"],
                                                      account_kind, preview["account_key"])):
                account_conflicts.append({"key": "account_identity", "blocking": True,
                                          "reason": "contract is already bound to another account of the same type"})
            return {"state": row[1], "selected_account": dict(zip(("id", "name", "broker", "kind"), selected_account)),
                    "required_decisions": sorted(self._required_review(preview)),
                    "ambiguous_events": ambiguities, "event_candidates": candidates,
                    "blocking_conflicts": [item for item in ambiguities if item.get("blocking")] + account_conflicts,
                    "diagnostic_rows": diagnostic_rows, "instrument_references": instrument_references,
                    "structural_rows": structural_rows,
                    "total_rows": total_rows,
                    "other_excluded": other_excluded, "reference_rows": reference_rows,
                    "requires_restage": requires_restage, "preview": preview}

    def confirm_attempt(self, attempt_id: int, decisions: dict[str, dict[str, Any]] | None = None) -> dict[str, int]:
        """Commit atomically. Ambiguous matches and incomplete data require a reasoned decision."""
        decisions = decisions or {}
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT a.preview_json,a.state,s.account_id,s.id,a.adapter_version FROM import_attempts a JOIN source_files s ON s.id=a.source_id WHERE a.id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown import attempt")
            preview_json, state, account_id, source_id, adapter_version = row
            if state == "committed":
                con.rollback()
                return {"inserted": 0, "observed_duplicates": 0}
            if state != "awaiting_review":
                raise ValueError("Import attempt is not awaiting review")
            preview = json.loads(preview_json)
            if ((preview.get("broker") == "sber" and adapter_version != SBER_ADAPTER_VERSION) or
                    (preview.get("broker") == "alfa" and adapter_version != ALFA_ADAPTER_VERSION)):
                raise ValueError("Restage broker report with the current adapter to classify source rows")
            bound = con.execute("SELECT identity_hash FROM account_identities WHERE account_id=?", (account_id,)).fetchone()
            candidate_key = preview.get("account_key")
            if not candidate_key:
                raise ValueError("Restage report with the current adapter to verify contract identity")
            detected_kind = preview.get("account_kind")
            account_kind = con.execute("SELECT kind FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
            if detected_kind:
                if detected_kind != account_kind:
                    raise ValueError("Report contract kind does not match the selected account")
            if bound and candidate_key != bound[0]:
                raise ValueError("Report contract does not match the selected account")
            if self._other_account_with_identity(con, account_id, preview["broker"],
                                                 account_kind, candidate_key):
                raise ValueError("Report contract is already bound to another account of the same type")
            if not bound and candidate_key:
                con.execute("INSERT INTO account_identities(account_id,identity_hash,confirmed_at) VALUES(?,?,?)",
                            (account_id, candidate_key, datetime.now(timezone.utc).isoformat()))
            required = self._required_review(preview)
            missing = [key for key in required if not str(decisions.get(key, {}).get("reason", "")).strip()]
            if missing:
                raise ValueError(f"Explicit review required for {len(missing)} items")
            promoted_summaries = set()
            for summary in preview.get("summary_trades", []):
                location = summary["location"]
                key = f"summary:{location['sheet']}:{location['row']}"
                action = decisions[key].get("action")
                if action not in {"control_only", "promote"}:
                    raise ValueError("Unkeyed trade requires control_only or promote decision")
                if action == "promote":
                    promoted_summaries.add((location["sheet"], location["row"]))
            # The state change and every ledger write share one transaction. If a later
            # validation fails, rollback restores awaiting_review and removes all writes.
            con.execute("UPDATE import_attempts SET state='committed' WHERE id=?", (attempt_id,))
            inserted = observed = 0
            now = datetime.now(timezone.utc).isoformat()
            staged_rows: dict[tuple[str, int], set[str]] = {}
            for sheet, row_number, classification in con.execute(
                "SELECT sheet,row_number,classification FROM source_rows WHERE attempt_id=?", (attempt_id,)
            ):
                staged_rows.setdefault((sheet, row_number), set()).add(classification)
            for item in self._event_items(preview, promoted_summaries=promoted_summaries):
                location = (item["sheet"], item["row"])
                required_class = "trade" if item["kind"] == "trade" else "cash_movement"
                classes = staged_rows.get(location, set())
                if (required_class not in classes
                        and not (location in promoted_summaries and item["kind"] == "trade"
                                 and "unparsed" in classes)):
                    raise ValueError("Ledger event has no matching staged source row")
                if self._cross_account_trade_ids(con, account_id, preview["broker"], item):
                    raise ValueError("Trade is already recorded under another account; verify contract identity")
                if (preview["broker"] == "sber"
                        and self._reclassified_sber_source_conflicts(con, account_id, attempt_id, item)):
                    raise ValueError("Previously imported source cells have a different cash event; review the legacy import before reloading")
                existing = self._existing_events(con, account_id, item)
                if (item["sheet"], item["row"]) in promoted_summaries and existing:
                    raise ValueError("Aggregated trade already matches a ledger event; keep it as a control")
                decision = decisions.get(item["decision_key"], {})
                if existing and any(not self._same_event_payload(item["kind"], old[1], item["payload"]) for old in existing):
                    raise ValueError("Conflicting broker event ID; correct source or adapter before confirmation")
                if existing and item["broker_id"] is None and any(old[2] != source_id for old in existing):
                    if decision.get("action") not in {"skip", "new"} or not str(decision.get("reason", "")).strip():
                        raise ValueError("Ambiguous overlapping cash event requires skip/new decision and reason")
                observed_here = con.execute("SELECT event_id FROM event_observations WHERE source_id=? AND sheet=? AND row_number=? AND event_type=? AND component=?",
                                            (source_id, item["sheet"], item["row"], item["kind"], item["component"])).fetchone()
                if observed_here and observed_here[0] not in {old[0] for old in existing}:
                    raise ValueError("Previously imported source row has a different event; review the legacy import before reloading")
                if len(existing) > 1 and item["broker_id"] is not None and not observed_here:
                    if decision.get("action") != "skip" or not str(decision.get("reason", "")).strip():
                        raise ValueError("Ambiguous broker reference requires selected existing event and reason")
                should_reuse = bool(existing) and (item["broker_id"] is not None or observed_here or decision.get("action") == "skip")
                if should_reuse:
                    selected = observed_here[0] if observed_here else decision.get("existing_event_id")
                    if selected is None and len(existing) == 1:
                        selected = existing[0][0]
                    if selected not in {old[0] for old in existing}:
                        raise ValueError("Select one matching existing event")
                    event_id = selected
                    already_in_file = con.execute(
                        "SELECT sheet,row_number FROM event_observations WHERE event_id=? AND source_id=?",
                        (event_id, source_id),
                    ).fetchone()
                    if already_in_file and already_in_file != (item["sheet"], item["row"]):
                        raise ValueError("Existing event is already matched to another row in this file")
                    observed += 1
                else:
                    cur = con.execute("INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                      (account_id, source_id, item["sheet"], item["row"], item["kind"], item["broker_id"], item["fingerprint"], item["payload"], now))
                    event_id = cur.lastrowid
                    inserted += 1
                con.execute("INSERT OR IGNORE INTO event_observations(event_id,source_id,sheet,row_number,event_type,component) VALUES(?,?,?,?,?,?)",
                            (event_id, source_id, item["sheet"], item["row"], item["kind"], item["component"]))
            for key, decision in decisions.items():
                if key in required or key.startswith("event:"):
                    reason = str(decision.get("reason", "")).strip()
                    if reason:
                        action = str(decision.get("action", "acknowledge"))
                        con.execute("INSERT INTO audit_decisions(attempt_id,action,reason,created_at) VALUES(?,?,?,?)", (attempt_id, f"{key}:{action}", reason, now))
            con.commit()
            return {"inserted": inserted, "observed_duplicates": observed}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def reject_attempt(self, attempt_id: int, reason: str) -> None:
        if not reason.strip():
            raise ValueError("Reason is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            cur = con.execute("UPDATE import_attempts SET state='rejected' WHERE id=? AND state='awaiting_review'", (attempt_id,))
            if cur.rowcount != 1:
                raise ValueError("Import attempt is not awaiting review")
            con.execute("INSERT INTO audit_decisions(attempt_id,action,reason,created_at) VALUES(?,?,?,?)",
                        (attempt_id, "reject", reason.strip(), now))

    def delete_unconfirmed_attempt(self, attempt_id: int) -> bool:
        """Remove one unconfirmed attempt and its source only when no source reference remains."""
        with self._file_lock():
            remove_path = None
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT a.state,a.source_id,s.sha256,s.private_path FROM import_attempts a "
                    "JOIN source_files s ON s.id=a.source_id WHERE a.id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("Import attempt not found")
                state, source_id, digest, private_path = row
                if state not in {"awaiting_review", "rejected"}:
                    raise ValueError("Committed import cannot be deleted")
                remaining = con.execute("SELECT COUNT(*) FROM import_attempts WHERE source_id=? AND id<>?",
                                        (source_id, attempt_id)).fetchone()[0]
                if not remaining:
                    candidate = Path(private_path)
                    if (not re.fullmatch(r"[0-9a-f]{64}", digest)
                            or candidate.parent.resolve() != self.files.resolve()
                            or candidate.name not in {f"{digest}{suffix}" for suffix in (".html", ".xls", ".xlsx")}
                            or candidate.is_symlink()):
                        raise ValueError("Stored report path is unsafe")
                con.execute("DELETE FROM audit_decisions WHERE attempt_id=?", (attempt_id,))
                con.execute("DELETE FROM source_rows WHERE attempt_id=?", (attempt_id,))
                con.execute("DELETE FROM import_attempts WHERE id=?", (attempt_id,))
                if not remaining:
                    con.execute("DELETE FROM source_files WHERE id=?", (source_id,))
                    if not con.execute("SELECT 1 FROM source_files WHERE private_path=? LIMIT 1",
                                       (private_path,)).fetchone():
                        remove_path = candidate
            if remove_path is not None:
                remove_path.unlink(missing_ok=True)
            return remove_path is not None
