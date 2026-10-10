"""Compare source descriptions of Sber cash rows with adapter event kinds.

The detailed output is private because it identifies report sources.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from bs4 import BeautifulSoup

from svoibumagi.sber import parse_sber


DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _number(value: str) -> Decimal:
    number = Decimal(value.replace("\u00a0", "").replace(" ", "").replace(",", ".")) if value.strip() else Decimal(0)
    if not number.is_finite():
        raise ValueError("Non-finite source cash amount")
    return number


def _kind(description: str) -> str:
    if description.startswith("Зачисление д/с (купон "):
        return "coupon"
    if description.startswith("Списание д/с. Налог на доходы физ.лиц"):
        return "tax_withheld"
    if description.startswith(("Зачисление д/с", "Пополнение")):
        return "deposit"
    if description.startswith(("Списание д/с", "Вывод д/с")):
        return "withdrawal"
    if description.startswith("Сделка"):
        return "trade_cash"
    if description.startswith("Комиссия"):
        return "trade_fee"
    if description.startswith("НДФЛ"):
        return "tax_withheld"
    if description.startswith(("Купон", "Выплата купонов")):
        return "coupon"
    if description.startswith(("Дивиденд", "Выплата дивидендов")):
        return "dividend"
    raise ValueError(f"Unknown Sber cash description: {description[:80]}")


def audit(registry_path: Path) -> dict:
    registry = json.loads(registry_path.read_text())
    counts: Counter[str] = Counter()
    coupon_credits = []
    tax_debits = []
    for item in registry["reports"]:
        if item["broker"] != "sber":
            continue
        path = Path(item["path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered Sber source changed")
        parsed = parse_sber(path)
        by_row = defaultdict(list)
        for movement in parsed.cash_movements:
            by_row[(movement.location.sheet, movement.location.row)].append(movement)
        source_locations = set()
        for table_number, table in enumerate(BeautifulSoup(path.read_bytes(), "html.parser").find_all("table")):
            rows = table.find_all("tr")
            if not rows:
                continue
            header = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["td", "th"], recursive=False)]
            if len(header) < 3 or header[0] != "Дата" or "Описание операции" not in header[2]:
                continue
            for row_number, row in enumerate(rows[2:], 3):
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
                if not cells or not DATE.fullmatch(cells[0]):
                    continue
                if len(cells) != 6 or cells[3] != "RUB":
                    raise ValueError("Unsupported source cash row must be diagnosed before semantic audit")
                expected = _kind(cells[2])
                location = (f"table:{table_number}", row_number)
                source_locations.add(location)
                movements = by_row.get(location, [])
                actual = [movement.kind for movement in movements]
                if expected == "dividend":
                    if not actual or actual[0] not in {"dividend", "dividend_net"} or set(actual) - {"dividend", "dividend_net", "tax_withheld"}:
                        raise ValueError(f"Sber dividend classification differs: {item['sha256'][:12]}:{location}")
                elif actual != [expected]:
                    raise ValueError(f"Sber cash classification differs: {item['sha256'][:12]}:{location}: {actual} != {expected}")
                net = _number(cells[4]) - _number(cells[5])
                if sum((movement.amount for movement in movements), Decimal(0)) != net:
                    raise ValueError("Sber cash components do not sum to the source row")
                source_date = datetime.strptime(cells[0], "%d.%m.%Y").date()
                if any(movement.date != source_date for movement in movements):
                    raise ValueError("Sber cash event date differs from the source")
                counts[expected] += 1
                fact = {"sha256": item["sha256"], "sheet": location[0], "row": location[1],
                        "date": source_date.isoformat(), "amount": str(net)}
                if cells[2].startswith("Зачисление д/с (купон "):
                    coupon_credits.append(fact)
                if cells[2].startswith("Списание д/с. Налог на доходы физ.лиц"):
                    tax_debits.append(fact)
        if set(by_row) != source_locations:
            raise ValueError("Sber adapter cash row has no matching source row")
        counts["reports"] += 1
    return {"schema": 1, "counts": dict(sorted(counts.items())),
            "coupon_credits": coupon_credits, "tax_debits": tax_debits}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: audit_sber_cash_meaning.py PRIVATE_REGISTRY PRIVATE_OUTPUT")
    result = audit(Path(sys.argv[1]))
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, prefix=".sber-cash-", suffix=".tmp",
                                     delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Checked {result['counts'].get('reports', 0)} Sber reports; "
          f"{len(result['coupon_credits'])} coupon credits and {len(result['tax_debits'])} tax debits")


if __name__ == "__main__":
    main()
