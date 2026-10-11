"""Owner-facing analytical pages on an empty synthetic installation."""

from fastapi.testclient import TestClient

from svoibumagi.analytics import TABS
from svoibumagi.auth import hash_password
from svoibumagi.locale_ru import reason_ru
from svoibumagi.web import create_app


def test_analytics_tabs_are_private_and_do_not_invent_empty_portfolio_returns(tmp_path):
    client = TestClient(create_app(tmp_path / "private", hash_password("synthetic-password-123"),
                                   secure_cookie=False), follow_redirects=False)
    assert client.get("/analytics").status_code == 401
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303
    for key, title in TABS:
        response = client.get("/analytics", params={"view": key})
        assert response.status_code == 200
        assert title in response.text
    assert "XIRR пока не подтверждён" in client.get("/analytics", params={"view": "return"}).text
    assert "0 %" not in client.get("/analytics", params={"view": "return"}).text
    assert client.get("/analytics", params={"view": "unknown"}).status_code == 404


def test_common_accounting_reasons_are_russian():
    assert reason_ru("full history from account opening has not been confirmed") == (
        "Не подтверждено, что загружены отчёты с открытия счёта.")
    assert reason_ru("FIFO unavailable: Sale has no confirmed opening lot: RU000A0JNAA8") == (
        "Не удалось рассчитать FIFO: Для продажи нет подтверждённой партии покупки: RU000A0JNAA8")
