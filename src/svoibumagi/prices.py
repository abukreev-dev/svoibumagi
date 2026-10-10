"""Conservative MOEX ISS share quote reader. SPDX-License-Identifier: GPL-3.0-or-later"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Callable
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo


ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
SECID = re.compile(r"[A-Z0-9.-]{1,24}")
BOND_BOARDS = {"TQOB", "TQCB"}
MAX_RESPONSE_BYTES = 1_000_000
MAX_PRICE_AGE_DAYS = 7


def price_age_days(price_date: date | str, valuation_date: date) -> int:
    quoted = date.fromisoformat(price_date) if isinstance(price_date, str) else price_date
    return (valuation_date - quoted).days


def _today_moscow() -> date:
    return datetime.now(ZoneInfo("Europe/Moscow")).date()


class PriceUnavailable(ValueError):
    """The response cannot prove a dated RUB price for the requested share."""


@dataclass(frozen=True)
class MoexQuote:
    isin: str
    secid: str
    amount: Decimal
    as_of: date
    url: str


@dataclass(frozen=True)
class MoexSecurity:
    isin: str
    secid: str
    board: str
    kind: str


def _rows(document: dict, block: str) -> list[dict]:
    table = document.get(block)
    if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("data"), list):
        raise PriceUnavailable(f"MOEX {block} block is missing")
    columns = table["columns"]
    if len(set(columns)) != len(columns):
        raise PriceUnavailable(f"MOEX {block} columns are ambiguous")
    result = []
    for values in table["data"]:
        if not isinstance(values, list) or len(values) != len(columns):
            raise PriceUnavailable(f"MOEX {block} row does not match columns")
        result.append(dict(zip(columns, values)))
    return result


def _download(url: str) -> dict:
    try:
        with urlopen(url, timeout=8) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise PriceUnavailable("MOEX response exceeds limit")
        document = json.loads(payload, parse_float=Decimal)
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
        raise PriceUnavailable("MOEX quote request failed") from exc
    if not isinstance(document, dict):
        raise PriceUnavailable("MOEX response is not an object")
    return document


def find_moex_security(isin: str, *, fetch_json: Callable[[str], dict] = _download) -> MoexSecurity:
    """Resolve one currently traded TQBR share or TQOB/TQCB bond by exact ISIN."""
    isin = isin.strip().upper()
    if not ISIN.fullmatch(isin):
        raise PriceUnavailable("Invalid ISIN")
    url = "https://iss.moex.com/iss/securities.json?" + urlencode({
        "q": isin, "iss.meta": "off", "iss.only": "securities"})
    candidates = []
    for row in _rows(fetch_json(url), "securities"):
        board = row.get("primary_boardid")
        group = row.get("group")
        if (row.get("isin") == isin and row.get("is_traded") == 1
                and row.get("marketprice_boardid") == board
                and ((board == "TQBR" and group == "stock_shares")
                     or (board in BOND_BOARDS and group == "stock_bonds"))
                and SECID.fullmatch(str(row.get("secid", "")))):
            candidates.append(MoexSecurity(isin, row["secid"], board,
                                           "share" if board == "TQBR" else "bond"))
    if len(candidates) != 1:
        raise PriceUnavailable("MOEX has no unique supported security for this ISIN")
    return candidates[0]


def get_moex_share_quote(isin: str, secid: str, *, fetch_json: Callable[[str], dict] = _download) -> MoexQuote:
    """Read LAST for an explicitly selected TQBR share; verify exact ISIN."""
    isin, secid = isin.strip().upper(), secid.strip().upper()
    if not ISIN.fullmatch(isin) or not SECID.fullmatch(secid):
        raise PriceUnavailable("Invalid ISIN or TQBR SECID")
    base = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{secid}.json"
    url = base + "?" + urlencode({"iss.meta": "off", "iss.only": "securities,marketdata,dataversion"})
    document = fetch_json(url)
    securities = [row for row in _rows(document, "securities") if row.get("SECID") == secid]
    marketdata = [row for row in _rows(document, "marketdata") if row.get("SECID") == secid]
    if len(securities) != 1 or len(marketdata) != 1:
        raise PriceUnavailable("MOEX SECID is missing or ambiguous on TQBR")
    security, market = securities[0], marketdata[0]
    if security.get("ISIN") != isin:
        raise PriceUnavailable("MOEX ISIN does not match the requested instrument")
    if security.get("BOARDID") not in (None, "TQBR") or market.get("BOARDID") not in (None, "TQBR"):
        raise PriceUnavailable("MOEX quote is not from TQBR")
    if security.get("CURRENCYID") not in (None, "RUB", "SUR"):
        raise PriceUnavailable("MOEX quote is not in RUB")
    try:
        amount = Decimal(str(market["LAST"]))
        direct_date = market.get("LASTTRADEDATE") or security.get("LASTTRADEDATE")
        if direct_date:
            as_of = date.fromisoformat(str(direct_date))
        else:
            # The live TQBR response has no LASTTRADEDATE. A positive trade
            # count and matching market-system/trade dates bind LAST to a day.
            versions = _rows(document, "dataversion")
            trades = Decimal(str(market.get("NUMTRADES")))
            if len(versions) != 1 or not trades.is_finite() or trades <= 0:
                raise PriceUnavailable("MOEX last trade date is not proven")
            as_of = date.fromisoformat(str(versions[0]["trade_date"]))
            if date.fromisoformat(str(market["SYSTIME"])[:10]) != as_of:
                raise PriceUnavailable("MOEX market data date differs from trade date")
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise PriceUnavailable("MOEX last trade has no valid price or date") from exc
    if not amount.is_finite() or amount <= 0 or as_of > _today_moscow():
        raise PriceUnavailable("MOEX last trade price or date is invalid")
    return MoexQuote(isin, secid, amount, as_of, url)


def get_moex_share_close(isin: str, secid: str, valuation_date: date,
                         *, fetch_json: Callable[[str], dict] = _download) -> MoexQuote:
    """Read a dated TQBR close within the supported freshness window."""
    isin, secid = isin.strip().upper(), secid.strip().upper()
    if not ISIN.fullmatch(isin) or not SECID.fullmatch(secid) or valuation_date > _today_moscow():
        raise PriceUnavailable("Invalid ISIN, TQBR SECID or valuation date")
    security_url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
                    f"securities/{secid}.json?" + urlencode({"iss.meta": "off", "iss.only": "securities"}))
    securities = [row for row in _rows(fetch_json(security_url), "securities") if row.get("SECID") == secid]
    if len(securities) != 1 or securities[0].get("ISIN") != isin:
        raise PriceUnavailable("MOEX SECID does not identify the requested ISIN on TQBR")
    if securities[0].get("BOARDID") not in (None, "TQBR") or securities[0].get("CURRENCYID") not in (None, "RUB", "SUR"):
        raise PriceUnavailable("MOEX security is not a RUB share on TQBR")
    history_url = (f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/"
                   f"securities/{secid}.json?" + urlencode({
                       "from": (valuation_date - timedelta(days=MAX_PRICE_AGE_DAYS)).isoformat(),
                       "till": valuation_date.isoformat(), "iss.meta": "off", "iss.only": "history"}))
    accepted = []
    for row in _rows(fetch_json(history_url), "history"):
        if row.get("SECID") != secid or row.get("BOARDID") != "TQBR" or row.get("CURRENCYID") not in ("RUB", "SUR"):
            raise PriceUnavailable("MOEX historical row has another security, board or currency")
        try:
            day = date.fromisoformat(str(row["TRADEDATE"]))
            amount = Decimal(str(row["CLOSE"]))
            trades = Decimal(str(row["NUMTRADES"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise PriceUnavailable("MOEX historical close has invalid price, date or trade count") from exc
        if not valuation_date - timedelta(days=MAX_PRICE_AGE_DAYS) <= day <= valuation_date:
            raise PriceUnavailable("MOEX historical row is outside requested date window")
        if not trades.is_finite():
            raise PriceUnavailable("MOEX historical trade count is not finite")
        if trades > 0 and amount.is_finite() and amount > 0:
            accepted.append((day, amount))
    if not accepted:
        raise PriceUnavailable("MOEX has no confirmed TQBR close within seven calendar days")
    newest = max(day for day, _ in accepted)
    values = [amount for day, amount in accepted if day == newest]
    if len(values) != 1:
        raise PriceUnavailable("MOEX has ambiguous TQBR closing rows on the latest trading day")
    return MoexQuote(isin, secid, values[0], newest, history_url)


def get_moex_bond_price(isin: str, secid: str, board: str, valuation_date: date,
                        *, fetch_json: Callable[[str], dict] = _download) -> MoexQuote:
    """Convert a dated RUB bond close and accrued interest to one dirty RUB price."""
    isin, secid, board = isin.strip().upper(), secid.strip().upper(), board.strip().upper()
    if (not ISIN.fullmatch(isin) or not SECID.fullmatch(secid) or board not in BOND_BOARDS
            or valuation_date > _today_moscow()):
        raise PriceUnavailable("Invalid bond ISIN, SECID, board or valuation date")
    base = f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/{board}/securities/{secid}.json"
    security_url = base + "?" + urlencode({"iss.meta": "off", "iss.only": "securities"})
    securities = [row for row in _rows(fetch_json(security_url), "securities") if row.get("SECID") == secid]
    if (len(securities) != 1 or securities[0].get("ISIN") != isin
            or securities[0].get("BOARDID") != board):
        raise PriceUnavailable("MOEX bond SECID, ISIN or board does not match")
    history_url = (f"https://iss.moex.com/iss/history/engines/stock/markets/bonds/boards/{board}/"
                   f"securities/{secid}.json?" + urlencode({
                       "from": (valuation_date - timedelta(days=MAX_PRICE_AGE_DAYS)).isoformat(),
                       "till": valuation_date.isoformat(), "iss.meta": "off", "iss.only": "history"}))
    accepted = []
    for row in _rows(fetch_json(history_url), "history"):
        if row.get("SECID") != secid or row.get("BOARDID") != board:
            raise PriceUnavailable("MOEX bond history identifies another instrument or board")
        if row.get("CURRENCYID") not in ("RUB", "SUR") or row.get("FACEUNIT") not in ("RUB", "SUR"):
            raise PriceUnavailable("MOEX bond price, nominal or accrued interest is not in RUB")
        try:
            day = date.fromisoformat(str(row["TRADEDATE"]))
            trades = Decimal(str(row["NUMTRADES"]))
            close_percent = Decimal(str(row["CLOSE"]))
            face = Decimal(str(row["FACEVALUE"]))
            accrued = Decimal(str(row["ACCINT"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise PriceUnavailable("MOEX bond close, nominal or accrued interest is missing") from exc
        if not valuation_date - timedelta(days=MAX_PRICE_AGE_DAYS) <= day <= valuation_date:
            raise PriceUnavailable("MOEX bond history is outside requested date window")
        if not all(value.is_finite() for value in (trades, close_percent, face, accrued)):
            raise PriceUnavailable("MOEX bond price components are not finite")
        if trades > 0 and close_percent > 0 and face > 0 and accrued >= 0:
            accepted.append((day, face * close_percent / Decimal(100) + accrued))
    if not accepted:
        raise PriceUnavailable("MOEX has no confirmed RUB bond price within seven calendar days")
    newest = max(day for day, _ in accepted)
    values = [amount for day, amount in accepted if day == newest]
    if len(values) != 1:
        raise PriceUnavailable("MOEX has ambiguous bond closing rows on the latest trading day")
    return MoexQuote(isin, secid, values[0], newest, history_url)


def resolve_moex_quote(isin: str, valuation_date: date,
                       *, fetch_json: Callable[[str], dict] = _download) -> tuple[MoexSecurity, MoexQuote]:
    """Resolve a supported security and its dated RUB quote without saving it."""
    security = find_moex_security(isin, fetch_json=fetch_json)
    quote = (get_moex_share_close(security.isin, security.secid, valuation_date, fetch_json=fetch_json)
             if security.kind == "share" else
             get_moex_bond_price(security.isin, security.secid, security.board, valuation_date,
                                 fetch_json=fetch_json))
    return security, quote
