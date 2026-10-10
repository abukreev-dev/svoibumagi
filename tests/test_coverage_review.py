"""Gap boundary controls. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
from pathlib import Path

from openpyxl import Workbook
import pytest

from svoibumagi.reports import account_key
from svoibumagi.reports import CashMovement, CashSnapshot, Location, ParsedReport, PositionSnapshot, Trade, UnparsedRow


spec = importlib.util.spec_from_file_location("coverage_review", Path(__file__).resolve().parents[1] / "scripts/build_coverage_review.py")
assert spec and spec.loader
coverage_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage_review)
_boundary_comparison = coverage_review._boundary_comparison
_gap_dated_rows = coverage_review._gap_dated_rows
_sber_pending_boundary = coverage_review._sber_pending_boundary
_source_account_number = coverage_review._source_account_number


def test_private_coverage_labels_use_verified_source_numbers_not_file_names(tmp_path):
    sber = tmp_path / "misleading-name.html"
    sber.write_text("<html>Договор на ведение индивидуального инвестиционного счета SYN123 от 01.01.2025</html>")
    item = {"path": str(sber), "sha256": hashlib.sha256(sber.read_bytes()).hexdigest(),
            "broker": "sber", "account_kind": "iis", "account_key": account_key("sber", "SYN123")}
    assert _source_account_number(item) == "SYN123"
    with pytest.raises(ValueError, match="kind differs"):
        _source_account_number({**item, "account_kind": "brokerage"})
    with pytest.raises(ValueError, match="identity"):
        _source_account_number({**item, "account_key": account_key("sber", "OTHER456")})

    alfa = tmp_path / "misleading-name.xlsx"
    workbook = Workbook()
    workbook.active.title = " Движение ДС"
    workbook.active.append(["№ 1234567 от 01.01.2025"])
    workbook.save(alfa)
    agreement = "№ 1234567 от 01.01.2025"
    item = {"path": str(alfa), "sha256": hashlib.sha256(alfa.read_bytes()).hexdigest(),
            "broker": "alfa", "account_kind": "unknown", "account_key": account_key("alfa", agreement)}
    assert _source_account_number(item) == "1234567"
    with pytest.raises(ValueError, match="source changed"):
        _source_account_number({**item, "sha256": "0" * 64})


def _report(positions: dict[str, str], cash: str) -> ParsedReport:
    report = ParsedReport("sber", "synthetic", date(2025, 1, 1), date(2025, 1, 1))
    loc = Location("synthetic", 1)
    report.positions = [PositionSnapshot(loc, report.period_end, isin, isin, Decimal(qty), Decimal(qty))
                        for isin, qty in positions.items()]
    report.cash_snapshots = [CashSnapshot(loc, report.period_end, Decimal(cash), Decimal(cash), "RUB")]
    return report


def test_gap_boundary_ignores_zero_rows_but_detects_real_changes():
    before = {"report": _report({"RU000A000001": "7"}, "100")}
    zero_only = {"report": _report({"RU000A000001": "7", "RU000A000002": "0"}, "100")}
    changed = {"report": _report({"RU000A000001": "8"}, "110")}
    parse = lambda item: item["report"]

    assert _boundary_comparison([before], [zero_only], parse) == ("совпали", "совпали")
    assert _boundary_comparison([before], [changed], parse) == ("отличаются", "отличаются")
    assert _boundary_comparison([before, changed], [zero_only], parse) == ("недоступно", "недоступно")


def test_gap_review_distinguishes_dated_rows_from_missing_history():
    report = _report({}, "0")
    loc = Location("synthetic", 2)
    report.trades = [Trade(loc, "deal-1", date(2025, 1, 31), date(2025, 2, 2),
                           "RU000A000001", "Synthetic", Decimal("1"), Decimal("10"),
                           Decimal("10"), Decimal("0"), "RUB")]
    report.cash_movements = [CashMovement(loc, date(2025, 2, 2), "trade_cash", Decimal("-10"), "RUB")]
    parse = lambda item: item["report"]
    assert _gap_dated_rows([{"report": report}], date(2025, 2, 1), date(2025, 2, 3), parse) == \
        "сделки: есть; деньги: есть"
    assert _gap_dated_rows([{"report": report}], date(2025, 2, 3), date(2025, 2, 28), parse) == \
        "сделки: нет; деньги: нет"


def test_sber_planned_settlement_explains_both_boundary_balances_without_proving_execution(tmp_path):
    trade_cells = ["07.08.2025", "09.08.2025", "", "Synthetic", "RU000A000001", "RUB",
                   "Покупка", "2", "100", "200", "2", "1", "0,5", "deal-1", "", "О"]
    html = ("<table><tr><th>Дата заключения</th></tr><tr></tr><tr>" +
            "".join(f"<td>{value}</td>" for value in trade_cells) + "</tr></table>").encode()
    path = tmp_path / "synthetic.html"
    path.write_bytes(html)
    before = _report({"RU000A000001": "1"}, "1000")
    before.excluded = [UnparsedRow(Location("table:0", 3), "not executed at report end: О")]
    after = _report({"RU000A000001": "3"}, "796.5")
    left = {"path": str(path), "sha256": hashlib.sha256(html).hexdigest(), "report": before}
    right = {"report": after}
    parse = lambda item: item["report"]
    assert _sber_pending_boundary([left], [right], date(2025, 8, 8), date(2025, 8, 9), parse) == \
        (1, "позиции и деньги")
    assert _sber_pending_boundary([left], [right], date(2025, 8, 10), date(2025, 8, 11), parse) == \
        (0, "нет кандидатов")
