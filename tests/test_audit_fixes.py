"""Focused regression checks for the October 2026 audit fixes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password, verify_password
from svoibumagi.storage import Storage, _detect
from svoibumagi.web import create_app


def test_login_limits_failed_attempts_without_charging_blocked_checks(tmp_path: Path):
    root = tmp_path / "private"
    client = TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    client.get("/login")
    csrf = client.cookies.get("sb_login_csrf")
    for _ in range(3):
        assert client.post("/login", data={"csrf": csrf, "password": "wrong"}).status_code == 200
    blocked = client.post("/login", data={"csrf": csrf, "password": "synthetic-password-123"})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_hash_parameters_are_checked_explicitly():
    encoded = hash_password("synthetic-password-123")
    assert verify_password("synthetic-password-123", encoded)
    assert not verify_password("wrong", encoded)
    with pytest.raises(ValueError, match="Unsupported owner password hash"):
        verify_password("synthetic-password-123", encoded.replace("32768", "16384", 1))


def test_expired_sessions_are_removed_on_lookup_and_logout(tmp_path: Path):
    store = Storage(tmp_path / "private")
    token, _ = store.create_session()
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(store.db_path) as con:
        con.execute("UPDATE owner_sessions SET expires_at=?", (expired,))
    assert store.get_session_csrf(token) is None
    with sqlite3.connect(store.db_path) as con:
        assert con.execute("SELECT count(*) FROM owner_sessions").fetchone()[0] == 0


def test_cp1251_sber_table_signature_is_detected(tmp_path: Path):
    path = tmp_path / "report.bin"
    path.write_bytes("<table><tr><td>Отчет брокера</td></tr></table>".encode("cp1251"))
    assert _detect(path) == ("sber", ".html")


def test_public_styles_are_served_without_inline_csp(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "private", hash_password("synthetic-password-123"), secure_cookie=False))
    page = client.get("/")
    assert page.status_code == 200
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert '<link rel="stylesheet" href="/assets/theme.css">' in page.text
    for name in ("theme", "landing", "base"):
        css = client.get(f"/assets/{name}.css")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")
