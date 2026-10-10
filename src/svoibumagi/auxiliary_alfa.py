"""Independent controls from an Alfa portfolio export. SPDX-License-Identifier: GPL-3.0-or-later"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

import xlrd

from .alfa import parse_alfa
from .calculations import IncompleteHistory, fifo
from .reports import account_key


ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{10}")


def _number(value: object) -> Decimal:
    return Decimal(str(value))


def _single(sheet, label: str, *, label_col: int, value_col: int) -> Decimal:
    values = [sheet.cell_value(row, value_col) for row in range(sheet.nrows)
              if str(sheet.cell_value(row, label_col)).strip() == label]
    if len(values) != 1:
        raise ValueError(f"Auxiliary export must contain one {label!r} value")
    return _number(values[0])


def check(export_path: Path, report_path: Path) -> dict[str, int]:
    book = xlrd.open_workbook(export_path, logfile=None)
    required = {"Структура", "Фин.рез.", "Движение ДС"}
    if not required <= set(book.sheet_names()):
        raise ValueError("Not a supported Alfa portfolio export")
    structure = book.sheet_by_name("Структура")
    results = book.sheet_by_name("Фин.рез.")
    cash = book.sheet_by_name("Движение ДС")
    report = parse_alfa(report_path)
    if account_key("alfa", str(structure.cell_value(2, 21)).strip()) != report.account_key:
        raise ValueError("Portfolio export belongs to another contract")
    period = re.fullmatch(r"(\d{2}\.\d{2}\.\d{4}) - (\d{2}\.\d{2}\.\d{4})",
                          str(structure.cell_value(0, 5)).strip())
    if period is None or tuple(datetime.strptime(value, "%d.%m.%Y").date() for value in period.groups()) != (
            report.period_start, report.period_end):
        raise ValueError("Portfolio export period differs from broker report")
    if len(report.cash_snapshots) != 1:
        raise ValueError("Broker report needs one RUB cash snapshot")
    cash_values = [cash.cell_value(i, 8) for i in range(cash.nrows)]
    labels = [str(cash.cell_value(i, 2)).strip() for i in range(cash.nrows)]
    expected_labels = [
        "Остаток денежных средств на начало периода",
        "Списания и зачисления по поручению клиентов",
        "Зачислено денежных средств", "Списано денежных средств",
        "Списания и зачисления в результате расчетов по сделкам",
        "Зачислено денежных средств", "Списано денежных средств",
        "Купоны и дивиденды", "Получено дивидендов", "Получено купонов",
        "Прочие расходы", "Уплачено налогов", "Удержано комиссий",
        "Остаток денежных средств на конец периода",
    ]
    hits = [(label, _number(cash_values[i])) for i, label in enumerate(labels) if label in expected_labels
            and cash_values[i] not in ("", None)]
    if [label for label, _ in hits] != [label for label in expected_labels if label not in {
            "Списания и зачисления по поручению клиентов",
            "Списания и зачисления в результате расчетов по сделкам", "Купоны и дивиденды", "Прочие расходы"}]:
        raise ValueError("Alfa portfolio cash control layout changed")
    values = [value for _, value in hits]
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for movement in report.cash_movements:
        sums[movement.kind] += movement.amount
        if movement.kind in {"trade_cash", "accrued_interest"}:
            sums["trade_cash_in" if movement.amount > 0 else "trade_cash_out"] += movement.amount
    expected = [report.cash_snapshots[0].start_amount, sums["deposit"], sums["withdrawal"],
                sums["trade_cash_in"], sums["trade_cash_out"], sums["dividend"] + sums["dividend_net"],
                sums["coupon"], sums["tax_withheld"], sums["trade_fee"] + sums["fee"],
                report.cash_snapshots[0].end_amount]
    if values != expected:
        raise ValueError("Portfolio cash categories differ from broker report")
    positions: dict[str, Decimal] = {}
    broker_gross_results: dict[str, Decimal] = {}
    for row in range(1, results.nrows):
        isin = str(results.cell_value(row, 5)).strip()
        if not ISIN.fullmatch(isin):
            continue
        quantity = results.cell_value(row - 1, 17)
        if quantity not in ("", None, 0, 0.0):
            positions[isin] = _number(quantity)
        gross_result = results.cell_value(row - 1, 11)
        if gross_result not in ("", None):
            broker_gross_results[isin] = _number(gross_result)
    report_positions = {item.isin: item.end_quantity for item in report.positions
                        if item.isin and item.end_quantity != 0}
    if positions != report_positions:
        raise ValueError("Portfolio open positions differ from broker report")
    by_isin: dict[str, list] = defaultdict(list)
    for trade in report.trades:
        by_isin[trade.isin].append(trade)
    gross_fifo_controls = 0
    fifo_blocked = 0
    for isin, trades in by_isin.items():
        if not any(trade.quantity < 0 for trade in trades):
            continue
        if isin not in broker_gross_results:
            raise ValueError("Sold instrument has no portfolio gross result")
        try:
            _, sales = fifo([replace(trade, commission=Decimal(0)) for trade in trades])
        except IncompleteHistory:
            fifo_blocked += 1
            continue
        gross = sum((sale.result for sale in sales), Decimal(0))
        if abs(gross - broker_gross_results[isin]) > Decimal("0.01"):
            raise ValueError("Gross FIFO result differs from portfolio export")
        gross_fifo_controls += 1
    cash_value = _single(structure, "Валюта", label_col=2, value_col=3)
    securities_value = _single(structure, "Акции", label_col=2, value_col=3)
    total_value = _single(structure, "Итого", label_col=2, value_col=3)
    if cash_value != report.cash_snapshots[0].end_amount or cash_value + securities_value != total_value:
        raise ValueError("Portfolio structure values do not add up")
    return {"cash_controls": len(values), "open_positions": len(positions), "structure_controls": 2,
            "gross_fifo_controls": gross_fifo_controls, "fifo_blocked": fifo_blocked}


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Alfa portfolio export check")
    parser.add_argument("export", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    result = check(args.export, args.report)
    print("Matched {cash_controls} cash controls, {open_positions} open positions, "
          "{structure_controls} portfolio structure controls and {gross_fifo_controls} gross FIFO results; "
          "{fifo_blocked} instrument results remain blocked".format(**result))


if __name__ == "__main__":
    main()
