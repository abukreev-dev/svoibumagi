"""Synthetic control examples only. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import date
from decimal import Decimal as D
import hashlib
import json

import pytest

from scripts.audit_sber_settlement import audit as audit_sber_settlement
from svoibumagi.reconcile import reconcile
from svoibumagi.reports import CashMovement, CashSnapshot, Location, ParsedReport, PositionSnapshot, Trade, UnparsedRow


def test_source_sber_fee_audit_detects_cash_fee_mismatch(tmp_path):
    def table(rows):
        return "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
                                    for row in rows) + "</table>"

    trade = ["01.01.2025", "01.01.2025", "12:00", "Synthetic", "SYN", "RUB", "Покупка", "1",
             "100", "100", "0", "2", "1", "deal-1", "", "И"]
    cash = [
        ["01.01.2025", "Main", "Сделка от 01.01.2025", "RUB", "0", "100"],
        ["01.01.2025", "Main", "Комиссия Брокера от 01.01.2025", "RUB", "0", "2"],
        ["01.01.2025", "Main", "Комиссия Биржи от 01.01.2025", "RUB", "0", "1"],
    ]
    source = tmp_path / "synthetic.html"
    registry = tmp_path / "registry.json"

    def write_source():
        source.write_text(table([["Дата заключения", "Номер сделки"], ["1", "2"], trade]) +
                          table([["Дата", "Описание операции"], ["1", "2"], *cash]))
        registry.write_text(json.dumps({"schema": 2, "reports": [{"broker": "sber", "path": str(source),
                                                                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}))

    write_source()
    assert audit_sber_settlement(registry)["matched_fee_dates"] == 1
    cash[2][5] = "1.01"
    write_source()
    with pytest.raises(ValueError, match="mismatches"):
        audit_sber_settlement(registry)


def test_report_reconciliation_and_unknown_row():
    day = date(2025, 1, 31)
    src = Location("synthetic", 2)
    report = ParsedReport("sber", "synthetic", day, day)
    report.trades.append(Trade(src, "trade-1", day, day, "RU000A000001", "Synthetic", D("2"), D("10"), D("20"), D("0.1"), "RUB"))
    report.positions.append(PositionSnapshot(src, day, "RU000A000001", "Synthetic", D("1"), D("3")))
    report.cash_movements.append(CashMovement(src, day, "trade_cash", D("-20"), "RUB"))
    report.cash_snapshots.append(CashSnapshot(src, day, D("50"), D("30"), "RUB"))
    checks = reconcile(report)
    assert [(c.kind, c.status) for c in checks] == [("position", "matched"), ("cash", "matched")]
    report.unparsed.append(UnparsedRow(Location("synthetic", 3), "unknown corporate action"))
    assert reconcile(report)[-1].status == "unavailable"


def test_sber_settlement_includes_accrued_interest_and_detects_mismatch():
    day = date(2025, 4, 30)
    source = Location("synthetic", 4)
    report = ParsedReport("sber", "sber-html-market-v2", day, day)
    report.trades.append(Trade(source, "deal-1", day, day, "RU000A000001", "Bond",
                               D("2"), D("100"), D("200"), D("0"), "RUB", D("10")))
    report.cash_movements.append(CashMovement(source, day, "trade_cash", D("-210"), "RUB"))
    check = next(item for item in reconcile(report) if item.kind == "settlement")
    assert (check.calculated, check.reported, check.status) == (D("-210"), D("-210"), "matched")
    report.cash_movements[0] = CashMovement(source, day, "trade_cash", D("-200"), "RUB")
    mismatch = next(item for item in reconcile(report) if item.kind == "settlement")
    assert (mismatch.difference, mismatch.status) == (D("-10"), "difference")


def test_sber_trade_fees_reconcile_separately_from_trade_cash():
    day = date(2025, 4, 30)
    source = Location("synthetic", 4)
    report = ParsedReport("sber", "sber-html-market-v2", day, day)
    report.trades.append(Trade(source, "deal-1", day, day, "RU000A000001", "Share",
                               D("1"), D("100"), D("100"), D("3"), "RUB"))
    report.cash_movements.extend([
        CashMovement(source, day, "trade_cash", D("-100"), "RUB"),
        CashMovement(Location("synthetic", 5), day, "trade_fee", D("-3"), "RUB"),
    ])
    assert next(check for check in reconcile(report) if check.kind == "settlement_fee").status == "matched"
    report.cash_movements[1] = CashMovement(Location("synthetic", 5), day, "trade_fee", D("-2"), "RUB")
    fee = next(check for check in reconcile(report) if check.kind == "settlement_fee")
    assert (fee.difference, fee.status) == (D("1"), "difference")


def test_missing_trade_is_not_matched():
    day = date(2025, 1, 31)
    src = Location("synthetic", 2)
    report = ParsedReport("alfa", "synthetic", day, day)
    report.positions.append(PositionSnapshot(src, day, "RU000A000001", "Synthetic", D("1"), D("3")))
    checks = reconcile(report)
    assert checks[0].status == "difference"
    assert checks[1].status == "unavailable"


def test_same_currency_cash_snapshots_reconcile_as_one_balance():
    day = date(2025, 1, 31)
    src = Location("synthetic", 2)
    report = ParsedReport("alfa", "synthetic", day, day)
    report.cash_snapshots.extend([
        CashSnapshot(src, day, D("40"), D("45"), "RUB"),
        CashSnapshot(Location("synthetic", 3), day, D("60"), D("65"), "RUB"),
    ])
    report.cash_movements.append(CashMovement(Location("synthetic", 4), day, "deposit", D("10"), "RUB"))
    cash_checks = [check for check in reconcile(report) if check.kind == "cash"]
    assert len(cash_checks) == 1
    assert (cash_checks[0].calculated, cash_checks[0].reported, cash_checks[0].status) == (D("110"), D("110"), "matched")


def test_unsettled_trade_does_not_change_reported_position():
    day = date(2025, 1, 31)
    src = Location("synthetic", 2)
    report = ParsedReport("sber", "synthetic", day, day)
    report.trades.append(Trade(src, "trade-2", day, date(2025, 2, 3), "RU000A000001", "Synthetic", D("2"), D("10"), D("20"), D("0"), "RUB"))
    report.positions.append(PositionSnapshot(src, day, "RU000A000001", "Synthetic", D("1"), D("1")))
    assert reconcile(report)[0].status == "matched"


def test_traded_instrument_missing_from_snapshot_is_unavailable():
    day = date(2025, 1, 31)
    src = Location("synthetic", 2)
    report = ParsedReport("sber", "synthetic", day, day)
    report.trades.append(Trade(src, "trade-3", day, day, "RU000A000001", "Synthetic", D("-2"), D("10"), D("20"), D("0"), "RUB"))
    checks = reconcile(report)
    assert any(check.kind == "position" and check.status == "unavailable" for check in checks)
