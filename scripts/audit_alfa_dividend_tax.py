"""Read dividend tax facts directly from private Alfa source cells.

The output is a Git-ignored private control, never a public test fixture.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


TAX = re.compile(r"Удержан налог\s+([\d\s\u00a0]+(?:[.,]\d{1,2})?)\s*руб\.", re.I)


def _number(value: object) -> Decimal:
    try:
        result = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Invalid dividend or tax number in source") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid dividend or tax number in source")
    return result


def _rows(path: Path) -> list[list[object]]:
    if path.suffix.lower() == ".xls":
        import xlrd

        book = xlrd.open_workbook(path, logfile=None)
        sheet = book.sheet_by_name(" Движение ДС")
        return [sheet.row_values(index) for index in range(sheet.nrows)]
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    return [list(row) for row in book[" Движение ДС"].values]


def _cell(row: list[object], index: int) -> object:
    return row[index] if index < len(row) and row[index] is not None else ""


def audit(registry_path: Path) -> dict:
    registry = json.loads(registry_path.read_text())
    from svoibumagi.alfa import parse_alfa

    sources = []
    unique_payments = set()
    for item in registry["reports"]:
        if item["broker"] != "alfa":
            continue
        path = Path(item["path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered Alfa source changed")
        rows = _rows(path)
        dividends = []
        subtotals = []
        for row_number, row in enumerate(rows, 1):
            operation = str(_cell(row, 9)).strip()
            comment = str(_cell(row, 10)).strip()
            label = str(_cell(row, 11)).strip()
            if operation == "Перевод" and comment.casefold().startswith("дивиденды "):
                net = _number(_cell(row, 14))
                matches = TAX.findall(comment)
                if len(matches) != 1:
                    raise ValueError(f"Dividend withholding is absent or ambiguous: {item['sha256'][:12]}:{row_number}")
                tax = _number(matches[0])
                dividends.append({"sheet": " Движение ДС", "row": row_number, "net": str(net),
                                  "tax": str(tax), "gross": str(net + tax)})
                unique_payments.add((item["account_key"], str(_cell(row, 2)), str(net), str(tax), comment))
            if label == "Дивиденды":
                subtotals.append({"sheet": " Движение ДС", "row": row_number,
                                  "net": str(_number(_cell(row, 14)))})
        if dividends or subtotals:
            net_sum = sum((Decimal(row["net"]) for row in dividends), Decimal(0))
            if len(subtotals) != 1 or Decimal(subtotals[0]["net"]) != net_sum:
                raise ValueError(f"Dividend subtotal does not match source rows: {item['sha256'][:12]}")
            parsed = parse_alfa(path)
            movements = {row.location.row: row for row in parsed.cash_movements if row.kind == "dividend"}
            if len(movements) != len(dividends):
                raise ValueError(f"Adapter dividend count differs from source: {item['sha256'][:12]}")
            for fact in dividends:
                movement = movements.get(fact["row"])
                taxes = [row for row in parsed.cash_movements if row.location.row == fact["row"]
                         and row.kind == "tax_withheld" and row.component == "dividend_tax"]
                if (movement is None or movement.amount != Decimal(fact["gross"])
                        or movement.withheld_tax != Decimal(fact["tax"])):
                    raise ValueError(f"Adapter dividend tax differs from source: {item['sha256'][:12]}:{fact['row']}")
                if (len(taxes) != (1 if Decimal(fact["tax"]) > 0 else 0)
                        or taxes and taxes[0].amount != -Decimal(fact["tax"])):
                    raise ValueError(f"Adapter tax event differs from source: {item['sha256'][:12]}:{fact['row']}")
            sources.append({"sha256": item["sha256"], "period": [item["period_start"], item["period_end"]],
                            "dividends": dividends, "subtotal": subtotals[0]})
    return {"schema": 1, "sources": sources, "source_rows": sum(len(item["dividends"]) for item in sources),
            "unique_payments": len(unique_payments)}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: audit_alfa_dividend_tax.py PRIVATE_REGISTRY PRIVATE_OUTPUT")
    result = audit(Path(sys.argv[1]))
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, prefix=".dividend-tax-", suffix=".tmp",
                                     delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Checked {len(result['sources'])} Alfa source files, {result['source_rows']} dividend rows "
          f"and {result['unique_payments']} unique payments with explicit tax")


if __name__ == "__main__":
    main()
