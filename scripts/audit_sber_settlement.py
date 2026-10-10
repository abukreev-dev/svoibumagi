"""Independently compare Sber trade principal plus accrued interest to cash settlement.

Reads registered private HTML source tables directly; never writes source values.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys

from bs4 import BeautifulSoup


DAY = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _cells(row) -> list[str]:
    return [cell.get_text(" ", strip=True).replace("\u00a0", " ")
            for cell in row.find_all(["td", "th"], recursive=False)]


def _number(value: str) -> Decimal:
    return Decimal(value.replace(" ", "").replace(",", ".")) if value.strip() else Decimal(0)


def audit(registry_path: Path) -> Counter[str]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("schema") != 2:
        raise ValueError("Unsupported private registry schema")
    totals: Counter[str] = Counter()
    mismatches: list[str] = []
    seen: set[str] = set()
    for item in registry["reports"]:
        if item["broker"] != "sber" or item["sha256"] in seen:
            continue
        seen.add(item["sha256"])
        path = Path(item["path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered Sber source changed")
        trade_by_day: dict[str, Decimal] = defaultdict(Decimal)
        cash_by_day: dict[str, Decimal] = defaultdict(Decimal)
        trade_fees_by_day: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        cash_fees_by_day: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        trade_sections = cash_sections = 0
        for table in BeautifulSoup(path.read_bytes(), "html.parser").find_all("table"):
            rows = table.find_all("tr")
            header = _cells(rows[0]) if rows else []
            if not header:
                continue
            if header[0] == "Дата заключения" and "Номер сделки" in header:
                trade_sections += 1
                for row in rows[2:]:
                    cells = _cells(row)
                    if len(cells) != 16 or not DAY.fullmatch(cells[0]):
                        continue
                    if cells[15] in {"О", "ЗО"}:
                        totals["unsettled_trades"] += 1
                        continue
                    if cells[15] not in {"И", "ЗИ"} or cells[6] not in {"Покупка", "Продажа"} or not DAY.fullmatch(cells[1]):
                        raise ValueError(f"Unrecognized trade state or date: {item['sha256'][:12]}")
                    principal, accrued = _number(cells[9]), _number(cells[10])
                    sign = -1 if cells[6] == "Покупка" else 1
                    trade_by_day[cells[1]] += sign * (principal + accrued)
                    trade_fees_by_day[cells[1]][0] += _number(cells[11])
                    trade_fees_by_day[cells[1]][1] += _number(cells[12])
                    totals["settled_trade_rows"] += 1
                    totals["accrued_interest_rows"] += bool(accrued)
                    totals["unkeyed_trade_rows"] += not bool(cells[13])
            elif header[0] == "Дата" and "Описание операции" in header:
                cash_sections += 1
                for row in rows[2:]:
                    cells = _cells(row)
                    if len(cells) == 6 and DAY.fullmatch(cells[0]) and cells[2].startswith("Сделка"):
                        if cells[3] != "RUB":
                            raise ValueError("Non-RUB Sber settlement")
                        cash_by_day[cells[0]] += _number(cells[4]) - _number(cells[5])
                        totals["settlement_cash_rows"] += 1
                    elif len(cells) == 6 and DAY.fullmatch(cells[0]) and cells[2].startswith("Комиссия Брокера"):
                        cash_fees_by_day[cells[0]][0] += _number(cells[5]) - _number(cells[4])
                        totals["broker_fee_cash_rows"] += 1
                    elif len(cells) == 6 and DAY.fullmatch(cells[0]) and cells[2].startswith("Комиссия Биржи"):
                        cash_fees_by_day[cells[0]][1] += _number(cells[5]) - _number(cells[4])
                        totals["exchange_fee_cash_rows"] += 1
        if trade_sections > 1 or cash_sections > 1 or (bool(trade_by_day) != bool(cash_by_day)):
            raise ValueError(f"Ambiguous or incomplete settlement sections: {item['sha256'][:12]}")
        for day in trade_by_day.keys() | cash_by_day.keys():
            if trade_by_day[day] != cash_by_day[day]:
                mismatches.append(f"{item['sha256'][:12]}:{day}")
            else:
                totals["matched_settlement_dates"] += 1
        for day in trade_fees_by_day.keys() | cash_fees_by_day.keys():
            if trade_fees_by_day[day] != cash_fees_by_day[day]:
                mismatches.append(f"{item['sha256'][:12]}:{day}:fees")
            else:
                totals["matched_fee_dates"] += 1
        totals["reports"] += 1
    if mismatches:
        raise ValueError(f"Sber settlement mismatches: {len(mismatches)}; first: {mismatches[:8]}")
    return totals


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: audit_sber_settlement.py PRIVATE_REGISTRY")
    result = audit(Path(sys.argv[1]))
    print("; ".join(f"{key}={value}" for key, value in sorted(result.items())))
