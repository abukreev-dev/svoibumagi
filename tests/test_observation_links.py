"""One source row per observed ledger event. SPDX-License-Identifier: GPL-3.0-or-later"""

from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password
from svoibumagi.storage import Storage
from tests.source_fixture import source_row
from svoibumagi.web import create_app


def _table(rows):
    return "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows) + "</table>"


def _report(extra: str = "") -> str:
    return "Отчет брокера за период с 01.01.2025 по 31.01.2025 Договор SYN123 от 01.01.2025 " + "".join([
        _table([["Основной рынок", "Начало периода", "Конец периода"],
                ["Наименование", "ISIN ценной бумаги"] + ["-"] * 16]),
        _table([["Торговая площадка", "Валюта", "Конец периода"], ["1", "2", "3"],
                ["Торговый счет, Основной рынок", "RUB", "1", "0", "20", "20", "0", "0", "20"]]),
        _table([["Дата", "Торговая площадка", "Описание операции", "Валюта", "Сумма зачисления", "Сумма списания"],
                ["1", "2", "3", "4", "5", "6"],
                ["15.01.2025", "Основной рынок", "Пополнение", "RUB", "10", ""],
                ["15.01.2025", "Основной рынок", "Пополнение", "RUB", "10", ""]]),
        extra,
    ])


def test_delete_unconfirmed_attempt_preserves_shared_and_committed_sources(tmp_path):
    store = Storage(tmp_path / "private")
    left = store.create_account("sber", "brokerage", "Left")
    right = store.create_account("sber", "brokerage", "Right")
    report = tmp_path / "synthetic.html"
    report.write_text(_report())
    first, _ = store.stage_file(left, report)
    second, _ = store.stage_file(left, report)
    third, _ = store.stage_file(right, report)
    with store._connect() as con:
        source_path = Path(con.execute("SELECT private_path FROM source_files LIMIT 1").fetchone()[0])
        assert con.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 2
    assert source_path.exists()
    assert not store.delete_unconfirmed_attempt(first)
    assert source_path.exists()
    store.reject_attempt(second, "synthetic rejection")
    assert not store.delete_unconfirmed_attempt(second)
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 1
    assert store.delete_unconfirmed_attempt(third)
    assert not source_path.exists()
    assert store.list_attempts() == []

    committed, _ = store.stage_file(left, report)
    review = store.review_attempt(committed)
    decisions = {key: {"reason": "synthetic report has no position snapshot"}
                 for key in review["required_decisions"]}
    assert store.confirm_attempt(committed, decisions)["inserted"] == 2
    with store._connect() as con:
        source_path = Path(con.execute("SELECT private_path FROM source_files LIMIT 1").fetchone()[0])
    replay, _ = store.stage_file(left, report)
    assert not store.delete_unconfirmed_attempt(replay)
    with pytest.raises(ValueError, match="Committed import"):
        store.delete_unconfirmed_attempt(committed)
    assert source_path.exists()
    assert len(store.list_events(left)) == 2


def test_web_delete_unconfirmed_requires_owner_and_csrf(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    report = tmp_path / "synthetic.html"
    report.write_text(_report())
    attempt, _ = store.stage_file(account, report)
    with store._connect() as con:
        source_path = Path(con.execute("SELECT private_path FROM source_files LIMIT 1").fetchone()[0])
    app = create_app(store.root, hash_password("synthetic-password-123"), secure_cookie=False)
    outsider = TestClient(app, follow_redirects=False)
    assert outsider.post(f"/imports/{attempt}/delete", data={"csrf": "x"}).status_code == 401
    client = TestClient(app, follow_redirects=False)
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                      "password": "synthetic-password-123"}).status_code == 303
    csrf = store.get_session_csrf(client.cookies.get("sb_session"))
    assert "Удалить загрузку" in client.get(f"/imports/{attempt}").text
    assert client.post(f"/imports/{attempt}/delete", data={"csrf": "invalid"}).status_code == 403
    assert source_path.exists()
    assert client.post(f"/imports/{attempt}/delete", data={"csrf": csrf}).status_code == 303
    assert not source_path.exists()
    assert client.get(f"/imports/{attempt}").status_code == 404


def test_failed_stage_rolls_back_new_private_file(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    report = tmp_path / "synthetic.html"
    report.write_text(_report())
    with store._connect() as con:
        con.execute("CREATE TRIGGER fail_source_row BEFORE INSERT ON source_rows "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic row failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic row failure"):
        store.stage_file(account, report)
    assert not list(store.files.glob("*.html"))
    assert store.list_attempts() == []
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0


def test_identical_rows_in_overlapping_files_need_distinct_existing_ids(tmp_path):
    store = Storage(tmp_path / "private")
    account = store.create_account("sber", "brokerage", "Synthetic")
    first = tmp_path / "first.html"
    first.write_text(_report())
    first_attempt, _ = store.stage_file(account, first)
    first_review = store.review_attempt(first_attempt)
    base_decisions = {key: {"reason": "synthetic report has no position snapshot"}
                      for key in first_review["required_decisions"]}
    assert store.confirm_attempt(first_attempt, base_decisions)["inserted"] == 2

    second = tmp_path / "second.html"
    second.write_text(_report("second export"))
    second_attempt, _ = store.stage_file(account, second)
    review = store.review_attempt(second_attempt)
    assert len(review["ambiguous_events"]) == 2
    assert all(len(item["existing_options"]) == 2 for item in review["ambiguous_events"])
    assert all(sum(option["same_location"] for option in item["existing_options"]) == 1
               for item in review["ambiguous_events"])
    assert all(all(option["raw_match"] is True for option in item["existing_options"])
               for item in review["ambiguous_events"])
    ids = [option["id"] for option in review["ambiguous_events"][0]["existing_options"]]
    assert len(set(ids)) == 2

    decisions = {key: {"reason": "synthetic report has no position snapshot"}
                 for key in review["required_decisions"]}
    for item in review["ambiguous_events"]:
        decisions[item["key"]] = {"action": "skip", "reason": "same row in overlapping report",
                                   "existing_event_id": ids[0]}
    with pytest.raises(ValueError, match="already matched"):
        store.confirm_attempt(second_attempt, decisions)
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0] == 2

    client = TestClient(create_app(store.root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    client.get("/login")
    login_csrf = client.cookies.get("sb_login_csrf")
    assert client.post("/login", data={"csrf": login_csrf, "password": "synthetic-password-123"}).status_code == 303
    csrf = store.get_session_csrf(client.cookies.get("sb_session"))
    page = client.get(f"/imports/{second_attempt}")
    assert page.status_code == 200 and 'name="existing::' in page.text
    form = {"csrf": csrf}
    for key in review["required_decisions"]:
        form[f"reason::{key}"] = "synthetic report has no position snapshot"
    for item, event_id in zip(review["ambiguous_events"], ids):
        form[f"action::{item['key']}"] = "skip"
        form[f"reason::{item['key']}"] = "same row in overlapping report"
        form[f"existing::{item['key']}"] = str(event_id)
    assert client.post(f"/imports/{second_attempt}/confirm", data=form).status_code == 303
    with store._connect() as con:
        observations = con.execute("SELECT event_id,source_id FROM event_observations ORDER BY id").fetchall()
        assert len(observations) == 4
        assert len(set(observations)) == 4
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                        (observations[2][0], observations[2][1], "table:2", 999, "cash"))


def test_migration_rejects_many_rows_observing_same_event_in_one_file(tmp_path):
    root = tmp_path / "private"
    store = Storage(root)
    account = store.create_account("sber", "brokerage", "Synthetic")
    with store._connect() as con:
        source = con.execute(
            "INSERT INTO source_files(account_id,sha256,original_name,private_path,size,detected_broker,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (account, "synthetic", "synthetic", "/synthetic", 1, "sber", "2025-01-01"),
        ).lastrowid
        source_row(con, source, "synthetic", 1)
        source_row(con, source, "synthetic", 2)
        event = con.execute(
            "INSERT INTO ledger_events(account_id,source_id,sheet,row_number,event_type,broker_event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account, source, "synthetic", 1, "cash", None, "fp", '{"kind":"deposit","date":"2025-01-01","amount":"1","currency":"RUB"}', "2025-01-01"),
        ).lastrowid
        con.execute("DROP INDEX event_observation_per_file")
        for row_number in (1, 2):
            con.execute("INSERT INTO event_observations(event_id,source_id,sheet,row_number,event_type) VALUES(?,?,?,?,?)",
                        (event, source, "synthetic", row_number, "cash"))
        con.execute("PRAGMA user_version=8")
    with pytest.raises(ValueError, match="multiple rows"):
        Storage(root)
    with store._connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8
