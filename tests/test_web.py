"""Private web journey on synthetic data. SPDX-License-Identifier: GPL-3.0-or-later"""

import csv
from datetime import date
from decimal import Decimal as D
from io import StringIO
from pathlib import Path

from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password
from svoibumagi.exports import gaps_csv, metrics_csv, operations_csv, valuations_csv
from svoibumagi.prices import MoexQuote, MoexSecurity, PriceUnavailable
from svoibumagi.storage import Storage
from svoibumagi.web import TEMPLATES, create_app


def _table(rows):
    return "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows) + "</table>"


def test_empty_service_does_not_show_zero_as_portfolio_valuation(tmp_path: Path):
    root = tmp_path / "private"
    store = Storage(root)
    estimate = store.portfolio_estimate()
    assert estimate["no_reports"] and estimate["incomplete"]
    client = TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303
    home = client.get("/")
    assert "Оценка недоступна: пока нет проведённых отчётов" in home.text
    assert "Известная стоимость бумаг на не задана: 0 ₽" not in home.text


def test_all_private_routes_require_session_and_mutations_require_csrf(tmp_path: Path):
    root = tmp_path / "private"
    app = create_app(root, hash_password("synthetic-password-123"), secure_cookie=False)
    client = TestClient(app, follow_redirects=False)
    routes = [(method, route.path.replace("{attempt_id}", "999")
               .replace("{account_id}", "999").replace("{event_id}", "999")
               .replace("{period_id}", "999"))
              for route in app.routes for method in route.methods
              if method in {"GET", "POST"} and route.path not in {"/", "/login"}]
    assert routes

    for method, path in routes:
        response = client.request(method, path)
        assert response.status_code == 401, (method, path, response.status_code)

    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303
    for method, path in routes:
        if method == "POST":
            response = client.post(path, data={"csrf": "invalid"})
            assert response.status_code == 403, (method, path, response.status_code)
    assert Storage(root).list_attempts() == []
    assert not list(root.glob("upload-*"))
    assert not list(root.glob("backup-*"))


def test_owner_can_remove_unused_account_but_not_one_with_manual_history(tmp_path: Path):
    root = tmp_path / "private"
    store = Storage(root)
    empty = store.create_account("sber", "brokerage", "Unused")
    used = store.create_account("sber", "brokerage", "Used")
    store.add_manual_cash(used, date(2025, 1, 1), "deposit", D("10"), "Synthetic deposit")
    with TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                    follow_redirects=False) as client:
        client.get("/login")
        assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                           "password": "synthetic-password-123"}).status_code == 303
        csrf = store.get_session_csrf(client.cookies.get("sb_session"))
        home = client.get("/")
        assert f'/accounts/{empty}/delete' in home.text
        assert f'/accounts/{used}/delete' not in home.text
        refused = client.post(f"/accounts/{used}/delete", data={"csrf": csrf})
        assert refused.status_code == 200
        assert "Account has reports" in refused.text
        assert client.post(f"/accounts/{empty}/delete", data={"csrf": csrf}).status_code == 303
        assert [account["id"] for account in store.list_accounts()] == [used]


def test_operation_journal_filters_account_date_type_source_and_security(tmp_path: Path):
    root = tmp_path / "private"
    store = Storage(root)
    first = store.create_account("sber", "brokerage", "Первый")
    second = store.create_account("alfa", "brokerage", "Второй")
    store.add_manual_cash(first, date(2025, 1, 2), "deposit", D("100"), "Synthetic deposit")
    store.add_manual_cash(second, date(2025, 1, 3), "coupon", D("5"), "Synthetic coupon")
    store.add_manual_trade(first, date(2025, 1, 4), date(2025, 1, 5), "RU000A000001",
                           "Synthetic Alpha", D("1"), D("10"), D("10"), D("0"), "Synthetic trade")
    client = TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303

    assert "Найдено операций: 3" in client.get("/operations").text
    assert "Найдено операций: 2" in client.get(f"/operations?account_id={first}").text
    assert "Найдено операций: 1" in client.get(f"/operations?account_id={second}").text
    assert "Найдено операций: 1" in client.get("/operations?event_type=trade").text
    assert "Найдено операций: 2" in client.get("/operations?event_type=cash").text
    assert "Найдено операций: 0" in client.get("/operations?source_type=broker").text
    assert "Найдено операций: 2" in client.get("/operations?from_date=2025-01-03&to_date=2025-01-04").text
    searched = client.get("/operations?q=ru000a000001&event_type=trade&source_type=manual")
    assert "Найдено операций: 1" in searched.text
    assert "Synthetic Alpha" in searched.text
    assert 'value="trade" selected' in searched.text
    assert 'value="manual" selected' in searched.text
    assert "Найдено операций: 1" in client.get("/operations?q=coupon").text
    assert client.get("/operations?event_type=unknown").status_code == 400
    assert client.get("/operations?from_date=2025-01-05&to_date=2025-01-02").status_code == 400
    assert client.get("/operations?from_date=invalid").status_code == 400


def test_import_history_reaches_attempts_older_than_home_limit(tmp_path: Path):
    root = tmp_path / "private"
    store = Storage(root)
    first = store.create_account("sber", "brokerage", "Первый")
    second = store.create_account("sber", "brokerage", "Второй")
    report = tmp_path / "synthetic.html"
    report.write_text("Отчет брокера за период с 01.01.2025 по 31.01.2025 Договор SYN123 от 01.01.2025 "
                      + _table([["Основной рынок", "Начало периода", "Конец периода"],
                                ["Наименование", "ISIN ценной бумаги"] + ["-"] * 16])
                      + _table([["Торговая площадка", "Валюта", "Конец периода"], ["1", "2", "3"],
                                ["Торговый счет, Основной рынок", "RUB", "1", "0", "0", "0", "0", "0", "0"]]),
                      encoding="utf-8")
    oldest, _ = store.stage_file(first, report)
    for _ in range(50):
        store.stage_file(first, report)
    other, _ = store.stage_file(second, report)
    with store._connect() as con:
        con.execute("DROP TRIGGER protect_import_attempt_fields")  # Simulate a preview saved before this field existed.
        con.execute("UPDATE import_attempts SET preview_json=json_remove(preview_json,'$.account_number') WHERE id=?",
                    (oldest,))
    client = TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    assert client.get("/imports").status_code == 401
    client.get("/login")
    assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                       "password": "synthetic-password-123"}).status_code == 303

    home = client.get("/").text
    assert "Открыть всю историю загрузок" in home
    assert f'href="/imports/{oldest}"' not in home
    first_page = client.get("/imports")
    assert first_page.status_code == 200
    assert "Номер договора/счёта" in first_page.text and "SYN123" in first_page.text
    assert "Всего попыток: 52" in first_page.text
    assert f'href="/imports/{other}"' in first_page.text
    assert 'href="/imports?page=2"' in first_page.text
    assert f'href="/imports/{oldest}"' not in first_page.text
    second_page = client.get("/imports?page=2")
    assert second_page.status_code == 200
    assert f'href="/imports/{oldest}"' in second_page.text
    assert "недоступен для этой загрузки" in client.get(f"/imports/{oldest}").text
    filtered = client.get(f"/imports?account_id={second}")
    assert "Всего попыток: 1" in filtered.text
    assert f'href="/imports/{other}"' in filtered.text
    assert f'href="/imports/{oldest}"' not in filtered.text
    assert client.get("/imports?page=0").status_code == 400
    assert client.get("/imports?page=3").status_code == 404
    assert client.get("/imports?account_id=9999").status_code == 400


def test_web_unknown_row_needs_decision_and_reupload_is_idempotent(tmp_path: Path):
    root = tmp_path / "private"
    app = create_app(root, hash_password("synthetic-password-123"), secure_cookie=False)
    with TestClient(app, follow_redirects=False) as client:
        client.get("/login")
        assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                           "password": "synthetic-password-123"}).status_code == 303
        store = Storage(root)
        csrf = store.get_session_csrf(client.cookies.get("sb_session"))
        assert client.post("/accounts", data={"csrf": csrf, "broker": "sber", "kind": "brokerage",
                                              "name": "Synthetic"}).status_code == 303
        account_id = store.list_accounts()[0]["id"]
        html = "Отчет брокера за период с 01.01.2025 по 31.01.2025 Договор SYN123 от 01.01.2025 " + "".join([
            _table([["Основной рынок", "Начало периода", "Конец периода"],
                    ["Наименование", "ISIN ценной бумаги"] + ["-"] * 16]),
            _table([["Торговая площадка", "Валюта", "Конец периода"], ["1", "2", "3"],
                    ["Торговый счет, Основной рынок", "RUB", "1", "0", "11", "11", "0", "0", "11"]]),
            _table([["Дата", "Торговая площадка", "Описание операции", "Валюта", "Сумма зачисления", "Сумма списания"],
                    ["1", "2", "3", "4", "5", "6"],
                    ["15.01.2025", "Основной рынок", "Пополнение", "RUB", "10", "0"],
                    ["16.01.2025", "Основной рынок", "Неизвестная операция", "RUB", "1", "0"]]),
        ])

        def upload():
            result = client.post("/imports", data={"csrf": csrf, "account_id": str(account_id)},
                                 files={"report": ("synthetic.html", html.encode(), "text/html")})
            assert result.status_code == 303
            return result.headers["location"]

        url = upload()
        review = store.review_attempt(int(url.rsplit("/", 1)[-1]))
        assert any(key.startswith("unparsed:") for key in review["required_decisions"])
        preview_page = client.get(url).text
        assert "Номер договора/счёта в исходном отчёте" in preview_page
        assert "SYN123" in preview_page
        assert "Выбран локальный счёт №" in preview_page
        assert "Неизвестная операция" in preview_page
        assert "Исходные строки для проверки" in preview_page
        assert "difference" in preview_page
        assert client.post(url + "/confirm", data={"csrf": "wrong"}).status_code == 403
        denied = client.post(url + "/confirm", data={"csrf": csrf})
        assert denied.status_code == 200 and "Explicit review required" in denied.text
        assert store.list_events(account_id) == []
        assert client.post(url + "/reject", data={"csrf": csrf, "reason": "Synthetic row needs review"}).status_code == 303
        assert store.review_attempt(int(url.rsplit("/", 1)[-1]))["state"] == "rejected"
        assert store.list_events(account_id) == []
        url = upload()
        review = store.review_attempt(int(url.rsplit("/", 1)[-1]))
        reasons = {f"reason::{key}": "Synthetic source reviewed; discrepancy remains"
                   for key in review["required_decisions"]}
        assert client.post(url + "/confirm", data={"csrf": csrf, **reasons}).status_code == 303
        assert len(store.list_events(account_id)) == 1
        exported = list(csv.DictReader(StringIO(client.get("/export/operations.csv").content.decode("utf-8-sig"))))
        assert exported[0]["account_number"] == "SYN123"
        assert any(check["status"] == "difference" for check in store.committed_reports(account_id)[0]["checks"])
        repeated = upload()
        assert "duplicate" in client.get(repeated).text
        assert client.post(repeated + "/confirm", data={"csrf": csrf, **reasons}).status_code == 303
        assert len(store.list_events(account_id)) == 1


def test_auth_csrf_and_import_preview(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    app = create_app(data, hash_password("synthetic-password-123"), secure_cookie=False,
                     moex_iss_enabled=True)
    client = TestClient(app, follow_redirects=False)
    landing = client.get("/")
    assert landing.status_code == 200
    assert "Как работает импорт" in landing.text
    assert "Сбер" in landing.text and "Альфа" in landing.text
    assert "SYN123" not in landing.text
    assert client.get("/operations").status_code == 401


    assert client.get("/instruments").status_code == 401
    page = client.get("/login")
    assert page.status_code == 200
    login_csrf = client.cookies.get("sb_login_csrf")
    assert client.post("/login", data={"password": "synthetic-password-123", "csrf": "wrong"}).status_code == 403
    assert client.post("/login", data={"password": "synthetic-password-123", "csrf": login_csrf}).status_code == 303
    store = Storage(data)
    csrf = store.get_session_csrf(client.cookies.get("sb_session"))
    assert csrf
    assert client.post("/accounts", data={"name": "Synthetic", "broker": "sber", "kind": "brokerage", "csrf": "wrong"}).status_code == 403
    assert client.post("/accounts", data={"name": "Synthetic", "broker": "sber", "kind": "brokerage", "csrf": csrf}).status_code == 303
    account_id = store.list_accounts()[0]["id"]
    monkeypatch.setattr("svoibumagi.web.MAX_UPLOAD_BYTES", 64)
    oversized_body = client.post("/imports", data={"account_id": str(account_id), "csrf": csrf},
                                 files={"report": ("too-large.html", b"x" * 70_000, "text/html")})
    assert oversized_body.status_code == 413
    oversized_file = client.post("/imports", data={"account_id": str(account_id), "csrf": csrf},
                                 files={"report": ("too-large.html", b"x" * 65, "text/html")})
    assert oversized_file.status_code == 413
    boundary = "synthetic-boundary"
    streamed_body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"report\"; filename=\"too-large.html\"\r\n"
                     "Content-Type: text/html\r\n\r\n").encode() + b"x" * 70_000 + f"\r\n--{boundary}--\r\n".encode()
    streamed = client.post("/imports", content=(streamed_body[i:i + 1024] for i in range(0, len(streamed_body), 1024)),
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": "1"})
    assert streamed.status_code == 413
    assert store.list_attempts() == []
    assert not list(data.glob("upload-*"))
    monkeypatch.setattr("svoibumagi.web.MAX_UPLOAD_BYTES", 20_000_000)
    html = "Отчет брокера за период с 01.01.2025 по 31.01.2025 Договор SYN123 от 01.01.2025 " + "".join([
        _table([["Основной рынок", "Начало периода", "Конец периода"], ["Наименование", "ISIN ценной бумаги"] + ["-"] * 16,
                ["Synthetic", "RU000A000001", "RUB", "1"] + ["0"] * 4 + ["1"] + ["0"] * 9]),
        _table([["Торговая площадка", "Валюта", "Конец периода"], ["1", "2", "3"],
                ["Торговый счет, Основной рынок", "RUB", "1", "50", "0", "50", "0", "0", "50"]]),
    ])
    response = client.post("/imports", data={"account_id": str(account_id), "csrf": csrf}, files={"report": ("synthetic.html", html.encode(), "text/html")})
    assert response.status_code == 303
    attempt_path = response.headers["location"]
    preview = client.get(attempt_path)
    assert preview.status_code == 200
    assert "matched" in preview.text
    assert client.post(attempt_path + "/confirm", data={"csrf": csrf}).status_code == 303
    assert store.list_attempts()[0]["state"] == "committed"
    overview = client.get("/")
    assert "SYN123 — Synthetic" in overview.text
    assert store.list_accounts()[0]["account_number"] == "SYN123"
    for export_name in ("positions", "valuations", "metrics"):
        rows = list(csv.DictReader(StringIO(client.get(f"/export/{export_name}.csv").content.decode("utf-8-sig"))))
        assert rows and rows[0]["account_number"] == "SYN123", export_name
    assert "Выплаты и удержания по проведённому журналу до 2025-01-31" in overview.text
    assert "неполные данные" in overview.text
    assert client.post("/prices", data={"csrf": csrf, "isin": "RU000A000001", "amount": "15", "as_of": "2025-01-31"}).status_code == 303
    monkeypatch.setattr("svoibumagi.web.get_moex_share_quote", lambda isin, secid: MoexQuote(isin, secid, D("16"), date(2025, 1, 31), "https://iss.moex.com/synthetic"))
    assert client.post("/prices/moex", data={"csrf": csrf, "isin": "RU000A000001", "secid": "SYN"}).status_code == 303
    assert store.latest_prices(date(2025, 1, 31))["RU000A000001"]["amount"] == "15"
    monkeypatch.setattr("svoibumagi.web.get_moex_share_close", lambda isin, secid, when: MoexQuote(isin, secid, D("17"), when, "https://iss.moex.com/history/synthetic"))
    historical = client.post("/prices/moex", data={"csrf": csrf, "isin": "RU000A000001", "secid": "SYN",
                                                   "as_of": "2025-01-30"})
    assert historical.status_code == 303 and historical.headers["location"] == "/?as_of=2025-01-30"
    assert store.latest_prices(date(2025, 1, 30))["RU000A000001"]["amount"] == "17"
    monkeypatch.setattr("svoibumagi.web.get_moex_bond_price", lambda isin, secid, board, when: MoexQuote(isin, secid, D("632.05"), when, "https://iss.moex.com/history/bond"))
    bond = client.post("/prices/moex/bond", data={"csrf": csrf, "isin": "RU000A000001", "secid": "SYNBOND",
                                                   "board": "TQOB", "as_of": "2025-01-29"})
    assert bond.status_code == 303 and bond.headers["location"] == "/?as_of=2025-01-29"
    assert store.latest_prices(date(2025, 1, 29))["RU000A000001"]["amount"] == "632.05"
    monkeypatch.setattr("svoibumagi.web.find_moex_security", lambda isin: MoexSecurity(isin, "SYNBOND", "TQOB", "bond"))
    automatic = client.post("/prices/moex/auto", data={"csrf": csrf, "isin": "RU000A000001",
                                                       "as_of": "2025-01-28"})
    assert automatic.status_code == 303 and automatic.headers["location"] == "/?as_of=2025-01-28"
    assert store.latest_prices(date(2025, 1, 28))["RU000A000001"]["amount"] == "632.05"
    monkeypatch.setattr("svoibumagi.web.resolve_moex_quote", lambda isin, when: (
        MoexSecurity(isin, "SYN", "TQBR", "share"),
        MoexQuote(isin, "SYN", D("18"), when, "https://iss.moex.com/history/synthetic")))
    refreshed = client.post("/prices/moex/refresh", data={"csrf": csrf, "as_of": "2025-01-31"})
    assert refreshed.status_code == 303 and refreshed.headers["location"] == "/?as_of=2025-01-31"
    with store._connect() as con:
        price_count = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert client.post("/prices/moex/refresh", data={"csrf": csrf, "as_of": "2025-01-31"}).status_code == 303
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == price_count
    monkeypatch.setattr("svoibumagi.web.resolve_moex_quote", lambda isin, when: (_ for _ in ()).throw(
        PriceUnavailable("synthetic quote unavailable")))
    assert "synthetic quote unavailable" in client.post(
        "/prices/moex/refresh", data={"csrf": csrf, "as_of": "2025-01-31"}).text
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == price_count
    monkeypatch.setattr("svoibumagi.web.get_moex_share_quote", lambda isin, secid: (_ for _ in ()).throw(PriceUnavailable("network unavailable")))
    with store._connect() as con:
        before = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert "network unavailable" in client.post("/prices/moex", data={"csrf": csrf, "isin": "RU000A000001", "secid": "SYN"}).text
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == before
    assert "15 ₽" in client.get("/").text
    store.add_price("RU000A000001", D("17"), date(2025, 2, 1), note="later synthetic price")
    historical = store.portfolio_estimate(date(2025, 1, 31))
    assert historical["positions"][0]["price"]["amount"] == "15"
    assert historical["stale_position_accounts"] == 0

    before_first = store.portfolio_estimate(date(2025, 1, 30))
    assert before_first["positions"] == [] and before_first["missing_accounts"] == 1
    missing_rows = list(csv.reader(StringIO(client.get("/export/valuations.csv?as_of=2025-01-30").content.decode("utf-8-sig"))))
    assert missing_rows[1][0] == str(account_id) and missing_rows[1][10] == "unavailable_no_report"
    assert missing_rows[1][12:17] == ["2025-01-30", "SYN123", "unavailable", "unavailable", "unavailable"]
    later = store.portfolio_estimate(date(2025, 2, 1))
    assert later["positions"][0]["price"]["amount"] == "17"
    assert later["stale_position_accounts"] == 1 and later["incomplete"]
    assert "Количество бумаг после отчёта не подтверждено" in client.get("/?as_of=2025-02-01").text
    valuation_rows = list(csv.reader(StringIO(client.get("/export/valuations.csv?as_of=2025-02-01").content.decode("utf-8-sig"))))
    assert valuation_rows[1][10] == "stale_position"
    assert valuation_rows[1][12:15] == ["2025-02-01", "SYN123", "matched"]
    assert valuation_rows[1][15] == "stale"
    assert client.get("/?as_of=invalid").status_code == 400
    assert client.get("/?as_of=2999-01-01").status_code == 400
    selected_metrics = list(csv.reader(StringIO(client.get(
        "/export/metrics.csv?as_of=2025-01-30").content.decode("utf-8-sig"))))
    assert selected_metrics[1][3] == "2025-01-30" and selected_metrics[1][4] == "unavailable"
    assert "нет подтверждённого отчёта с конечным остатком" in selected_metrics[1][12]
    assert "нет подтверждённого отчёта с конечным остатком" in client.get("/?as_of=2025-01-30").text
    assert client.get("/export/operations.csv").status_code == 200
    assert client.get("/export/positions.csv").status_code == 200
    assert client.get("/export/valuations.csv").status_code == 200
    assert client.get("/export/metrics.csv").status_code == 200
    assert client.get("/instruments").status_code == 200
    backup = client.post("/backup", data={"csrf": csrf})
    assert backup.status_code == 200 and backup.content.startswith(b"PK")
    assert client.post("/logout", data={"csrf": csrf}).status_code == 303
    assert client.get("/operations").status_code == 401


def test_moex_routes_are_disabled_by_default_without_affecting_manual_prices(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    app = create_app(data, hash_password("synthetic-password-123"), secure_cookie=False)
    client = TestClient(app, follow_redirects=False)
    assert client.post("/prices/moex", data={}).status_code == 401
    client.get("/login")
    assert client.post("/login", data={"password": "synthetic-password-123",
                                      "csrf": client.cookies.get("sb_login_csrf")}).status_code == 303
    csrf = Storage(data).get_session_csrf(client.cookies.get("sb_session"))
    page = client.get("/").text
    assert "Автоматическая загрузка цен Мосбиржи отключена" in page
    assert 'action="/prices/moex"' not in page

    def unexpected_request(*args, **kwargs):
        raise AssertionError("Disabled MOEX endpoint made a network request")

    for name in ("get_moex_share_quote", "get_moex_share_close", "get_moex_bond_price",
                 "find_moex_security", "resolve_moex_quote"):
        monkeypatch.setattr(f"svoibumagi.web.{name}", unexpected_request)
    for path in ("/prices/moex", "/prices/moex/bond", "/prices/moex/auto", "/prices/moex/refresh"):
        assert client.post(path, data={"csrf": csrf}).status_code == 403

    added = client.post("/prices", data={"csrf": csrf, "isin": "RU000A000001",
                                         "amount": "100", "as_of": "2025-01-31", "note": "Synthetic"})
    assert added.status_code == 303
    assert Storage(data).latest_prices()["RU000A000001"]["source"] == "manual"


def test_source_download_verifies_private_file_integrity(tmp_path: Path):
    data = tmp_path / "data"
    store = Storage(data)
    account = store.create_account("sber", "brokerage", "Synthetic")
    report = tmp_path / "synthetic.html"
    report.write_text("Отчет брокера за период с 01.01.2025 по 31.01.2025 Договор SYN123 от 01.01.2025 " + "".join([
        _table([["Основной рынок", "Начало периода", "Конец периода"],
                ["Наименование", "ISIN ценной бумаги"] + ["-"] * 16]),
        _table([["Торговая площадка", "Валюта", "Конец периода"], ["1", "2", "3"],
                ["Торговый счет, Основной рынок", "RUB", "1", "0", "10", "10", "0", "0", "10"]]),
        _table([["Дата", "Торговая площадка", "Описание операции", "Валюта", "Сумма зачисления", "Сумма списания"],
                ["1", "2", "3", "4", "5", "6"],
                ["15.01.2025", "Основной рынок", "Пополнение", "RUB", "10", "0"]]),
    ]))
    attempt, _ = store.stage_file(account, report)
    review = store.review_attempt(attempt)
    decisions = {key: {"reason": "synthetic missing position snapshot"} for key in review["required_decisions"]}
    assert store.confirm_attempt(attempt, decisions)["inserted"] == 1
    event_id = store.list_events(account)[0]["id"]
    client = TestClient(create_app(data, hash_password("synthetic-password-123"), secure_cookie=False),
                        follow_redirects=False)
    assert client.get(f"/operations/{event_id}/source").status_code == 401
    client.get("/login")
    csrf = client.cookies.get("sb_login_csrf")
    assert client.post("/login", data={"csrf": csrf, "password": "synthetic-password-123"}).status_code == 303
    source_url = f"/operations/{event_id}/source"
    assert client.get(source_url).content == report.read_bytes()
    source_path, _ = store.event_source(event_id)
    original = source_path.read_bytes()
    source_path.write_bytes(original[:-1] + b"!")
    assert client.get(source_url).status_code == 409
    source_path.write_bytes(original)
    with store._connect() as con:
        con.execute("UPDATE source_files SET private_path=? WHERE id=(SELECT source_id FROM ledger_events WHERE id=?)",
                    (str(tmp_path / "outside.html"), event_id))
    assert client.get(source_url).status_code == 409


def test_https_cookies_and_private_response_headers(tmp_path: Path):
    app = create_app(tmp_path / "data", hash_password("synthetic-password-123"))
    client = TestClient(app, base_url="https://example.test", follow_redirects=False)
    landing = client.get("/")
    assert landing.status_code == 200
    assert landing.headers["cache-control"] == "no-store"
    assert "/login" in landing.text
    page = client.get("/login")
    assert page.status_code == 200
    assert "Secure" in page.headers["set-cookie"]
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "SameSite=strict" in page.headers["set-cookie"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    denied = client.get("/operations")
    assert denied.status_code == 401
    assert denied.headers["cache-control"] == "no-store"
    csrf = client.cookies.get("sb_login_csrf")
    signed_in = client.post("/login", data={"password": "synthetic-password-123", "csrf": csrf})
    assert signed_in.status_code == 303
    assert "Secure" in signed_in.headers["set-cookie"]
    assert client.get("/").status_code == 200


def test_csv_text_cannot_start_a_formula():
    event = {"id": 1, "account_id": 2, "event_type": "trade", "payload": {"name": "=HYPERLINK(1)", "amount": "10"},
             "original_name": "+private.xls", "sheet": "@sheet", "row_number": 3}
    rows = list(csv.reader(StringIO(operations_csv([event]).decode("utf-8-sig"))))
    assert rows[1][6] == "'=HYPERLINK(1)"
    assert rows[1][12] == "'+private.xls"
    assert rows[1][13] == "'@sheet"
    transfer = {"id": 2, "account_id": 1, "event_type": "cash", "payload": {
        "kind": "internal_out", "source_kind": "transfer_unclassified", "paired_event_id": 3,
        "date": "2025-01-02", "amount": "-10", "currency": "RUB"},
        "original_name": "synthetic.xls", "sheet": "cash", "row_number": 4, "sha256": "abc123"}
    classified = list(csv.reader(StringIO(operations_csv([transfer]).decode("utf-8-sig"))))
    assert classified[1][6] == "internal_out"
    assert classified[1][16:23] == ["transfer_unclassified", "3", "abc123", "", "broker", "", ""]
    transfer["source_row_verified"] = False
    provenance_rows = list(csv.DictReader(StringIO(operations_csv([transfer]).decode("utf-8-sig"))))
    assert provenance_rows[0]["source_row_status"] == "missing"
    transfer["source_row_verified"] = True
    provenance_rows = list(csv.DictReader(StringIO(operations_csv([transfer]).decode("utf-8-sig"))))
    assert provenance_rows[0]["source_row_status"] == "matched"
    transfer["source_row_verified"] = False
    page = TEMPLATES.get_template("operations.html").render(
        events=[transfer], accounts=[], csrf="synthetic", error=None)
    assert "Нет сохранённой исходной строки; подтверждённый расчёт недоступен." in page
    dividend = {**transfer, "payload": {"kind": "dividend", "date": "2025-01-02",
                                          "amount": "100", "withheld_tax": "13", "currency": "RUB"}}
    income_rows = list(csv.reader(StringIO(operations_csv([dividend]).decode("utf-8-sig"))))
    assert income_rows[1][income_rows[0].index("withheld_tax_rub")] == "13"
    event["payload"]["name"] = " \t=HYPERLINK(1)"
    event["original_name"] = "\ufeff+private.xls"
    padded = list(csv.reader(StringIO(operations_csv([event]).decode("utf-8-sig"))))
    assert padded[1][6] == "' \t=HYPERLINK(1)"
    assert padded[1][12] == "'\ufeff+private.xls"
    for value in ("＝1+2", "＋1+2", "－1+2", "＠SUM(1)",
                  "\u200b=1+2", "\x00=1+2", "\tplain", "\rplain", "\nplain"):
        event["payload"]["name"] = value
        exported = list(csv.reader(StringIO(operations_csv([event]).decode("utf-8-sig"))))
        assert exported[1][6] == "'" + value


def test_calculated_csv_preserves_incomplete_status_and_safe_text():
    account = {"id": 1, "name": "=private"}
    estimate = {"positions": [{"account": account, "report_date": "2025-01-31", "isin": "RU000A000001",
                               "name": "+instrument", "quantity": 2, "price": None, "value": None,
                               "account_incomplete": True}]}
    valuation_rows = list(csv.reader(StringIO(valuations_csv(estimate).decode("utf-8-sig"))))
    assert valuation_rows[1][1] == "'=private"
    assert valuation_rows[1][4] == "'+instrument"
    assert valuation_rows[1][10] == "unavailable"
    assert valuation_rows[1][7] == ""
    metric = {"account": account, "as_of": None, "status": "unavailable", "cash": None,
              "securities": None, "terminal": None, "cost_basis": None, "realized": None,
              "xirr": None, "inferred_zero_count": 0, "reasons": ["=missing history"],
              "income": {"dividend_gross_known": D("100"), "dividend_tax_unknown": D("40"),
                         "dividend_with_tax_unpaired": D("2"),
                         "dividend_net": D(0), "coupon": D("5"), "tax_withheld": D("13"),
                             "accrued_interest_net": D("-2"), "coupon_net_of_accrued_interest": D("3"),
                             "fees_paid_cash": D("2")}}
    combined = {"status": "unavailable", "as_of": None, "terminal": None, "xirr": None,
                "reasons": ["at least one account incomplete"]}
    metric_rows = list(csv.reader(StringIO(metrics_csv([metric], combined).decode("utf-8-sig"))))
    assert metric_rows[1][2] == "'=private"
    assert metric_rows[1][4] == "unavailable"
    assert metric_rows[1][7] == ""
    assert metric_rows[1][12] == "'=missing history"
    assert metric_rows[0][14:20] == ["dividend_gross_known_rub", "dividend_tax_unknown_rub",
                                   "dividend_with_tax_unpaired_rub", "dividend_net_rub",
                                   "coupon_rub", "tax_withheld_rub"]
    assert metric_rows[0][20:25] == ["accrued_interest_net_rub", "coupon_net_of_accrued_interest_rub",
                                     "fees_paid_cash_rub", "in_kind_transfer_flow_rub", "xirr_status"]
    assert metric_rows[1][14:] == ["100", "40", "2", "0", "5", "13", "-2", "3", "2", "0", "unavailable", ""]
    assert metric_rows[2][14:] == [""] * 10 + ["unavailable", ""]
    assert metric_rows[2][0] == "combined"


def test_combined_csv_sums_recorded_income_only_for_common_verified_date():
    income = {"dividend_gross_known": D("100"), "dividend_tax_unknown": D(0),
              "dividend_with_tax_unpaired": D(0),
              "dividend_net": D(0), "coupon": D("5"), "tax_withheld": D("13"),
              "accrued_interest_net": D("-2"), "coupon_net_of_accrued_interest": D("3"),
              "fees_paid_cash": D("2")}
    account = {"account": {"id": 1, "name": "Synthetic"}, "as_of": date(2025, 1, 31),
               "status": "available", "cash": D("87"), "securities": D(0),
               "terminal": D("87"), "cost_basis": D(0), "realized": D(0),
               "xirr": D("0.01"), "inferred_zero_count": 0, "reasons": [], "income": income}
    combined = {"status": "available", "as_of": date(2025, 1, 31), "cash": D("174"),
                "securities": D(0), "terminal": D("174"), "cost_basis": D(0),
                "realized": D(0), "xirr": D("0.01"), "reasons": []}
    rows = list(csv.reader(StringIO(metrics_csv([account, {**account, "account": {"id": 2, "name": "Other"}}],
                                            combined).decode("utf-8-sig"))))
    assert rows[-1][14:] == ["200", "0", "0", "0", "10", "26", "-4", "6", "4", "", "available", ""]


def test_gap_export_separates_uncovered_dates_from_owner_statements_and_protects_csv():
    account = {"id": 7, "account_number": "SYN123", "account_number_conflict": False,
               "broker": "sber", "kind": "brokerage"}
    metric = {"account": account,
              "gaps": [{"start": "2025-02-01", "end": "2025-02-03", "days": 3},
                       {"start": "2025-03-01", "end": "2025-03-02", "days": 2}],
              "confirmable_gaps": [{"start": "2025-02-01", "end": "2025-02-03", "days": 3}]}
    confirmations = {7: [{"start_date": "2025-01-01", "end_date": "2025-01-31",
                          "reason": "=unsafe", "confirmed_at": "2025-02-05T00:00:00Z", "revoked_at": None},
                         {"start_date": "2025-04-01", "end_date": "2025-04-02",
                          "reason": "mistake", "confirmed_at": "2025-04-03T00:00:00Z",
                          "revoked_at": "2025-04-04T00:00:00Z"}]}
    rows = list(csv.DictReader(StringIO(gaps_csv([metric], confirmations).decode("utf-8-sig"))))
    assert [(row["account_number"], row["record_type"], row["status"], row["days"]) for row in rows] == [
        ("SYN123", "uncovered_gap", "needs_owner_confirmation", "3"),
        ("SYN123", "uncovered_gap", "needs_source_or_review", "2"),
        ("SYN123", "owner_statement", "owner_confirmed", "31"),
        ("SYN123", "owner_statement", "revoked", "2")]
    assert rows[2]["reason"] == "'=unsafe"


def test_gap_export_requires_login_and_includes_saved_owner_statement(tmp_path: Path):
    root = tmp_path / "private"
    store = Storage(root)
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    store.confirm_inactive_period(account_id, date(2025, 2, 1), date(2025, 2, 28), "No operations")
    with TestClient(create_app(root, hash_password("synthetic-password-123"), secure_cookie=False),
                    follow_redirects=False) as client:
        assert client.get("/export/gaps.csv").status_code == 401
        client.get("/login")
        assert client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                           "password": "synthetic-password-123"}).status_code == 303
        response = client.get("/export/gaps.csv")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        rows = list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"))))
        assert len(rows) == 1
        assert rows[0]["account_number_status"] == "unknown"
        assert rows[0]["record_type"] == "owner_statement"
        assert rows[0]["status"] == "owner_confirmed"
