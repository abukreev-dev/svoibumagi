"""Conservative Sber HTML broker-report parser. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from bs4 import BeautifulSoup

from .alfa import ISIN, UnsupportedReport, _num
from .reports import CashMovement, CashSnapshot, Location, ParsedReport, PositionSnapshot, SourceTotal, Trade, UnparsedRow, account_key


DATE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
PERIOD = re.compile(r"Отчет брокера за период с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})", re.I)
CONTRACT = re.compile(r"Договор(?P<iis>\s+на\s+ведение\s+индивидуального\s+инвестиционного\s+счета)?\s+(?P<code>[A-Z0-9]+)\s+от", re.I)
DIVIDEND_GROSS = re.compile(r"Дополнительная информация:\s*Дивиденды\s+([\d\s\u00a0]+(?:[.,]\d{1,2})?)\s+RUR\b", re.I)
ADAPTER_VERSION = "sber-html-market-v7"
SERVICE_REASON = "structural Sber row; no ledger event"
TOTAL_REASON = "broker subtotal; see source_total checks"
OVERVIEW_REASON = "portfolio overview; see source_total checks"


def _cells(tr) -> list[str]:
    return [cell.get_text(" ", strip=True).replace("\u00a0", " ") for cell in tr.find_all(["td", "th"], recursive=False)]


def _d(value: str):
    match = DATE.search(value)
    if not match:
        raise ValueError("missing date")
    return datetime.strptime(match.group(), "%d.%m.%Y").date()


def _n(value: str) -> Decimal:
    return _num(value) if value.strip() else Decimal(0)


def parse_sber(path: Path) -> ParsedReport:
    if path.suffix.lower() not in {".html", ".htm"}:
        raise UnsupportedReport("Sber adapter expects HTML report")
    if path.stat().st_size > 20_000_000:
        raise UnsupportedReport("HTML report exceeds parser limit")
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    text = soup.get_text(" ", strip=True)
    match = PERIOD.search(text)
    if not match:
        raise UnsupportedReport("Sber report signature or period missing")
    start, end = (_d(v) for v in match.groups())
    if start > end:
        raise UnsupportedReport("Report period ends before it starts")
    report = ParsedReport("sber", ADAPTER_VERSION, start, end)
    contracts = {(match.group("code").upper(), "iis" if match.group("iis") else "brokerage")
                 for match in CONTRACT.finditer(text)}
    if len(contracts) != 1:
        raise UnsupportedReport("Sber contract identity is missing or ambiguous")
    code, report.account_kind = next(iter(contracts))
    report.account_key = account_key("sber", code)
    report.account_number = code
    tables = soup.find_all("table")
    by_header: dict[str, list[tuple[int, list]]] = {}
    for index, table in enumerate(tables):
        trs = table.find_all("tr")
        if trs:
            h = _cells(trs[0])
            if h:
                by_header.setdefault(h[0], []).append((index, trs))

    def section(first: str, required: str, *, optional: bool = False):
        candidates = by_header.get(first, [])
        found = [(index, trs) for index, trs in candidates if required in " ".join(_cells(trs[0]))]
        if not found:
            if optional and not candidates:
                return (-1, [])
            raise UnsupportedReport(f"Sber section {first} not recognized")
        if len(found) != 1:
            raise UnsupportedReport(f"Sber section {first} is ambiguous")
        return found[0]

    position_index, position_rows = section("Основной рынок", "Конец периода")
    cash_index, cash_rows = section("Торговая площадка", "Валюта")
    movement_index, movement_rows = section("Дата", "Описание операции", optional=True)
    trade_index, trade_rows = section("Дата заключения", "Номер сделки", optional=True)
    instrument_index, instrument_rows = section("Наименование", "ISIN ценной бумаги", optional=not trade_rows)
    overview_candidates = [(index, trs) for index, trs in by_header.get("Торговая площадка", [])
                           if len(trs) > 1 and _cells(trs[1])[:2] == ["Ценные бумаги", "Денежные средства"]]
    if len(overview_candidates) > 1:
        raise UnsupportedReport("Sber portfolio overview is ambiguous")
    overview_index, overview_rows = overview_candidates[0] if overview_candidates else (-1, [])
    code_to_isin: dict[str, str] = {}
    for row_number, tr in enumerate(instrument_rows[2:], 3):
        fields = _cells(tr)
        if not any(fields):
            continue
        location = Location(f"table:{instrument_index}", row_number)
        if len(fields) >= 3 and ISIN.fullmatch(fields[2]):
            code = fields[1].strip()
            if not code:
                report.unparsed.append(UnparsedRow(location, "instrument reference has no security code"))
            elif code in code_to_isin and code_to_isin[code] != fields[2]:
                report.unparsed.append(UnparsedRow(location, "instrument reference conflicts with earlier ISIN"))
            else:
                code_to_isin[code] = fields[2]
                report.excluded.append(UnparsedRow(location, "instrument reference used for ISIN mapping; no ledger event"))
        else:
            report.unparsed.append(UnparsedRow(location, "unrecognized instrument reference row"))

    for i, tr in enumerate(position_rows[2:], 3):
        a = _cells(tr)
        if a and a[0].isdigit() and (len(a) < 2 or not ISIN.fullmatch(a[1])):
            if len(a) == 18 and all(field.isdigit() for field in a):
                report.excluded.append(UnparsedRow(Location(f"table:{position_index}", i), SERVICE_REASON))
            else:
                report.unparsed.append(UnparsedRow(Location(f"table:{position_index}", i), "unrecognized numbered position row"))
            continue
        if a == ["Площадка: Фондовый рынок"]:
            report.excluded.append(UnparsedRow(Location(f"table:{position_index}", i), SERVICE_REASON))
            continue
        if len(a) != 18:
            if any(ISIN.fullmatch(field) for field in a):
                report.unparsed.append(UnparsedRow(Location(f"table:{position_index}", i), "position row has unexpected column count"))
            continue
        isin = a[1].strip()
        if not ISIN.fullmatch(isin):
            report.unparsed.append(UnparsedRow(Location(f"table:{position_index}", i), "position without supported ISIN"))
            continue
        try:
            if a[2] != "RUB":
                raise ValueError("non-RUB security price")
            report.positions.append(PositionSnapshot(Location(f"table:{position_index}", i), end, isin, a[0], _n(a[3]), _n(a[8])))
        except ValueError as exc:
            report.unparsed.append(UnparsedRow(Location(f"table:{position_index}", i), f"position: {exc}"))

    for i, tr in enumerate(cash_rows[2:], 3):
        a = _cells(tr)
        if len(a) == 10 and (all(field.isdigit() for field in a) or
                             (a[0] == "Основной рынок" and all(field.isdigit() for field in a[1:]))):
            report.excluded.append(UnparsedRow(Location(f"table:{cash_index}", i), SERVICE_REASON))
            continue
        if a and a[0].startswith("Торговый счет") and len(a) != 9:
            report.unparsed.append(UnparsedRow(Location(f"table:{cash_index}", i), "cash snapshot row has unexpected column count"))
            continue
        if len(a) != 9 or not a[0].startswith("Торговый счет"):
            continue
        if a[1] != "RUB":
            if any(_n(v) != 0 for v in (a[3], a[5])):
                report.unparsed.append(UnparsedRow(Location(f"table:{cash_index}", i), "non-RUB cash balance"))
            elif a[1] in {"EUR", "USD"}:
                report.excluded.append(UnparsedRow(Location(f"table:{cash_index}", i), SERVICE_REASON))
            else:
                report.unparsed.append(UnparsedRow(Location(f"table:{cash_index}", i), "unsupported zero-balance currency"))
            continue
        try:
            report.cash_snapshots.append(CashSnapshot(Location(f"table:{cash_index}", i), end, _n(a[3]), _n(a[5]), "RUB"))
        except ValueError:
            report.unparsed.append(UnparsedRow(Location(f"table:{cash_index}", i), "invalid cash snapshot"))

    for i, tr in enumerate(movement_rows[2:], 3):
        a = _cells(tr)
        if a and DATE.search(a[0]) and len(a) != 6:
            report.unparsed.append(UnparsedRow(Location(f"table:{movement_index}", i), "cash movement row has unexpected column count"))
            continue
        if len(a) != 6 or not DATE.search(a[0]):
            continue
        loc = Location(f"table:{movement_index}", i)
        try:
            movement_date = _d(a[0])
            if not start <= movement_date <= end:
                raise ValueError("cash movement date outside report period")
            if a[3] != "RUB":
                raise ValueError("non-RUB cash movement")
            amount = _n(a[4]) - _n(a[5])
            desc = a[2]
            if desc.startswith("Зачисление д/с (купон "):
                kind = "coupon"
            elif desc.startswith("Списание д/с. Налог на доходы физ.лиц"):
                kind = "tax_withheld"
            elif desc.startswith(("Зачисление д/с", "Пополнение")):
                kind = "deposit"
            elif desc.startswith(("Вывод д/с", "Списание д/с")):
                kind = "withdrawal"
            elif desc.startswith("Сделка"):
                kind = "trade_cash"
            elif desc.startswith("Комиссия"):
                kind = "trade_fee"
            elif desc.startswith("НДФЛ"):
                kind = "tax_withheld"
            elif desc.startswith("Дивиденд"):
                kind = "dividend"
            elif desc.startswith("Купон"):
                kind = "coupon"
            elif desc.startswith("Выплата купонов"):
                kind = "coupon"
            elif desc.startswith("Выплата дивидендов"):
                kind = "dividend_net"
            else:
                raise ValueError("unknown cash movement type")
            if kind in {"dividend", "dividend_net"} and "Налог удержан" in desc:
                gross_values = DIVIDEND_GROSS.findall(desc)
                if len(gross_values) == 1:
                    try:
                        gross = _n(gross_values[0])
                    except ValueError:
                        gross = None
                    if gross is not None and gross.is_finite() and gross >= amount >= 0:
                        tax = gross - amount
                        report.cash_movements.append(CashMovement(loc, movement_date, "dividend", gross,
                                                                  "RUB", withheld_tax=tax))
                        if tax:
                            report.cash_movements.append(CashMovement(loc, movement_date, "tax_withheld", -tax,
                                                                      "RUB", component="dividend_tax"))
                        continue
                kind = "dividend_net"
                report.unparsed.append(UnparsedRow(loc, "dividend withholding amount not separately verified"))
            report.cash_movements.append(CashMovement(loc, movement_date, kind, amount, "RUB"))
        except ValueError as exc:
            report.unparsed.append(UnparsedRow(loc, f"cash: {exc}"))

    for i, tr in enumerate(trade_rows[2:], 3):
        a = _cells(tr)
        if a == ["Площадка: Фондовый рынок"]:
            report.excluded.append(UnparsedRow(Location(f"table:{trade_index}", i), SERVICE_REASON))
            continue
        if a and DATE.search(a[0]) and len(a) != 16:
            report.unparsed.append(UnparsedRow(Location(f"table:{trade_index}", i), "trade row has unexpected column count"))
            continue
        if len(a) != 16 or not DATE.search(a[0]):
            continue
        loc = Location(f"table:{trade_index}", i)
        try:
            isin = a[4] if ISIN.fullmatch(a[4]) else code_to_isin.get(a[4])
            if a[5] != "RUB" or not isin:
                raise ValueError("unsupported currency or security code")
            if a[6] not in {"Покупка", "Продажа"}:
                raise ValueError("unknown trade side")
            if a[15] in {"О", "ЗО"}:
                report.excluded.append(UnparsedRow(loc, f"not executed at report end: {a[15]}"))
                continue
            if a[15] not in {"И", "ЗИ"}:
                raise ValueError("unknown trade execution status")
            qty = _n(a[7]) * (1 if a[6] == "Покупка" else -1)
            if qty == 0:
                raise ValueError("zero trade quantity")
            traded_at, settled_at = _d(a[0]), _d(a[1])
            if settled_at < traded_at:
                raise ValueError("trade settlement precedes execution")
            if not start <= settled_at <= end:
                raise ValueError("executed trade settlement outside report period")
            trade = Trade(loc, a[13] or None, traded_at, settled_at, isin, a[3], qty, _n(a[8]), _n(a[9]), _n(a[11]) + _n(a[12]), "RUB", _n(a[10]))
            if trade.broker_id is None:
                report.summary_trades.append(trade)
                report.unparsed.append(UnparsedRow(loc, "trade without broker ID may aggregate detailed executions; manual matching required"))
            else:
                report.trades.append(trade)
        except ValueError as exc:
            report.unparsed.append(UnparsedRow(loc, f"trade: {exc}"))

    def check_totals(index: int, rows: list, labels: set[str], mapping: dict[int, int],
                     detail):
        if not rows:
            return
        details = [_cells(tr) for tr in rows[2:]]
        data = [a for a in details if detail(a)]
        for row_number, a in enumerate(details, 3):
            if not a or a[0] not in labels:
                continue
            loc = Location(f"table:{index}", row_number)
            if len(a) <= max(mapping) or not data:
                report.unparsed.append(UnparsedRow(loc, "broker subtotal has no comparable detail rows"))
                continue
            try:
                totals = [(field, sum((_n(row[column]) for row in data), Decimal(0)), _n(a[field]))
                          for field, column in mapping.items()]
            except (IndexError, ValueError):
                report.unparsed.append(UnparsedRow(loc, "broker subtotal contains unreadable numeric cells"))
                continue
            for field, calculated, reported in totals:
                if not a[field].strip():
                    report.unparsed.append(UnparsedRow(loc, f"broker subtotal field {field} is empty"))
                    break
                report.source_totals.append(SourceTotal(loc, f"{loc.sheet}:{loc.row}:field:{field}", calculated, reported))
            else:
                report.excluded.append(UnparsedRow(loc, TOTAL_REASON))

    check_totals(position_index, position_rows,
                 {"Итого по площадке Фондовый рынок, RUB", "Итого по Основному рынку, RUB"},
                 {1: 6, 2: 7, 6: 11, 7: 12, 9: 14},
                 lambda a: len(a) == 18 and ISIN.fullmatch(a[1]))
    check_totals(cash_index, cash_rows,
                 {"Итого по площадке Основной рынок", "Итого в рублевой оценке по курсам Банка России"},
                 {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8},
                 lambda a: len(a) == 9 and a[0].startswith("Торговый счет") and a[1] == "RUB")
    check_totals(movement_index, movement_rows, {"Итого, RUB"}, {1: 4, 2: 5},
                 lambda a: len(a) == 6 and bool(DATE.search(a[0])) and a[3] == "RUB")
    check_totals(trade_index, trade_rows, {"Итого, RUB"}, {1: 9, 2: 10, 3: 11, 4: 12},
                 lambda a: len(a) == 16 and bool(DATE.search(a[0])) and a[5] == "RUB")

    if overview_rows:
        overview = [(_cells(tr), row_number) for row_number, tr in enumerate(overview_rows[2:], 3)]
        numbered = [(a, row_number) for a, row_number in overview
                    if len(a) == 10 and all(field.isdigit() for field in a)]
        for _, row_number in numbered:
            report.excluded.append(UnparsedRow(Location(f"table:{overview_index}", row_number), SERVICE_REASON))
        data_rows = [(a, row_number) for a, row_number in overview if a and a[0] == "Основной рынок"]
        footer_rows = [(a, row_number) for a, row_number in overview if a and a[0] == "Итого"]
        pos_totals = [(_cells(tr), row_number) for row_number, tr in enumerate(position_rows[2:], 3)
                      if _cells(tr) and _cells(tr)[0] == "Итого по Основному рынку, RUB"]
        cash_totals = [(_cells(tr), row_number) for row_number, tr in enumerate(cash_rows[2:], 3)
                       if _cells(tr) and _cells(tr)[0] == "Итого по площадке Основной рынок"]
        if len(data_rows) == len(footer_rows) == len(pos_totals) == len(cash_totals) == 1:
            data, data_row = data_rows[0]
            footer, footer_row = footer_rows[0]
            position_total = pos_totals[0][0]
            cash_total = cash_totals[0][0]
            try:
                if len(data) != 10 or len(footer) != 4:
                    raise ValueError("unexpected overview column count")
                expected = {1: _n(position_total[1]) + _n(position_total[2]),
                            2: _n(cash_total[1]),
                            4: _n(position_total[6]) + _n(position_total[7]),
                            5: _n(cash_total[3])}
                expected[3] = expected[1] + expected[2]
                expected[6] = expected[4] + expected[5]
                for field in range(1, 7):
                    report.source_totals.append(SourceTotal(Location(f"table:{overview_index}", data_row),
                                                            f"table:{overview_index}:{data_row}:field:{field}",
                                                            expected[field], _n(data[field])))
                for field in range(7, 10):
                    report.source_totals.append(SourceTotal(Location(f"table:{overview_index}", data_row),
                                                            f"table:{overview_index}:{data_row}:field:{field}",
                                                            _n(data[field - 3]) - _n(data[field - 6]), _n(data[field])))
                for field, column in {1: 3, 2: 6, 3: 9}.items():
                    report.source_totals.append(SourceTotal(Location(f"table:{overview_index}", footer_row),
                                                            f"table:{overview_index}:{footer_row}:field:{field}",
                                                            _n(data[column]), _n(footer[field])))
                report.excluded.extend((UnparsedRow(Location(f"table:{overview_index}", data_row), OVERVIEW_REASON),
                                        UnparsedRow(Location(f"table:{overview_index}", footer_row), OVERVIEW_REASON)))
            except (IndexError, ValueError) as exc:
                report.unparsed.append(UnparsedRow(Location(f"table:{overview_index}", data_row),
                                                   f"portfolio overview: {exc}"))
        else:
            report.unparsed.append(UnparsedRow(Location(f"table:{overview_index}", 1),
                                               "portfolio overview lacks unique detail and subtotal rows"))

    classified = {(item.location.sheet, item.location.row)
                  for group in (report.trades, report.summary_trades, report.cash_movements,
                                report.positions, report.cash_snapshots, report.unparsed, report.excluded)
                  for item in group}
    for index in {position_index, cash_index, movement_index, trade_index, instrument_index, overview_index} - {-1}:
        for row_number, tr in enumerate(tables[index].find_all("tr")[2:], 3):
            if (f"table:{index}", row_number) not in classified and any(_cells(tr)):
                report.unparsed.append(UnparsedRow(Location(f"table:{index}", row_number),
                                                   "unrecognized nonempty Sber source row"))
    return report
