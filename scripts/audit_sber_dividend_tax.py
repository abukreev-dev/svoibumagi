"""Read Sber dividend and withholding facts from private source HTML.

The JSON result contains financial data and must remain outside Git.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

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


GROSS = re.compile(r"Дополнительная информация:\s*Дивиденды\s+([\d\s\u00a0]+(?:[.,]\d{1,2})?)\s+RUR\b", re.I)
QUANTITY = re.compile(r"Кол-во\s+([\d.,]+)\s*;", re.I)
RATE = re.compile(r"Ставка Выплаты\s+([\d.,]+)\s*;", re.I)
CONVERSION = re.compile(r"Курс конвертации\s+([\d.,]+)\s*;", re.I)


def _number(value: str) -> Decimal:
    number = Decimal(value.replace("\u00a0", "").replace(" ", "").replace(",", "."))
    if not number.is_finite() or number < 0:
        raise ValueError("Invalid source dividend value")
    return number


def _one(pattern: re.Pattern, text: str) -> Decimal | None:
    matches = pattern.findall(text)
    return _number(matches[0]) if len(matches) == 1 else None


def audit(registry_path: Path) -> dict:
    registry = json.loads(registry_path.read_text())
    sources = []
    for item in registry["reports"]:
        if item["broker"] != "sber":
            continue
        path = Path(item["path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered Sber source changed")
        tables = BeautifulSoup(path.read_bytes(), "html.parser").find_all("table")
        facts = []
        for table_number, table in enumerate(tables):
            for row_number, row in enumerate(table.find_all("tr"), 1):
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
                if len(cells) != 6 or "Налог удержан" not in cells[2] or not cells[2].startswith(("Дивиденды", "Выплата дивидендов")):
                    continue
                if cells[3] != "RUB":
                    raise ValueError("Withheld dividend is not RUB")
                net = _number(cells[4]) - _number(cells[5])
                gross = _one(GROSS, cells[2])
                fact = {"sheet": f"table:{table_number}", "row": row_number, "net": str(net)}
                if gross is not None:
                    quantity, rate, conversion = (_one(pattern, cells[2]) for pattern in (QUANTITY, RATE, CONVERSION))
                    if None in (quantity, rate, conversion) or quantity * rate * conversion != gross or gross < net:
                        raise ValueError(f"Source gross dividend arithmetic differs: {item['sha256'][:12]}:{row_number}")
                    fact.update({"gross": str(gross), "tax": str(gross - net), "quantity": str(quantity),
                                 "rate": str(rate), "conversion": str(conversion)})
                else:
                    fact["status"] = "withholding_unavailable"
                facts.append(fact)
        if not facts:
            continue
        parsed = parse_sber(path)
        by_location = {}
        for movement in parsed.cash_movements:
            by_location.setdefault((movement.location.sheet, movement.location.row), []).append(movement)
        for fact in facts:
            rows = by_location.get((fact["sheet"], fact["row"]), [])
            if fact.get("status") == "withholding_unavailable":
                if len(rows) != 1 or rows[0].kind != "dividend_net" or rows[0].amount != Decimal(fact["net"]):
                    raise ValueError("Unresolved Sber dividend was not kept as net-only")
                if not any(row.location.sheet == fact["sheet"] and row.location.row == fact["row"]
                           and "withholding amount not separately verified" in row.reason for row in parsed.unparsed):
                    raise ValueError("Missing Sber withholding diagnostic")
            else:
                dividend = [row for row in rows if row.kind == "dividend"]
                tax = [row for row in rows if row.kind == "tax_withheld" and row.component == "dividend_tax"]
                expected_tax = Decimal(fact["tax"])
                if (len(dividend) != 1 or dividend[0].amount != Decimal(fact["gross"])
                        or dividend[0].withheld_tax != expected_tax or len(tax) != (1 if expected_tax else 0)
                        or tax and tax[0].amount != -expected_tax):
                    raise ValueError("Parsed Sber dividend or tax differs from source")
        sources.append({"sha256": item["sha256"], "period": [item["period_start"], item["period_end"]],
                        "dividends": facts})
    return {"schema": 1, "sources": sources,
            "gross_known": sum("gross" in row for source in sources for row in source["dividends"]),
            "withholding_unavailable": sum("status" in row for source in sources for row in source["dividends"])}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: audit_sber_dividend_tax.py PRIVATE_REGISTRY PRIVATE_OUTPUT")
    result = audit(Path(sys.argv[1]))
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, prefix=".sber-dividend-tax-", suffix=".tmp",
                                     delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
