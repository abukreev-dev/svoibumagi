"""Independently inventory all nonempty rows in recognized private Sber tables.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys

from bs4 import BeautifulSoup

from svoibumagi.sber import OVERVIEW_REASON, SERVICE_REASON, TOTAL_REASON, parse_sber


ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
DAY = re.compile(r"^\d{2}\.\d{2}\.\d{4}")


def _cells(tr) -> list[str]:
    return [cell.get_text(" ", strip=True).replace("\u00a0", " ")
            for cell in tr.find_all(["td", "th"], recursive=False)]


def _nonzero(value: str) -> bool:
    if not value.strip():
        return False
    try:
        return Decimal(value.replace(" ", "").replace(",", ".")) != 0
    except InvalidOperation:
        return True  # An unreadable balance needs parser review too.


def _money(value: str) -> Decimal:
    amount = Decimal(value.replace(" ", "").replace(",", ".")) if value.strip() else Decimal(0)
    if not amount.is_finite():
        raise ValueError("Non-finite source subtotal")
    return amount


def check(registry_path: Path) -> tuple[int, Counter[str]]:
    registry = json.loads(registry_path.read_text())
    checked = 0
    candidates: Counter[str] = Counter()
    missing = []
    for item in registry["reports"]:
        if item["broker"] != "sber":
            continue
        path = Path(item["path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered Sber report changed")
        report = parse_sber(path)
        classified = {(row.location.sheet, row.location.row)
                      for group in (report.positions, report.cash_snapshots, report.cash_movements,
                                    report.trades, report.summary_trades, report.unparsed, report.excluded)
                      for row in group}
        instrument_service = {(row.location.sheet, row.location.row) for row in report.excluded
                              if row.reason == "instrument reference used for ISIN mapping; no ledger event"}
        instrument_service.update((row.location.sheet, row.location.row) for row in report.unparsed)
        structural_service = {(row.location.sheet, row.location.row) for row in report.excluded
                              if row.reason == SERVICE_REASON}
        source_totals = {(row.location.sheet, row.location.row, int(row.key.rsplit(":", 1)[1])): row
                         for row in report.source_totals}
        total_service = {(row.location.sheet, row.location.row) for row in report.excluded
                         if row.reason == TOTAL_REASON}
        tables = BeautifulSoup(path.read_bytes(), "html.parser").find_all("table")
        for table_number, table in enumerate(tables):
            rows = table.find_all("tr")
            header = _cells(rows[0]) if rows else []
            if not header:
                continue
            section = header[0]
            if section not in {"Основной рынок", "Торговая площадка", "Дата", "Дата заключения", "Наименование"}:
                continue
            all_values = [_cells(tr) for tr in rows[2:]]
            for row_number, tr in enumerate(rows[2:], 3):
                values = _cells(tr)
                if not values:
                    continue
                economic = False
                if section == "Основной рынок":
                    economic = any(ISIN.fullmatch(value) for value in values)
                elif section == "Торговая площадка":
                    economic = (len(values) >= 6 and values[0].startswith("Торговый счет")
                                and (values[1] == "RUB" or _nonzero(values[3]) or _nonzero(values[5])))
                elif section in {"Дата", "Дата заключения"}:
                    economic = bool(values and DAY.match(values[0]))
                elif section == "Наименование":
                    economic = len(values) >= 3 and bool(ISIN.fullmatch(values[2]))
                structural = False
                if section == "Основной рынок" and "Конец периода" in " ".join(header):
                    structural = (values == ["Площадка: Фондовый рынок"] or
                                  len(values) == 18 and all(v.isdigit() for v in values))
                elif section == "Торговая площадка" and "Валюта" in " ".join(header):
                    structural = (len(values) == 10 and
                                  (all(v.isdigit() for v in values) or
                                   values[0] == "Основной рынок" and all(v.isdigit() for v in values[1:])) or
                                  len(values) == 9 and values[0].startswith("Торговый счет") and
                                  values[1] in {"EUR", "USD"} and not _nonzero(values[3]) and
                                  not _nonzero(values[5]))
                elif section == "Дата заключения" and "Номер сделки" in " ".join(header):
                    structural = values == ["Площадка: Фондовый рынок"]
                mapping = None
                predicate = None
                if section == "Основной рынок" and "Конец периода" in " ".join(header) and values[0] in {
                    "Итого по площадке Фондовый рынок, RUB", "Итого по Основному рынку, RUB"}:
                    mapping = {1: 6, 2: 7, 6: 11, 7: 12, 9: 14}
                    predicate = lambda a: len(a) == 18 and bool(ISIN.fullmatch(a[1]))
                elif section == "Торговая площадка" and "Валюта" in " ".join(header) and values[0] in {
                    "Итого по площадке Основной рынок", "Итого в рублевой оценке по курсам Банка России"}:
                    mapping = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8}
                    predicate = lambda a: len(a) == 9 and a[0].startswith("Торговый счет") and a[1] == "RUB"
                elif section == "Дата" and "Описание операции" in " ".join(header) and values[0] == "Итого, RUB":
                    mapping = {1: 4, 2: 5}
                    predicate = lambda a: len(a) == 6 and bool(DAY.match(a[0])) and a[3] == "RUB"
                elif section == "Дата заключения" and "Номер сделки" in " ".join(header) and values[0] == "Итого, RUB":
                    mapping = {1: 9, 2: 10, 3: 11, 4: 12}
                    predicate = lambda a: len(a) == 16 and bool(DAY.match(a[0])) and a[5] == "RUB"
                if mapping:
                    location = (f"table:{table_number}", row_number)
                    if location not in total_service:
                        missing.append((item["sha256"][:12], section, table_number, row_number, "total row"))
                    detail = [a for a in all_values if predicate(a)]
                    for field, column in mapping.items():
                        calculated = sum((_money(a[column]) for a in detail), Decimal(0))
                        reported = _money(values[field])
                        total = source_totals.get((*location, field))
                        if total is None or calculated != total.calculated or reported != total.reported or abs(calculated - reported) > Decimal("0.01"):
                            missing.append((item["sha256"][:12], section, table_number, row_number, field))
                if economic or structural or mapping:
                    location = (f"table:{table_number}", row_number)
                    recognized = (instrument_service if section == "Наименование" else
                                  total_service if mapping else
                                  structural_service if structural and not economic else classified)
                    if location not in recognized:
                        missing.append((item["sha256"][:12], section, table_number, row_number))
                if any(values):
                    candidates[section] += 1
                    if (f"table:{table_number}", row_number) not in classified:
                        missing.append((item["sha256"][:12], section, table_number, row_number, "unclassified nonempty row"))
        overview = []
        position_total = []
        cash_total = []
        for table_number, table in enumerate(tables):
            rows = [_cells(tr) for tr in table.find_all("tr")]
            if len(rows) < 2 or not rows[0]:
                continue
            if rows[0][0] == "Торговая площадка" and rows[1][:2] == ["Ценные бумаги", "Денежные средства"]:
                overview.append((table_number, rows))
            if rows[0][0] == "Основной рынок" and "Конец периода" in " ".join(rows[0]):
                position_total.extend(a for a in rows if a and a[0] == "Итого по Основному рынку, RUB")
            if rows[0][0] == "Торговая площадка" and "Валюта" in " ".join(rows[0]):
                cash_total.extend(a for a in rows if a and a[0] == "Итого по площадке Основной рынок")
        if overview:
            if len(overview) != 1 or len(position_total) != 1 or len(cash_total) != 1:
                raise ValueError("Sber overview has ambiguous source totals")
            table_number, rows = overview[0]
            data = [(a, n) for n, a in enumerate(rows, 1) if a and a[0] == "Основной рынок"]
            footer = [(a, n) for n, a in enumerate(rows, 1) if a and a[0] == "Итого"]
            if len(data) != 1 or len(footer) != 1:
                raise ValueError("Sber overview has ambiguous rows")
            detail, detail_number = data[0]
            summary, summary_number = footer[0]
            p, c = position_total[0], cash_total[0]
            expected = {1: _money(p[1]) + _money(p[2]), 2: _money(c[1]),
                        4: _money(p[6]) + _money(p[7]), 5: _money(c[3])}
            expected[3] = expected[1] + expected[2]
            expected[6] = expected[4] + expected[5]
            for field in range(7, 10):
                expected[field] = _money(detail[field - 3]) - _money(detail[field - 6])
            for row_number, values, field_values in ((detail_number, detail, expected),
                                                     (summary_number, summary,
                                                      {1: _money(detail[3]), 2: _money(detail[6]),
                                                       3: _money(detail[9])})):
                location = (f"table:{table_number}", row_number)
                if not any(row.reason == OVERVIEW_REASON and
                           (row.location.sheet, row.location.row) == location for row in report.excluded):
                    missing.append((item["sha256"][:12], "overview", table_number, row_number, "classification"))
                for field, calculated in field_values.items():
                    total = source_totals.get((*location, field))
                    if total is None or total.calculated != calculated or total.reported != _money(values[field]) or abs(total.calculated - total.reported) > Decimal("0.01"):
                        missing.append((item["sha256"][:12], "overview", table_number, row_number, field))
        checked += 1
    if missing:
        raise ValueError(f"Unclassified or mismatched Sber rows: {len(missing)}; first locations: {missing[:8]}")
    return checked, candidates


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_sber_source_rows.py PRIVATE_REGISTRY")
    count, totals = check(Path(sys.argv[1]))
    print(f"Checked {count} Sber reports and {sum(totals.values())} significant source rows; "
          f"unclassified: 0")
