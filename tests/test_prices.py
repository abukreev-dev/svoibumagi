"""MOEX quote controls with a synthetic ISS response. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import date
from decimal import Decimal as D
import csv
from io import StringIO
from urllib.parse import parse_qs, urlsplit

import pytest

from svoibumagi.prices import MoexQuote, MoexSecurity, PriceUnavailable, find_moex_security, get_moex_bond_price, get_moex_share_close, get_moex_share_quote
from svoibumagi.exports import positions_csv, valuations_csv
from svoibumagi.storage import Storage


ISIN = "RU000A000001"


def test_moex_batch_is_atomic_and_idempotent(tmp_path):
    store = Storage(tmp_path / "private")
    day = date(2025, 1, 31)
    values = [(MoexSecurity(isin, secid, "TQBR", "share"),
               MoexQuote(isin, secid, D(amount), day, "https://iss.moex.com/synthetic"))
              for isin, secid, amount in (("RU000A000001", "SYN1", "10"),
                                          ("RU000A000002", "SYN2", "20"))]
    with store._connect() as con:
        con.execute("CREATE TRIGGER reject_second BEFORE INSERT ON prices "
                    "WHEN NEW.isin='RU000A000002' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(Exception, match="synthetic failure"):
        store.add_moex_prices_batch(values)
    assert store.latest_prices(day) == {}
    with store._connect() as con:
        con.execute("DROP TRIGGER reject_second")
    assert store.add_moex_prices_batch(values) == 2
    assert store.add_moex_prices_batch(values) == 0
    assert {key: value["amount"] for key, value in store.latest_prices(day).items()} == {
        "RU000A000001": "10", "RU000A000002": "20"}
    bad = (values[1][0], MoexQuote("RU000A000003", "SYN2", D("30"), day,
                                   "https://iss.moex.com/synthetic"))
    with pytest.raises(ValueError, match="mismatched"):
        store.add_moex_prices_batch([values[0], bad])


def _response(*, isin=ISIN, last="100.25", traded="2025-01-31"):
    return {
        "securities": {"columns": ["CURRENCYID", "ISIN", "SECID", "BOARDID"],
                       "data": [["SUR", isin, "SYN", "TQBR"]]},
        "marketdata": {"columns": ["LASTTRADEDATE", "LAST", "BOARDID", "SECID"],
                       "data": [[traded, last, "TQBR", "SYN"]]},
    }


def test_tqbr_quote_verifies_identity_date_and_decimal(tmp_path):
    seen = []
    quote = get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: seen.append(url) or _response())
    assert quote.amount == D("100.25")
    assert quote.as_of == date(2025, 1, 31)
    assert "/TQBR/securities/SYN.json" in seen[0]
    store = Storage(tmp_path / "private")
    store.add_price(quote.isin, quote.amount, quote.as_of, source="moex", note=quote.url)
    assert store.latest_prices()[ISIN]["source"] == "moex"
    store.add_price(ISIN, D("99"), quote.as_of, note="owner override")
    assert store.latest_prices()[ISIN]["amount"] == "99"
    store.add_price(ISIN, D("101"), quote.as_of, source="moex", note="later quote")
    assert store.latest_prices()[ISIN]["amount"] == "99"


def test_tqbr_live_shape_uses_proven_trade_date_when_lasttradedate_absent():
    document = _response()
    document["marketdata"]["columns"] = ["LAST", "BOARDID", "SECID", "NUMTRADES", "SYSTIME"]
    document["marketdata"]["data"] = [["100.25", "TQBR", "SYN", 55, "2025-01-31 15:52:44"]]
    document["dataversion"] = {"columns": ["trade_date"], "data": [["2025-01-31"]]}
    urls = []
    quote = get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: urls.append(url) or document)
    assert quote.amount == D("100.25") and quote.as_of == date(2025, 1, 31)
    assert parse_qs(urlsplit(urls[0]).query)["iss.only"] == ["securities,marketdata,dataversion"]
    document["marketdata"]["data"][0][3] = 0
    with pytest.raises(PriceUnavailable):
        get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: document)
    document["marketdata"]["data"][0][3] = "NaN"
    with pytest.raises(PriceUnavailable):
        get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: document)
    document["marketdata"]["data"][0][3] = 55
    document["marketdata"]["data"][0][4] = "2025-02-01 00:00:01"
    with pytest.raises(PriceUnavailable):
        get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: document)


def test_tqbr_quote_date_uses_moscow_trading_day(monkeypatch):
    monkeypatch.setattr("svoibumagi.prices._today_moscow", lambda: date(2025, 1, 31))
    assert get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: _response()).as_of == date(2025, 1, 31)
    monkeypatch.setattr("svoibumagi.prices._today_moscow", lambda: date(2025, 1, 30))
    with pytest.raises(PriceUnavailable, match="date is invalid"):
        get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: _response())


@pytest.mark.parametrize("document", [
    _response(isin="RU000A000002"),
    _response(last=None),
    _response(traded=""),
    {"securities": {"columns": ["SECID"], "data": [["SYN"]]}},
])
def test_moex_quote_rejects_unproven_value(document):
    with pytest.raises(PriceUnavailable):
        get_moex_share_quote(ISIN, "SYN", fetch_json=lambda url: document)


def test_moex_secid_cannot_change_request_host():
    with pytest.raises(PriceUnavailable, match="Invalid"):
        get_moex_share_quote(ISIN, "../private", fetch_json=lambda url: _response())


def test_historical_tqbr_close_uses_latest_trade_within_seven_days(monkeypatch):
    monkeypatch.setattr("svoibumagi.prices._today_moscow", lambda: date(2025, 2, 8))
    seen = []
    security = {"securities": {"columns": ["SECID", "ISIN", "BOARDID", "CURRENCYID"],
                               "data": [["SYN", ISIN, "TQBR", "SUR"]]}}
    history = {"history": {"columns": ["SECID", "BOARDID", "CURRENCYID", "TRADEDATE", "CLOSE", "NUMTRADES"],
                           "data": [["SYN", "TQBR", "SUR", "2025-02-06", "101.25", 10],
                                    ["SYN", "TQBR", "SUR", "2025-02-07", "102.50", 12]]}}

    def fetch(url):
        seen.append(url)
        return history if "/history/" in url else security

    quote = get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)
    assert (quote.amount, quote.as_of) == (D("102.50"), date(2025, 2, 7))
    assert parse_qs(urlsplit(seen[1]).query)["from"] == ["2025-02-01"]
    assert parse_qs(urlsplit(seen[1]).query)["till"] == ["2025-02-08"]
    history["history"]["data"][1][2] = "USD"
    with pytest.raises(PriceUnavailable, match="currency"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)
    history["history"]["data"][1][2] = "SUR"
    history["history"]["data"] = []
    with pytest.raises(PriceUnavailable, match="no confirmed"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)
    with pytest.raises(PriceUnavailable, match="Invalid"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 9), fetch_json=fetch)


def test_historical_tqbr_close_rejects_wrong_identity_no_trade_and_ambiguous_day(monkeypatch):
    monkeypatch.setattr("svoibumagi.prices._today_moscow", lambda: date(2025, 2, 8))
    security = {"securities": {"columns": ["SECID", "ISIN", "BOARDID", "CURRENCYID"],
                               "data": [["SYN", "RU000A000002", "TQBR", "SUR"]]}}
    history = {"history": {"columns": ["SECID", "BOARDID", "CURRENCYID", "TRADEDATE", "CLOSE", "NUMTRADES"],
                           "data": [["SYN", "TQBR", "SUR", "2025-02-07", "100", 0]]}}

    def fetch(url):
        return history if "/history/" in url else security

    with pytest.raises(PriceUnavailable, match="ISIN"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)
    security["securities"]["data"][0][1] = ISIN
    with pytest.raises(PriceUnavailable, match="no confirmed"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)
    history["history"]["data"][0][5] = "NaN"
    with pytest.raises(PriceUnavailable, match="not finite"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)
    history["history"]["data"][0][5] = 10
    history["history"]["data"].append(list(history["history"]["data"][0]))
    with pytest.raises(PriceUnavailable, match="ambiguous"):
        get_moex_share_close(ISIN, "SYN", date(2025, 2, 8), fetch_json=fetch)


def test_bond_dirty_rub_price_uses_dated_face_value_and_accrued_interest(monkeypatch):
    monkeypatch.setattr("svoibumagi.prices._today_moscow", lambda: date(2025, 2, 8))
    security = {"securities": {"columns": ["SECID", "ISIN", "BOARDID"],
                               "data": [["SYNBOND", ISIN, "TQOB"]]}}
    history = {"history": {"columns": ["SECID", "BOARDID", "TRADEDATE", "NUMTRADES", "CLOSE",
                                       "FACEVALUE", "ACCINT", "CURRENCYID", "FACEUNIT"],
                           "data": [["SYNBOND", "TQOB", "2025-02-07", 40, "62.467",
                                     "1000", "7.38", "SUR", "RUB"]]}}
    seen = []

    def fetch(url):
        seen.append(url)
        return history if "/history/" in url else security

    quote = get_moex_bond_price(ISIN, "SYNBOND", "TQOB", date(2025, 2, 8), fetch_json=fetch)
    assert quote.amount == D("632.050") and quote.as_of == date(2025, 2, 7)
    assert "/markets/bonds/boards/TQOB/" in seen[1]
    assert parse_qs(urlsplit(seen[1]).query)["from"] == ["2025-02-01"]
    history["history"]["data"][0][5] = "800"
    assert get_moex_bond_price(ISIN, "SYNBOND", "TQOB", date(2025, 2, 8), fetch_json=fetch).amount == D("507.116")
    history["history"]["data"][0][6] = None
    with pytest.raises(PriceUnavailable, match="accrued interest"):
        get_moex_bond_price(ISIN, "SYNBOND", "TQOB", date(2025, 2, 8), fetch_json=fetch)
    history["history"]["data"][0][6] = "7.38"
    history["history"]["data"][0][8] = "USD"
    with pytest.raises(PriceUnavailable, match="not in RUB"):
        get_moex_bond_price(ISIN, "SYNBOND", "TQOB", date(2025, 2, 8), fetch_json=fetch)
    with pytest.raises(PriceUnavailable, match="Invalid"):
        get_moex_bond_price(ISIN, "SYNBOND", "../../", date(2025, 2, 8), fetch_json=fetch)


def test_moex_isin_lookup_accepts_only_one_traded_supported_board():
    columns = ["isin", "secid", "is_traded", "group", "primary_boardid", "marketprice_boardid"]
    document = {"securities": {"columns": columns, "data": [
        [ISIN, "SYN", 1, "stock_shares", "TQBR", "TQBR"],
        [ISIN, "OLD", 0, "stock_shares", "EQBR", None],
        ["RU000A000002", "OTHER", 1, "stock_shares", "TQBR", "TQBR"],
    ]}}
    security = find_moex_security(ISIN, fetch_json=lambda url: document)
    assert (security.secid, security.board, security.kind) == ("SYN", "TQBR", "share")
    document["securities"]["data"][0] = [ISIN, "SYNBOND", 1, "stock_bonds", "TQCB", "TQCB"]
    assert find_moex_security(ISIN, fetch_json=lambda url: document).kind == "bond"
    document["securities"]["data"].append([ISIN, "ANOTHER", 1, "stock_bonds", "TQOB", "TQOB"])
    with pytest.raises(PriceUnavailable, match="unique"):
        find_moex_security(ISIN, fetch_json=lambda url: document)
    document["securities"]["data"][0][1] = "../private"
    document["securities"]["data"].pop()
    with pytest.raises(PriceUnavailable, match="unique"):
        find_moex_security(ISIN, fetch_json=lambda url: document)


@pytest.mark.parametrize("amount", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_manual_price_rejects_nonfinite_amount(tmp_path, amount):
    store = Storage(tmp_path / "private")
    with pytest.raises(ValueError, match="Invalid RUB price"):
        store.add_price(ISIN, amount, date(2025, 1, 31))
    assert store.latest_prices() == {}


def test_portfolio_estimate_marks_mixed_report_dates_incomplete(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    snapshots = []
    for account_id, report_date in ((1, "2025-01-31"), (2, "2025-02-28")):
        snapshots.append({"account": {"id": account_id, "name": f"Synthetic {account_id}"},
                          "period_end": report_date, "unparsed": [], "checks": [],
                          "positions": [{"isin": ISIN, "name": "Synthetic", "end_quantity": "1"}]})
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: snapshots)
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {ISIN: {"amount": "100", "as_of": "2025-02-21"}})
    estimate = store.portfolio_estimate()
    assert estimate["known_securities_subtotal"] == D("200")
    assert estimate["mixed_report_dates"] is True
    assert estimate["incomplete"] is True
    assert estimate["report_dates"] == ["2025-01-31", "2025-02-28"]


def test_price_older_than_seven_days_is_visible_but_excluded_from_value(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    account = {"id": 1, "name": "Synthetic"}
    monkeypatch.setattr(store, "list_accounts", lambda: [account])
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: [{
        "account": account, "period_end": "2025-01-31", "unparsed": [], "checks": [],
        "positions": [{"isin": ISIN, "name": "Synthetic", "end_quantity": "2"}],
    }])
    quote = {"amount": "100", "as_of": "2025-01-23", "source": "manual"}
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {ISIN: quote})
    stale = store.portfolio_estimate(date(2025, 1, 31))
    assert stale["positions"][0]["price"] == quote
    assert stale["positions"][0]["price_status"] == "stale"
    assert stale["known_securities_subtotal"] == 0
    assert stale["stale_prices"] == 1 and stale["missing_prices"] == 0 and stale["incomplete"]
    rows = list(csv.reader(StringIO(valuations_csv(stale).decode("utf-8-sig"))))
    assert rows[1][7] == "2025-01-23" and rows[1][10] == "stale_price"
    quote["as_of"] = "2025-01-24"
    fresh = store.portfolio_estimate(date(2025, 1, 31))
    assert fresh["known_securities_subtotal"] == D("200") and fresh["stale_prices"] == 0


def test_valuation_csv_preserves_independent_report_position_and_price_statuses(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    account = {"id": 1, "name": "Synthetic", "account_number": "SYN123"}
    monkeypatch.setattr(store, "list_accounts", lambda: [account])
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: [{
        "account": account, "period_end": "2025-01-31",
        "unparsed": [{"reason": "synthetic unresolved row"}], "checks": [],
        "positions": [{"isin": ISIN, "name": "Synthetic", "end_quantity": "2"}],
    }])
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {
        ISIN: {"amount": "100", "as_of": "2025-01-20", "source": "manual"}})
    estimate = store.portfolio_estimate(date(2025, 2, 10))
    row = next(csv.DictReader(StringIO(valuations_csv(estimate).decode("utf-8-sig"))))
    assert row["status"] == "stale_price"
    assert row["report_check_status"] == "incomplete"
    assert row["position_date_status"] == "stale"
    assert row["price_status"] == "stale"
    assert row["value_rub"] == "" and row["account_number"] == "SYN123"


def test_missing_report_checks_never_export_as_matched(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    account = {"id": 1, "name": "Synthetic", "account_number": "SYN123"}
    snapshot = {"account": account, "period_end": "2025-01-31", "unparsed": [], "checks": [],
                "positions": [{"isin": ISIN, "name": "Synthetic", "end_quantity": "2"}]}
    monkeypatch.setattr(store, "list_accounts", lambda: [account])
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: [snapshot])
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {
        ISIN: {"amount": "100", "as_of": "2025-01-31", "source": "manual"}})
    estimate = store.portfolio_estimate(date(2025, 1, 31))
    assert estimate["incomplete"] and estimate["positions"][0]["account_incomplete"]
    valuation = next(csv.DictReader(StringIO(valuations_csv(estimate).decode("utf-8-sig"))))
    position = next(csv.DictReader(StringIO(positions_csv([snapshot]).decode("utf-8-sig"))))
    assert valuation["report_check_status"] == "incomplete"
    assert position["status"] == "incomplete"


def test_zero_position_needs_no_price_and_exports_known_zero(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    account = store.list_accounts()[0]
    assert account["id"] == account_id
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: [{
        "account": account, "period_end": "2025-01-31", "unparsed": [],
        "checks": [{"status": "matched"}],
        "positions": [{"isin": ISIN, "name": "Closed", "end_quantity": "0"}],
    }])
    estimate = store.portfolio_estimate(date(2025, 1, 31))
    assert estimate["positions"][0]["value"] == D(0)
    assert estimate["missing_prices"] == 0 and not estimate["incomplete"]
    rows = list(csv.reader(StringIO(valuations_csv(estimate).decode("utf-8-sig"))))
    assert rows[1][9:11] == ["0", "zero_position"]


def test_foreign_position_is_visible_without_false_ruble_value(tmp_path, monkeypatch):
    store = Storage(tmp_path / "private")
    account = {"id": 1, "name": "Synthetic"}
    monkeypatch.setattr(store, "list_accounts", lambda: [account])
    monkeypatch.setattr(store, "latest_snapshots", lambda as_of=None: [{
        "account": account, "period_end": "2025-01-31", "unparsed": [], "checks": [],
        "positions": [{"isin": ISIN, "name": "Foreign synthetic", "end_quantity": "2", "currency": "USD"}],
    }])
    monkeypatch.setattr(store, "latest_prices", lambda as_of=None: {
        ISIN: {"amount": "100", "as_of": "2025-01-31", "source": "manual"}})
    estimate = store.portfolio_estimate(date(2025, 1, 31))
    assert estimate["incomplete"] and estimate["conversion_required"] == 1
    assert estimate["known_securities_subtotal"] == 0
    assert estimate["positions"][0]["value"] is None
    row = next(csv.DictReader(StringIO(valuations_csv(estimate).decode("utf-8-sig"))))
    assert row["value_rub"] == "" and row["status"] == "currency_conversion_required"
    assert row["instrument"].endswith("(USD)")
