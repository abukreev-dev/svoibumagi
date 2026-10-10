"""Report-level reconciliation. SPDX-License-Identifier: GPL-3.0-or-later"""

from dataclasses import dataclass
from decimal import Decimal

from .reports import ParsedReport


@dataclass(frozen=True)
class Check:
    kind: str
    key: str
    calculated: Decimal | None
    reported: Decimal | None
    difference: Decimal | None
    status: str  # matched, difference, unavailable
    reason: str = ""


def reconcile(report: ParsedReport, *, quantity_tolerance: Decimal = Decimal("0"),
              cash_tolerance: Decimal = Decimal("0.01")) -> list[Check]:
    checks: list[Check] = []
    initial: dict[str, Decimal] = {}
    final: dict[str, Decimal] = {}
    for snapshot in report.positions:
        if not snapshot.isin:
            checks.append(Check("position", snapshot.name, None, snapshot.end_quantity, None, "unavailable", "missing ISIN"))
            continue
        initial[snapshot.isin] = initial.get(snapshot.isin, Decimal(0)) + snapshot.start_quantity
        final[snapshot.isin] = final.get(snapshot.isin, Decimal(0)) + snapshot.end_quantity
    for trade in report.trades + report.summary_trades:
        if trade.settled_at <= report.period_end:
            initial[trade.isin] = initial.get(trade.isin, Decimal(0)) + trade.quantity
    if not report.positions:
        checks.append(Check("position", "all", None, None, None, "unavailable", "report has no position snapshot"))
    for isin in sorted(set(initial) | set(final)):
        if isin not in final:
            checks.append(Check("position", isin, None, None, None, "unavailable", "instrument absent from report position snapshot"))
            continue
        calculated = initial.get(isin, Decimal(0))
        reported = final.get(isin, Decimal(0))
        diff = calculated - reported
        checks.append(Check("position", isin, calculated, reported, diff,
                            "matched" if abs(diff) <= quantity_tolerance else "difference"))

    if not report.cash_snapshots:
        checks.append(Check("cash", "RUB", None, None, None, "unavailable", "report has no cash snapshot"))
    cash_totals: dict[str, tuple[Decimal, Decimal]] = {}
    for snapshot in report.cash_snapshots:
        opening, closing = cash_totals.get(snapshot.currency, (Decimal(0), Decimal(0)))
        cash_totals[snapshot.currency] = (opening + snapshot.start_amount, closing + snapshot.end_amount)
    for currency, (opening, closing) in sorted(cash_totals.items()):
        movements = sum((m.amount for m in report.cash_movements if m.currency == currency), Decimal(0))
        calculated = opening + movements
        diff = calculated - closing
        checks.append(Check("cash", currency, calculated, closing, diff,
                            "matched" if abs(diff) <= cash_tolerance else "difference"))

    if report.variant.startswith("sber-html-market-"):
        settlement: dict[str, Decimal] = {}
        reported_cash: dict[str, Decimal] = {}
        trade_fees: dict[str, Decimal] = {}
        cash_fees: dict[str, Decimal] = {}
        for trade in report.trades + report.summary_trades:
            if report.period_start <= trade.settled_at <= report.period_end:
                key = trade.settled_at.isoformat()
                sign = -1 if trade.quantity > 0 else 1
                settlement[key] = settlement.get(key, Decimal(0)) + sign * (trade.amount + (trade.accrued_interest or Decimal(0)))
                trade_fees[key] = trade_fees.get(key, Decimal(0)) + trade.commission
        for movement in report.cash_movements:
            if movement.kind == "trade_cash":
                key = movement.date.isoformat()
                reported_cash[key] = reported_cash.get(key, Decimal(0)) + movement.amount
            elif movement.kind == "trade_fee":
                key = movement.date.isoformat()
                cash_fees[key] = cash_fees.get(key, Decimal(0)) - movement.amount
        for key in sorted(set(settlement) | set(reported_cash)):
            calculated = settlement.get(key, Decimal(0))
            reported = reported_cash.get(key, Decimal(0))
            difference = calculated - reported
            checks.append(Check("settlement", key, calculated, reported, difference,
                                "matched" if abs(difference) <= cash_tolerance else "difference"))
        for key in sorted(set(trade_fees) | set(cash_fees)):
            calculated = trade_fees.get(key, Decimal(0))
            reported = cash_fees.get(key, Decimal(0))
            if calculated == reported == 0:
                continue
            difference = calculated - reported
            checks.append(Check("settlement_fee", key, calculated, reported, difference,
                                "matched" if abs(difference) <= cash_tolerance else "difference"))

    for subtotal in report.cash_flow_subtotals:
        calculated = sum((m.amount for m in report.cash_movements
                          if m.currency == subtotal.currency and
                          (m.kind == subtotal.kind or
                           (subtotal.kind == "bank_fee" and m.kind in {"fee", "trade_fee"}) or
                           (subtotal.kind == "dividend" and m.kind == "tax_withheld"
                            and m.component == "dividend_tax"))), Decimal(0))
        diff = calculated - subtotal.amount
        checks.append(Check("cash_flow", subtotal.kind, calculated, subtotal.amount, diff,
                            "matched" if abs(diff) <= cash_tolerance else "difference"))

    for total in report.source_totals:
        diff = total.calculated - total.reported
        checks.append(Check("source_total", total.key, total.calculated, total.reported, diff,
                            "matched" if abs(diff) <= cash_tolerance else "difference"))

    if report.unparsed:
        # Even matching arithmetic is not proof of complete interpretation.
        checks.append(Check("completeness", "unparsed", None, None, None, "unavailable",
                            f"{len(report.unparsed)} source rows need review"))
    return checks
