"""Check private, independently calculated FIFO examples from broker source rows.

Real source paths and control values must remain in Git-ignored private files.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from datetime import time
from decimal import Decimal
import json
from pathlib import Path
import sys

from svoibumagi.calculations import fifo
from svoibumagi.alfa import _sheet_rows, parse_alfa
from svoibumagi.sber import parse_sber

if __package__:
    from .check_manual_controls import check as check_manual_controls
else:
    from check_manual_controls import check as check_manual_controls


CENT = Decimal("0.01")


def _sum(values) -> Decimal:
    return sum(values, Decimal(0))


def check(registry_path: Path, manual_path: Path, fifo_path: Path) -> int:
    # The manual control checks the file SHA-256 and every transcribed trade first.
    check_manual_controls(registry_path, manual_path)
    registry = json.loads(registry_path.read_text())
    manual = json.loads(manual_path.read_text())
    controls = json.loads(fifo_path.read_text())
    if controls.get("schema") != 1:
        raise ValueError("Unsupported FIFO control schema")
    reports = {item["sha256"]: item for item in registry["reports"]}
    manual_cases = {item["sha256"]: item for item in manual["cases"]}
    for case in controls["cases"]:
        sha = case["sha256"]
        broker = reports[sha]["broker"]
        if broker not in {"sber", "alfa"}:
            raise ValueError("Unsupported FIFO control broker")
        source = manual_cases[sha]
        isin = case["isin"]
        if Decimal(source["positions"][isin][0]) != 0:
            raise ValueError("Opening position is not zero")
        rows = {item["location"]: item for item in source["trades"] if item["isin"] == isin}
        first_buy = rows[case["first_buy_row"]]
        sales = [rows[row] for row in case["sale_rows"]]
        later_buys = [rows[row] for row in case["later_buy_rows"]]
        if set(rows) != {case["first_buy_row"], *case["sale_rows"], *case["later_buy_rows"]}:
            raise ValueError("FIFO control omits source trades for this instrument")
        if Decimal(first_buy["quantity"]) <= 0 or any(Decimal(row["quantity"]) >= 0 for row in sales):
            raise ValueError("Invalid source buy/sale signs")
        if any(Decimal(row["quantity"]) <= 0 for row in later_buys):
            raise ValueError("Invalid source later-buy signs")
        sold_quantity = -_sum(Decimal(row["quantity"]) for row in sales)
        first_quantity = Decimal(first_buy["quantity"])
        first_cost = Decimal(first_buy["amount"]) + Decimal(first_buy["commission"])
        case_type = case.get("case_type", "closed_first_lot")
        if case_type == "closed_first_lot":
            if first_quantity != sold_quantity:
                raise ValueError("First source lot is not closed by selected sales")
            source_cost = first_cost
        elif case_type == "partial_first_lot":
            if len(sales) != 1 or not 0 < sold_quantity < first_quantity:
                raise ValueError("Source sale does not partially close the first lot")
            if any(row["settled_at"] >= sales[0]["settled_at"]
                   for row in [first_buy, *later_buys]):
                raise ValueError("Source lot order is not established")
            if broker == "alfa":
                source_times = case.get("source_times", {})
                if set(source_times) != set(rows):
                    raise ValueError("Alfa FIFO control must include all source execution times")
                sheets, _ = _sheet_rows(Path(reports[sha]["path"]))
                for location, expected_time in source_times.items():
                    sheet, row_number = location.rsplit(":", 1)
                    raw_time = str(sheets[sheet][int(row_number) - 1][7]).split()[-1]
                    if time.fromisoformat(raw_time.zfill(8)) != time.fromisoformat(expected_time):
                        raise ValueError("Source FIFO execution time changed")
                first_time = time.fromisoformat(source_times[case["first_buy_row"]])
                if any(row["traded_at"] == first_buy["traded_at"]
                       and first_time >= time.fromisoformat(source_times[row["location"]])
                       for row in later_buys):
                    raise ValueError("First source lot is not first on the trade date")
            source_cost = first_cost * sold_quantity / first_quantity
        else:
            raise ValueError("Unsupported FIFO control case")
        source_proceeds = _sum(Decimal(row["amount"]) - Decimal(row["commission"]) for row in sales)
        source_ending_quantity = first_quantity - sold_quantity + _sum(Decimal(row["quantity"]) for row in later_buys)
        source_ending_cost = first_cost - source_cost + _sum(
            Decimal(row["amount"]) + Decimal(row["commission"]) for row in later_buys)
        expected = case["expected"]
        source_values = {
            "sold_quantity": sold_quantity,
            "cost": source_cost,
            "proceeds": source_proceeds,
            "result": source_proceeds - source_cost,
            "ending_quantity": source_ending_quantity,
            "ending_cost": source_ending_cost,
        }
        for key, actual in source_values.items():
            if actual != Decimal(expected[key]):
                raise ValueError(f"Source FIFO control differs: {key}")
        report = (parse_sber if broker == "sber" else parse_alfa)(Path(reports[sha]["path"]))
        lots, realized = fifo([trade for trade in report.trades if trade.isin == isin])
        actual_values = {
            "sold_quantity": _sum(item.quantity for item in realized),
            "cost": _sum(item.cost for item in realized),
            "proceeds": _sum(item.proceeds for item in realized),
            "result": _sum(item.result for item in realized),
            "ending_quantity": _sum(item.quantity for item in lots[isin]),
            "ending_cost": _sum(item.cost for item in lots[isin]),
        }
        if len(realized) != len(sales):
            raise ValueError("Unexpected number of FIFO sales")
        for key, actual in actual_values.items():
            if actual.quantize(CENT) != Decimal(expected[key]).quantize(CENT):
                raise ValueError(f"Calculated FIFO control differs: {key}")
        if source_ending_quantity != Decimal(source["positions"][isin][1]):
            raise ValueError("Ending source quantity differs from broker position")
    return len(controls["cases"])


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: check_fifo_source_controls.py REGISTRY MANUAL_CONTROLS FIFO_CONTROLS")
    count = check(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"Checked {count} private FIFO source control cases")
