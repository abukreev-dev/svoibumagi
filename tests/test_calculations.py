"""Independent hand-calculated controls. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import date
from decimal import Decimal as D
from fractions import Fraction
from itertools import combinations
from math import lcm

import pytest

from svoibumagi.calculations import IncompleteHistory, Lot, PrincipalAction, SecurityTransfer, amortize_lots, fifo, fifo_with_principal, replay_portfolio_lots, transfer_lots, xirr
from svoibumagi.reports import Location, Trade


def _trade(ident, day, quantity, amount, fee):
    return Trade(Location("synthetic", int(ident[-1])), ident, day, day, "RU000A000001", "Synthetic", D(quantity), D("100"), D(amount), D(fee), "RUB")


def test_fifo_partial_sale_and_fee():
    day = date(2025, 1, 1)
    lots, sales = fifo([_trade("trade-1", day, "10", "1000", "10"), _trade("trade-2", date(2025, 2, 1), "-4", "600", "4")])
    assert sales[0].cost == D("404")
    assert sales[0].proceeds == D("596")
    assert sales[0].result == D("192")
    assert lots["RU000A000001"][0].quantity == D("6")
    assert lots["RU000A000001"][0].cost == D("606")


def test_fifo_rejects_missing_opening_history():
    with pytest.raises(IncompleteHistory):
        fifo([_trade("trade-1", date(2025, 1, 1), "-1", "100", "0")])


def test_financial_replay_rejects_nonfinite_inputs_before_changing_lots():
    day = date(2025, 1, 1)
    with pytest.raises(ValueError, match="non-finite"):
        fifo([_trade("trade-1", day, "1", "Infinity", "0")])
    with pytest.raises(ValueError, match="non-finite"):
        fifo([_trade("trade-1", day, "1", "100", "NaN")])
    original = {"RU000A000001": [Lot(D("2"), D("180"), day)]}
    with pytest.raises(ValueError, match="Invalid amortization"):
        amortize_lots(original, "RU000A000001", D("0.5"), D("Infinity"))
    assert original["RU000A000001"] == [Lot(D("2"), D("180"), day)]
    with pytest.raises(IncompleteHistory, match="invalid confirmed lots"):
        transfer_lots({"RU000A000001": [Lot(D("2"), D("Infinity"), day)]}, original,
                      "RU000A000001", D("1"))
    assert original["RU000A000001"] == [Lot(D("2"), D("180"), day)]


def test_fifo_does_not_move_later_same_day_buy_before_sale():
    day = date(2025, 1, 1)
    sale = _trade("trade-1", day, "-1", "100", "0")
    buy = _trade("trade-2", day, "1", "90", "0")
    with pytest.raises(IncompleteHistory, match="same-day later purchase"):
        fifo([buy, sale])


def test_alfa_fifo_uses_execution_time_when_report_rows_are_out_of_order():
    day = date(2025, 1, 1)
    later_buy = Trade(Location("alfa", 1), "buy", day, day, "RU000A000001", "Synthetic",
                      D("1"), D("90"), D("90"), D("0"), "RUB",
                      execution_time="12:54:39", execution_time_required=True)
    earlier_sale = Trade(Location("alfa", 2), "sell", day, day, "RU000A000001", "Synthetic",
                         D("-1"), D("100"), D("100"), D("0"), "RUB",
                         execution_time="07:44:39", execution_time_required=True)
    with pytest.raises(IncompleteHistory, match="same-day later purchase"):
        fifo([later_buy, earlier_sale])

    earlier_buy = Trade(Location("alfa", 2), "buy", day, day, "RU000A000001", "Synthetic",
                        D("1"), D("90"), D("90"), D("0"), "RUB",
                        execution_time="07:44:39", execution_time_required=True)
    later_sale = Trade(Location("alfa", 1), "sell", day, day, "RU000A000001", "Synthetic",
                       D("-1"), D("100"), D("100"), D("0"), "RUB",
                       execution_time="12:54:39", execution_time_required=True)
    _, sales = fifo([later_sale, earlier_buy])
    assert sales[0].cost == D("90") and sales[0].result == D("10")
    with pytest.raises(IncompleteHistory, match="execution time is unavailable"):
        fifo([Trade(Location("alfa", 3), "missing", day, day, "RU000A000001", "Synthetic",
                    D("1"), D("90"), D("90"), D("0"), "RUB", execution_time_required=True)])


def test_security_transfer_carries_cost_and_original_fifo_order():
    isin = "RU000A000001"
    source, _ = fifo([_trade("trade-1", date(2025, 1, 1), "10", "1000", "10")])
    destination = {isin: [Lot(D("2"), D("400"), date(2025, 2, 1))]}
    moved_cost = transfer_lots(source, destination, isin, D("4"))
    assert moved_cost == D("404")
    assert source[isin][0] == Lot(D("6"), D("606"), date(2025, 1, 1))
    assert destination[isin] == [Lot(D("4"), D("404"), date(2025, 1, 1)),
                                 Lot(D("2"), D("400"), date(2025, 2, 1))]
    remaining, sales = fifo([_trade("trade-2", date(2025, 4, 1), "-5", "700", "0")], destination)
    assert sales[0].cost == D("604")
    assert sales[0].result == D("96")
    assert remaining[isin][0].cost == D("200")
    with pytest.raises(IncompleteHistory, match="exceeds"):
        transfer_lots(source, destination, isin, D("7"))
    assert source[isin][0].quantity == D("6")


def test_cross_account_replay_preserves_reduced_cost_and_rejects_unproven_order():
    isin = "RU000A000001"
    bought = _trade("trade-1", date(2025, 1, 1), "10", "1000", "10")
    sold = _trade("trade-2", date(2025, 4, 1), "-4", "600", "4")
    action = PrincipalAction(date(2025, 2, 1), isin, D("0.2"), D("200"))
    move = SecurityTransfer(1, date(2025, 3, 1), 1, 2, isin, D("4"))
    result = replay_portfolio_lots({1: [bought], 2: [sold]}, {1: [action]}, [move])
    assert result[1][0][isin] == [Lot(D("6"), D("484.8"), date(2025, 1, 1))]
    assert result[2][0][isin] == []
    assert result[2][1][0].cost == D("323.2")
    assert result[2][1][0].result == D("272.8")
    assert result[1][2][0].realized_result == D("-2")
    with pytest.raises(IncompleteHistory, match="same date"):
        replay_portfolio_lots({1: [bought], 2: [sold]}, {1: [action]},
                              [SecurityTransfer(1, date(2025, 4, 1), 1, 2, isin, D("4"))])
    with pytest.raises(IncompleteHistory, match="exceeds"):
        replay_portfolio_lots({1: [bought]}, {},
                              [SecurityTransfer(1, date(2025, 3, 1), 1, 2, isin, D("11"))])


def test_transfer_rejects_same_day_destination_lot_without_changing_either_account():
    isin = "RU000A000001"
    source = {isin: [Lot(D("4"), D("404"), date(2025, 1, 1))]}
    destination = {isin: [Lot(D("2"), D("180"), date(2025, 1, 1))]}
    with pytest.raises(IncompleteHistory, match="shares an acquisition date"):
        transfer_lots(source, destination, isin, D("3"))
    assert source[isin] == [Lot(D("4"), D("404"), date(2025, 1, 1))]
    assert destination[isin] == [Lot(D("2"), D("180"), date(2025, 1, 1))]


def test_amortization_and_full_redemption_allocate_original_cost():
    isin = "RU000A000001"
    lots = {isin: [Lot(D("10"), D("950"), date(2025, 1, 1))]}
    partial = amortize_lots(lots, isin, D("0.2"), D("200"))
    assert partial.cost_reduction == D("190")
    assert partial.realized_result == D("10")
    assert lots[isin][0].quantity == D("10")
    assert lots[isin][0].cost == D("760")
    final = amortize_lots(lots, isin, D("1"), D("800"))
    assert final.cost_reduction == D("760")
    assert final.realized_result == D("40")
    assert lots[isin] == []
    with pytest.raises(IncompleteHistory, match="no confirmed lots"):
        amortize_lots(lots, isin, D("1"), D("100"))


def test_principal_actions_replay_with_fifo_and_block_unknown_intraday_order():
    isin = "RU000A000001"
    trades = [_trade("trade-1", date(2025, 1, 1), "10", "950", "0"),
              _trade("trade-2", date(2025, 4, 1), "-2", "210", "2")]
    actions = [PrincipalAction(date(2025, 2, 1), isin, D("0.2"), D("200")),
               PrincipalAction(date(2025, 6, 1), isin, D("1"), D("640"))]
    lots, sales, returned = fifo_with_principal(trades, actions)
    assert sales[0].cost == D("152")
    assert sales[0].result == D("56")
    assert [item.realized_result for item in returned] == [D("10"), D("32")]
    assert [item.quantity_change for item in returned] == [D("0"), D("-8")]
    assert lots[isin] == []
    with pytest.raises(IncompleteHistory, match="same date"):
        fifo_with_principal(trades, [PrincipalAction(date(2025, 4, 1), isin, D("0.2"), D("200"))])


def test_principal_actions_reject_second_instrument_trade_on_shared_date():
    day = date(2025, 4, 1)
    first = "RU000A000001"
    second = "RU000A000002"
    opening_first = _trade("trade-1", date(2025, 1, 1), "1", "100", "0")
    opening_second = Trade(Location("synthetic", 2), "trade-2", date(2025, 1, 1),
                           date(2025, 1, 1), second, "Synthetic second", D("1"),
                           D("100"), D("100"), D("0"), "RUB")
    same_day_second = Trade(Location("synthetic", 3), "trade-3", day, day, second,
                            "Synthetic second", D("1"), D("100"), D("100"), D("0"), "RUB")
    actions = [PrincipalAction(day, first, D("0.2"), D("20")),
               PrincipalAction(day, second, D("0.2"), D("20"))]
    lots, _, returned = fifo_with_principal([opening_first, opening_second], actions)
    assert [item.cost_reduction for item in returned] == [D("20"), D("20")]
    assert [lots[isin][0].cost for isin in (first, second)] == [D("80"), D("80")]
    with pytest.raises(IncompleteHistory, match="same date"):
        fifo_with_principal([opening_first, opening_second, same_day_second], actions)


def test_xirr_one_year_and_ambiguous_case():
    assert abs(xirr([(date(2025, 1, 1), D("-1000")), (date(2026, 1, 1), D("1100"))]) - D("0.1")) < D("0.000001")
    with pytest.raises(IncompleteHistory):
        xirr([(date(2025, 1, 1), D("100")), (date(2025, 2, 1), D("50"))])


def test_xirr_rejects_roots_hidden_between_sampled_rates():
    # In y = 1 / (1 + r), NPV = 156(y - 5/6)(y - 10/13)(y - 1/2).
    # Annual rates 20%, 30%, and 100% are all valid roots.
    flows = list(zip((date(year, 1, 1) for year in range(2025, 2029)),
                     map(D, ("-50", "225", "-328", "156"))))
    with pytest.raises(IncompleteHistory, match="no unique root"):
        xirr(flows)


def test_xirr_accepts_unique_root_with_multiple_cash_flow_sign_changes():
    # NPV = (y - 1/2)(20y² + 2y + 2): the quadratic is positive for y > 0.
    flows = list(zip((date(year, 1, 1) for year in range(2025, 2029)),
                     map(D, ("-1", "1", "-8", "20"))))
    assert abs(xirr(flows) - D("1")) < D("0.000001")


def test_xirr_rejects_tangent_root_alongside_another_root():
    # NPV = 20(y - 1/2)²(y - 4/5), with rates 100% and 25%.
    flows = list(zip((date(year, 1, 1) for year in range(2025, 2029)),
                     map(D, ("-4", "21", "-36", "20"))))
    with pytest.raises(IncompleteHistory, match="cannot be established"):
        xirr(flows)


def test_xirr_does_not_treat_near_zero_minimum_as_a_root():
    # NPV = (y - 1/2)² + 1e-45, strictly positive for every valid rate.
    flows = [(date(2025, 1, 1), D("0.250000000000000000000000000000000000000000001")),
             (date(2026, 1, 1), D("-1")),
             (date(2027, 1, 1), D("1"))]
    with pytest.raises(IncompleteHistory):
        xirr(flows)


def test_xirr_algebraic_root_family():
    """Independent polynomial factorization supplies exact root counts."""
    def multiply(left, right):
        result = [Fraction(0)] * (len(left) + len(right) - 1)
        for i, a in enumerate(left):
            for j, b in enumerate(right):
                result[i + j] += a * b
        return result

    def dated(poly):
        denominator = lcm(*(value.denominator for value in poly))
        return [(date(2025 + i, 1, 1), D((value * denominator).numerator))
                for i, value in enumerate(poly)]

    roots = [Fraction(1, 5), Fraction(1, 3), Fraction(1, 2),
             Fraction(2, 3), Fraction(4, 5)]
    for triple in combinations(roots, 3):
        poly = [Fraction(1)]
        for root in triple:
            poly = multiply(poly, [-root, Fraction(1)])
        with pytest.raises(IncompleteHistory):
            xirr(dated(poly))

    for root in roots:
        # y² + y + 1 is positive at every y > 0, leaving one valid root.
        poly = multiply([-root, Fraction(1)], [Fraction(1)] * 3)
        expected = D(root.denominator) / D(root.numerator) - 1
        assert abs(xirr(dated(poly)) - expected) < D("0.000001")
