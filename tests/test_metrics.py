"""Independent account calculation controls. SPDX-License-Identifier: GPL-3.0-or-later"""

import csv
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from io import StringIO

from svoibumagi.exports import metrics_csv
from svoibumagi.metrics import evaluate_account, evaluate_combined, recorded_income
from svoibumagi.storage import Storage
from svoibumagi.web import TEMPLATES


def _fixture():
    account = {"id": 1, "broker": "sber", "kind": "brokerage", "name": "Synthetic"}
    reports = [{
        "attempt_id": 1, "period_start": "2025-01-01", "period_end": "2026-01-01", "unparsed": [],
        "checks": [{"kind": "position", "status": "matched"}, {"kind": "cash", "status": "matched"}],
        "positions": [{"isin": "RU000A000001", "start_quantity": "0", "end_quantity": "1", "name": "Synthetic", "currency": "RUB"}],
        "cash_snapshots": [{"start_amount": "0", "end_amount": "0", "currency": "RUB"}],
    }]
    events = [
        {"event_type": "trade", "sheet": "synthetic", "row_number": 2,
         "payload": {"broker_id": "deal-1", "traded_at": "2025-01-01", "settled_at": "2025-01-01", "isin": "RU000A000001",
                     "name": "Synthetic", "quantity": "1", "price": "100", "amount": "100", "commission": "0", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 3,
         "payload": {"kind": "deposit", "date": "2025-01-01", "amount": "100", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 4,
         "payload": {"kind": "trade_cash", "date": "2025-01-01", "amount": "-100", "currency": "RUB"}},
    ]
    prices = {"RU000A000001": {"amount": "110", "as_of": "2026-01-01", "source": "manual"}}
    return account, events, reports, prices


def test_account_xirr_requires_complete_source_and_valuation():
    account, events, reports, prices = _fixture()
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["terminal"] == D("110")
    assert result["cost_basis"] == D("100")
    assert abs(result["xirr"] - D("0.1")) < D("0.000001")
    missing_history = evaluate_account(account, events, reports, prices, None)
    assert missing_history["status"] == "unavailable"
    assert not missing_history["combined_xirr_ready"]
    assert evaluate_combined([missing_history])["xirr"] is None
    assert evaluate_account(account, events, reports, {}, date(2025, 1, 1))["xirr"] is None
    prices["RU000A000001"]["as_of"] = "2025-12-24"
    stale = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert stale["status"] == "unavailable" and stale["xirr"] is None
    assert any("stale price" in reason for reason in stale["reasons"])


def test_missing_preserved_broker_row_blocks_verified_financial_metrics():
    account, events, reports, prices = _fixture()
    events[0]["payload"]["accrued_interest"] = "0"
    for event in events:
        event["source_type"] = "broker"
        event["source_row_verified"] = True
    verified = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert verified["status"] == verified["xirr_status"] == "available"

    events[0]["source_row_verified"] = False
    missing = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert missing["status"] == missing["xirr_status"] == "unavailable"
    assert missing["cost_basis"] is missing["realized"] is missing["xirr"] is None
    assert missing["cash"] == D("0") and missing["terminal"] == D("110")
    assert "broker event has no preserved matching source row" in missing["reasons"]


def test_conflicting_source_account_numbers_block_verified_financial_metrics():
    account, events, reports, prices = _fixture()
    account["account_number_conflict"] = True
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == result["xirr_status"] == "unavailable"
    assert result["cost_basis"] is result["realized"] is result["xirr"] is None
    assert result["cash"] == D("0") and result["terminal"] == D("110")
    assert "committed reports disagree on the source account number" in result["reasons"]
    assert evaluate_combined([result])["xirr"] is None


def test_opening_cash_is_included_in_partial_period_balance_check():
    account, events, reports, prices = _fixture()
    reports[0]["cash_snapshots"][0].update({"start_amount": "25", "end_amount": "25"})
    result = evaluate_account(account, events, reports, prices, None)
    assert "first report has no confirmed zero opening cash balance" in result["reasons"]
    assert "ledger cash movements do not match latest reported balance" not in result["reasons"]


def test_uncovered_period_does_not_claim_final_cash_mismatch():
    account, events, reports, prices = _fixture()
    first = deepcopy(reports[0])
    first["period_end"] = "2025-01-31"
    second = deepcopy(reports[0])
    second.update({"attempt_id": 2, "period_start": "2025-03-01", "period_end": "2026-01-01"})
    second["cash_snapshots"][0]["end_amount"] = "900"
    result = evaluate_account(account, events, [first, second], prices, None)
    assert result["gaps"] == [{"start": "2025-02-01", "end": "2025-02-28", "days": 28}]
    assert "ledger cash movements do not match latest reported balance" not in result["reasons"]


def test_fifo_fractional_cost_is_rounded_only_in_display_and_csv():
    account, events, reports, prices = _fixture()
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    result["cost_basis"] = D("39016.100000000000000000000001")
    result["realized"] = D("-60.219999999999999999999999")
    combined = evaluate_combined([result])
    assert result["realized"] != D("-60.22")  # Accounting value has not been changed.

    rows = list(csv.DictReader(StringIO(metrics_csv([result], combined).decode("utf-8-sig"))))
    assert [(row["open_fifo_cost_rub"], row["realized_fifo_rub"]) for row in rows] == [
        ("39016.10", "-60.22"), ("39016.10", "-60.22")]

    html = TEMPLATES.get_template("home.html").render(
        logged_in=True, csrf="synthetic", accounts=[], metrics=[result], combined=combined,
        estimate={"positions": []}, snapshots=[], attempts=[], event_count=0, today="2025-01-01")
    assert "Себестоимость открытых партий FIFO: 39016.10 ₽" in html
    assert "реализованный результат: -60.22 ₽" in html
    assert "-60.219999999999999999999999" not in html


def test_metrics_csv_keeps_reported_cash_and_priced_value_when_history_has_a_gap():
    account, events, reports, prices = _fixture()
    reports[0]["period_end"] = "2025-01-31"
    later = deepcopy(reports[0])
    later["attempt_id"] = 2
    later["period_start"] = "2025-03-01"
    later["period_end"] = "2025-03-31"
    later["positions"][0]["start_quantity"] = "1"
    reports.append(later)
    prices["RU000A000001"]["as_of"] = "2025-03-31"
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable" and result["xirr"] is None
    assert result["cash"] == 0 and result["securities"] == result["terminal"] == D("110")
    assert result["gaps"] == [{"start": "2025-02-01", "end": "2025-02-28", "days": 28}]

    rows = list(csv.DictReader(StringIO(metrics_csv([result], evaluate_combined([result])).decode("utf-8-sig"))))
    assert [(rows[0][key], rows[1][key]) for key in ("cash_rub", "securities_rub", "terminal_rub")] == [
        ("0", ""), ("110", ""), ("110", "")]
    assert rows[0]["xirr_annual"] == rows[0]["open_fifo_cost_rub"] == rows[0]["realized_fifo_rub"] == ""


def test_non_rub_report_values_are_not_exported_as_rubles():
    account, events, reports, prices = _fixture()
    reports[0]["cash_snapshots"].append({"start_amount": "0", "end_amount": "10", "currency": "USD"})
    cash_result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert cash_result["status"] == "unavailable"
    assert cash_result["cash"] is cash_result["terminal"] is None
    cash_row = next(csv.DictReader(StringIO(metrics_csv(
        [cash_result], evaluate_combined([cash_result])).decode("utf-8-sig"))))
    assert cash_row["cash_rub"] == cash_row["terminal_rub"] == ""

    account, events, reports, prices = _fixture()
    reports[0]["positions"].append({"isin": "US0000000001", "start_quantity": "0",
                                    "end_quantity": "2", "name": "Synthetic foreign", "currency": "USD"})
    prices["US0000000001"] = {"amount": "15", "as_of": "2026-01-01", "source": "manual"}
    position_result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert position_result["status"] == "unavailable"
    assert position_result["securities"] is position_result["terminal"] is None
    position_row = next(csv.DictReader(StringIO(metrics_csv(
        [position_result], evaluate_combined([position_result])).decode("utf-8-sig"))))
    assert position_row["securities_rub"] == position_row["terminal_rub"] == ""


def test_sale_cash_stays_internal_while_same_day_withdrawal_is_external():
    account, events, reports, _ = _fixture()
    reports[0]["positions"][0]["end_quantity"] = "0"
    reports[0]["cash_snapshots"][0]["end_amount"] = "100"
    events.extend([
        {"event_type": "trade", "sheet": "synthetic", "row_number": 5,
         "payload": {"broker_id": "deal-2", "traded_at": "2026-01-01", "settled_at": "2026-01-01",
                     "isin": "RU000A000001", "name": "Synthetic", "quantity": "-1", "price": "120",
                     "amount": "120", "commission": "0", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 6,
         "payload": {"kind": "trade_cash", "date": "2026-01-01", "amount": "120", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 7,
         "payload": {"kind": "withdrawal", "date": "2026-01-01", "amount": "-20", "currency": "RUB"}},
    ])
    result = evaluate_account(account, events, reports, {}, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["realized"] == D("20") and result["terminal"] == D("100")
    assert result["external_flows"] == [(date(2025, 1, 1), D("-100")),
                                        (date(2026, 1, 1), D("20"))]
    assert abs(result["xirr"] - D("0.2")) < D("0.000001")


def test_unallocated_daily_broker_fee_blocks_verified_fifo_result():
    account, events, reports, prices = _fixture()
    events[1]["payload"]["amount"] = "102"
    events.append({"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                   "payload": {"kind": "fee", "date": "2025-06-01", "amount": "-2", "currency": "RUB"}})
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable" and result["realized"] is None
    assert result["cost_basis"] is None and result["xirr_status"] == "available"
    assert abs(result["xirr"] - (D("110") / D("102") - 1)) < D("0.000001")
    assert result["income"]["fees_paid_cash"] == D("2")
    assert any("broker fee is recorded by day without trade allocation" in reason
               for reason in result["reasons"])
    combined = evaluate_combined([result, {**result, "account": {**account, "id": 2}}])
    assert combined["status"] == "unavailable" and combined["xirr_status"] == "available"
    assert combined["cost_basis"] is None and combined["xirr"] == result["xirr"]
    exported = list(csv.DictReader(StringIO(metrics_csv([result], evaluate_combined([result])).decode("utf-8-sig"))))
    assert exported[0]["xirr_status"] == exported[1]["xirr_status"] == "available"
    assert exported[0]["open_fifo_cost_rub"] == exported[1]["open_fifo_cost_rub"] == ""
    assert D(exported[0]["xirr_annual"]) == result["xirr"]


def test_bond_accrued_interest_is_separate_from_fifo_and_included_in_cash_return():
    account, events, reports, prices = _fixture()
    events[0]["payload"]["accrued_interest"] = "10"
    events[2]["payload"]["amount"] = "-110"
    events[1]["payload"]["amount"] = "110"
    events.append({"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                   "payload": {"kind": "coupon", "date": "2025-06-01", "amount": "30", "currency": "RUB"}})
    reports[0]["cash_snapshots"][0]["end_amount"] = "30"
    prices["RU000A000001"]["amount"] = "105"
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["cost_basis"] == D("100") and result["realized"] == 0
    assert result["terminal"] == D("135")
    assert result["income"]["coupon"] == D("30")
    assert result["income"]["accrued_interest_net"] == D("-10")
    assert result["income"]["coupon_net_of_accrued_interest"] == D("20")
    assert abs(result["xirr"] - (D("135") / D("110") - 1)) < D("0.000001")


def test_legacy_sber_trade_without_accrued_field_is_not_verified():
    account, events, reports, prices = _fixture()
    events[0]["source_type"] = "broker"
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert "legacy Sber trade lacks a verified accrued-interest field" in result["reasons"]


def test_selected_metric_date_excludes_future_report_events_and_requires_terminal_report(tmp_path, monkeypatch):
    account, events, reports, _ = _fixture()
    first = {**reports[0], "period_end": "2025-01-31"}
    second = {**reports[0], "attempt_id": 2, "period_start": "2025-02-01", "period_end": "2025-02-28",
              "positions": [{**reports[0]["positions"][0], "start_quantity": "1", "end_quantity": "0"}],
              "cash_snapshots": [{"start_amount": "0", "end_amount": "125", "currency": "RUB"}]}
    events.extend([
        {"event_type": "trade", "sheet": "synthetic", "row_number": 5,
         "payload": {"broker_id": "deal-2", "traded_at": "2025-02-20", "settled_at": "2025-02-21",
                     "isin": "RU000A000001", "name": "Synthetic", "quantity": "-1", "price": "120",
                     "amount": "120", "commission": "0", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 6,
         "payload": {"kind": "trade_cash", "date": "2025-02-21", "amount": "120", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 7,
         "payload": {"kind": "coupon", "date": "2025-02-22", "amount": "5", "currency": "RUB"}},
    ])
    store = Storage(tmp_path / "private")
    account_id = store.create_account("sber", "brokerage", "Synthetic")
    store.confirm_account_history(account_id, date(2025, 1, 1), "synthetic complete history")
    monkeypatch.setattr(store, "committed_reports", lambda _: [first, second])
    monkeypatch.setattr(store, "list_events", lambda _=None: events)
    store.add_price("RU000A000001", D("110"), date(2025, 1, 31))

    january = store.financial_metrics(date(2025, 1, 31))[0]
    assert january["status"] == "available"
    assert january["terminal"] == D("110") and january["realized"] == D("0")
    assert january["income"]["coupon"] == 0
    missing = store.financial_metrics(date(2025, 2, 15))[0]
    assert missing["status"] == "unavailable" and missing["as_of"] == date(2025, 2, 15)
    assert "нет подтверждённого отчёта с конечным остатком" in missing["reasons"][0]
    february = store.financial_metrics(date(2025, 2, 28))[0]
    assert february["status"] == "available" and february["terminal"] == D("125")
    assert february["realized"] == D("20")
    assert february["income"]["coupon"] == D("5")


def test_duplicate_isin_snapshot_rows_are_summed_for_account_and_cross_report_checks():
    account, events, reports, prices = _fixture()
    original = reports[0]["positions"][0]
    reports[0]["positions"] = [{**original, "end_quantity": "0.4"},
                               {**original, "end_quantity": "0.6"}]
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["securities"] == D("110")

    earlier = {**reports[0], "attempt_id": 2, "period_end": "2025-06-30"}
    later = {**reports[0], "attempt_id": 3, "period_start": "2025-07-01",
             "positions": [{**original, "end_quantity": "0.5"},
                           {**original, "end_quantity": "0.6"}]}
    result = evaluate_account(account, events, [earlier, later], prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert "position snapshots disagree with ledger trades between reports" in result["reasons"]


def test_same_currency_cash_rows_are_checked_between_reports():
    account, events, reports, prices = _fixture()
    early = {**reports[0], "attempt_id": 2, "period_end": "2025-06-30",
             "cash_snapshots": [{"start_amount": "0", "end_amount": "0", "currency": "RUB"},
                                {"start_amount": "0", "end_amount": "0", "currency": "RUB"}]}
    late = {**reports[0], "attempt_id": 3, "period_start": "2025-07-01",
            "cash_snapshots": [{"start_amount": "0", "end_amount": "5", "currency": "RUB"},
                               {"start_amount": "0", "end_amount": "5", "currency": "RUB"}]}
    events.append({"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                   "payload": {"kind": "deposit", "date": "2025-07-15", "amount": "10", "currency": "RUB"}})
    result = evaluate_account(account, events, [early, late], prices, date(2025, 1, 1))
    assert "cash snapshots disagree with ledger movements between reports" not in result["reasons"]
    late["cash_snapshots"][1]["end_amount"] = "6"
    result = evaluate_account(account, events, [early, late], prices, date(2025, 1, 1))
    assert "cash snapshots disagree with ledger movements between reports" in result["reasons"]


def test_account_calculation_rejects_unclassified_transfer():
    account, events, reports, prices = _fixture()
    events.append({"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                   "payload": {"kind": "transfer_unclassified", "date": "2025-01-01", "amount": "0", "currency": "RUB"}})
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert any("unclassified" in reason for reason in result["reasons"])


def test_dividend_tax_metadata_blocks_verified_financial_result_until_separate_event_exists():
    account, events, reports, prices = _fixture()
    events.append({"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                   "payload": {"kind": "dividend", "date": "2025-06-01", "amount": "87",
                               "withheld_tax": "13", "currency": "RUB"}})
    reports[0]["cash_snapshots"][0]["end_amount"] = "87"
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable" and result["xirr"] is None
    assert "dividend withholding has no matching separate tax event" in result["reasons"]
    assert result["income"]["dividend_with_tax_unpaired"] == D("87")
    assert result["income"]["dividend_gross_known"] == 0


def test_dividend_with_separate_tax_event_can_be_verified():
    account, events, reports, prices = _fixture()
    events.extend([
        {"event_type": "cash", "sheet": "synthetic", "row_number": 5,
         "payload": {"kind": "dividend", "date": "2025-06-01", "amount": "100",
                     "withheld_tax": "13", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 5,
         "payload": {"kind": "tax_withheld", "date": "2025-06-01", "amount": "-13",
                     "component": "dividend_tax", "currency": "RUB"}},
    ])
    reports[0]["cash_snapshots"][0]["end_amount"] = "87"
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["cash"] == D("87")
    assert result["income"]["dividend_gross_known"] == D("100")
    assert result["income"]["tax_withheld"] == D("13")
    combined = evaluate_combined([result, {**result, "account": {**account, "id": 2}}])
    assert combined["status"] == "available"
    assert combined["income"]["dividend_gross_known"] == D("200")
    assert combined["income"]["tax_withheld"] == D("26")


def test_recorded_income_separates_unknown_tax_and_stops_at_report_date():
    events = [
        {"event_type": "cash", "payload": {"kind": "dividend", "date": "2025-01-02", "amount": "40", "currency": "RUB"}},
        {"event_type": "cash", "payload": {"kind": "dividend_net", "date": "2025-01-03", "amount": "15", "currency": "RUB"}},
        {"event_type": "cash", "payload": {"kind": "coupon", "date": "2025-01-04", "amount": "5", "currency": "RUB"}},
        {"event_type": "cash", "payload": {"kind": "tax_withheld", "date": "2025-01-05", "amount": "-3", "currency": "RUB"}},
        {"event_type": "cash", "payload": {"kind": "coupon", "date": "2025-02-01", "amount": "9", "currency": "RUB"}},
    ]
    income = recorded_income(events, date(2025, 1, 31))
    assert income == {"dividend_gross_known": D(0), "dividend_tax_unknown": D(40),
                      "dividend_with_tax_unpaired": D(0),
                      "dividend_net": D(15), "coupon": D(5), "tax_withheld": D(3),
                      "accrued_interest_net": D(0), "coupon_net_of_accrued_interest": D(5),
                      "fees_paid_cash": D(0)}
    assert all(value == 0 for value in recorded_income(events, None).values())


def test_recorded_bond_interest_uses_signed_sber_trades_and_alfa_cash_rows():
    def trade(day, quantity, accrued):
        return {"event_type": "trade", "payload": {"settled_at": day, "quantity": quantity,
                 "accrued_interest": accrued, "currency": "RUB"}}

    sber = [trade("2025-01-02", "2", "10"), trade("2025-01-03", "-1", "15"),
            {"event_type": "cash", "payload": {"kind": "coupon", "date": "2025-01-04",
                                               "amount": "30", "currency": "RUB"}},
            trade("2025-02-01", "-1", "9")]
    january = recorded_income(sber, date(2025, 1, 31))
    assert january["accrued_interest_net"] == D("5")
    assert january["coupon_net_of_accrued_interest"] == D("35")
    assert recorded_income(sber, date(2025, 2, 1))["accrued_interest_net"] == D("14")

    alfa = [{"event_type": "cash", "payload": {"kind": "accrued_interest", "date": "2025-01-02",
                                                 "amount": "-3", "currency": "RUB"}},
            {"event_type": "cash", "payload": {"kind": "accrued_interest", "date": "2025-01-03",
                                               "amount": "8", "currency": "RUB"}},
            {"event_type": "cash", "payload": {"kind": "coupon", "date": "2025-01-04",
                                               "amount": "10", "currency": "RUB"}}]
    assert recorded_income(alfa, date(2025, 1, 31))["coupon_net_of_accrued_interest"] == D("15")


def test_recorded_income_uses_each_tax_row_once_and_only_through_valuation_date():
    dividend = {"event_type": "cash", "source_type": "broker", "sha256": "synthetic",
                "sheet": "income", "row_number": 7,
                "payload": {"kind": "dividend", "date": "2025-01-02", "amount": "100",
                            "withheld_tax": "13", "currency": "RUB"}}
    tax = {**dividend, "payload": {"kind": "tax_withheld", "component": "dividend_tax",
                                   "date": "2025-01-02", "amount": "-13", "currency": "RUB"}}
    future = {**tax, "payload": {**tax["payload"], "date": "2025-02-02"}}
    before = recorded_income([dividend, dividend, future], date(2025, 1, 31))
    assert before["dividend_gross_known"] == 0
    assert before["dividend_with_tax_unpaired"] == D("200")
    after = recorded_income([dividend, dividend, tax], date(2025, 1, 31))
    assert after["dividend_gross_known"] == D("100")
    assert after["dividend_with_tax_unpaired"] == D("100")
    assert after["tax_withheld"] == D("13")


def test_combined_xirr_needs_same_date_and_complete_accounts():
    account, events, reports, prices = _fixture()
    first = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    second = evaluate_account({**account, "id": 2}, events, reports, prices, date(2025, 1, 1))
    combined = evaluate_combined([first, second])
    assert combined["terminal"] == D("220")
    assert abs(combined["xirr"] - D("0.1")) < D("0.000001")
    assert evaluate_combined([first, {**second, "as_of": date(2026, 1, 2)}])["status"] == "unavailable"


def test_two_distinct_accounts_match_hand_calculated_overview_and_csv():
    account, events, reports, prices = _fixture()
    second_account = {**account, "id": 2, "name": "Synthetic second"}
    second_events = deepcopy(events)
    second_events[0]["payload"].update({"quantity": "2", "amount": "200"})
    second_events[1]["payload"]["amount"] = "200"
    second_events[2]["payload"]["amount"] = "-200"
    second_events.extend([
        {"event_type": "trade", "sheet": "synthetic", "row_number": 5,
         "payload": {"broker_id": "deal-2", "traded_at": "2026-01-01", "settled_at": "2026-01-01",
                     "isin": "RU000A000001", "name": "Synthetic", "quantity": "-1", "price": "130",
                     "amount": "130", "commission": "0", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 6,
         "payload": {"kind": "trade_cash", "date": "2026-01-01", "amount": "130", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 7,
         "payload": {"kind": "withdrawal", "date": "2026-01-01", "amount": "-30", "currency": "RUB"}},
    ])
    second_reports = deepcopy(reports)
    second_reports[0]["positions"][0]["end_quantity"] = "1"
    second_reports[0]["cash_snapshots"][0]["end_amount"] = "100"

    first = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    second = evaluate_account(second_account, second_events, second_reports, prices, date(2025, 1, 1))
    combined = evaluate_combined([first, second])
    assert [item["status"] for item in (first, second, combined)] == ["available"] * 3
    assert [(item["cash"], item["securities"], item["terminal"], item["cost_basis"], item["realized"])
            for item in (first, second, combined)] == [
                (D("0"), D("110"), D("110"), D("100"), D("0")),
                (D("100"), D("110"), D("210"), D("100"), D("30")),
                (D("100"), D("220"), D("320"), D("200"), D("30")),
            ]
    assert second["external_flows"] == [(date(2025, 1, 1), D("-200")),
                                         (date(2026, 1, 1), D("30"))]
    for item, expected in ((first, D("0.1")), (second, D("0.2")), (combined, D(1) / D(6))):
        assert abs(item["xirr"] - expected) < D("0.000001")

    rows = list(csv.DictReader(StringIO(metrics_csv([first, second], combined).decode("utf-8-sig"))))
    assert [(row["scope"], row["account_id"], row["cash_rub"], row["securities_rub"],
             row["terminal_rub"], row["open_fifo_cost_rub"], row["realized_fifo_rub"])
            for row in rows] == [
                ("account", "1", "0", "110", "110", "100.00", "0.00"),
                ("account", "2", "100", "110", "210", "100.00", "30.00"),
                ("combined", "", "100", "220", "320", "200.00", "30.00"),
            ]
    for row, expected in zip(rows, (D("0.1"), D("0.2"), D(1) / D(6))):
        assert abs(D(row["xirr_annual"]) - expected) < D("0.000001")

    html = TEMPLATES.get_template("home.html").render(
        logged_in=True, csrf="synthetic", accounts=[account, second_account],
        metrics=[first, second], combined=combined, estimate={"positions": []},
        snapshots=[], attempts=[], event_count=0, today="2026-01-01")
    assert "Общий портфель на 2026-01-01: 320 ₽" in html
    assert "Synthetic second — 2026-01-01" in html
    assert "Денежный остаток: 100 ₽; бумаги: 110 ₽; сумма: 210 ₽" in html
    assert "Себестоимость открытых партий FIFO: 100.00 ₽; реализованный результат: 30.00 ₽" in html


def test_combined_xirr_excludes_paired_internal_transfer():
    account, events, reports, prices = _fixture()
    first_events = [dict(event) for event in events]
    first_events.append({"event_type": "cash", "sheet": "synthetic", "row_number": 5,
                         "payload": {"kind": "internal_out", "date": "2025-07-01", "amount": "-10",
                                     "currency": "RUB", "paired_event_id": 11}})
    first_reports = [{**reports[0], "cash_snapshots": [{"start_amount": "0", "end_amount": "-10", "currency": "RUB"}]}]
    second_events = [{"event_type": "cash", "sheet": "synthetic", "row_number": 6,
                      "payload": {"kind": "internal_in", "date": "2025-07-01", "amount": "10",
                                  "currency": "RUB", "paired_event_id": 10}}]
    second_reports = [{**reports[0], "positions": [],
                       "cash_snapshots": [{"start_amount": "0", "end_amount": "10", "currency": "RUB"}]}]
    first = evaluate_account(account, first_events, first_reports, prices, date(2025, 1, 1))
    second = evaluate_account({**account, "id": 2}, second_events, second_reports, {}, date(2025, 1, 1))
    assert first["status"] == "available" and second["status"] == "available"
    assert first["external_flows"] == [(date(2025, 1, 1), D("-100"))]
    assert second["external_flows"] == []
    combined = evaluate_combined([first, second])
    assert combined["terminal"] == D("110")
    assert abs(combined["xirr"] - D("0.1")) < D("0.000001")


def test_non_rub_account_value_is_unavailable():
    account, events, reports, prices = _fixture()
    reports[0]["cash_snapshots"][0]["currency"] = "USD"
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert any("non-RUB" in reason for reason in result["reasons"])


def test_zero_foreign_currency_position_does_not_require_conversion():
    account, events, reports, prices = _fixture()
    reports[0]["positions"].append({"isin": "US0000000001", "start_quantity": "0",
                                   "end_quantity": "0", "name": "Sold out", "currency": "USD"})
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["terminal"] == D("110")


def test_sold_out_instrument_can_be_derived_only_from_complete_history():
    account, events, reports, prices = _fixture()
    omitted = "RU000A000002"
    reports[0]["checks"].append({"kind": "position", "key": omitted, "status": "unavailable",
                                  "reason": "instrument absent from report position snapshot"})
    reports[0]["cash_snapshots"][0]["end_amount"] = "5"
    for row, when, quantity, amount in ((5, "2025-03-01", "1", "20"), (7, "2025-06-01", "-1", "25")):
        events.append({"event_type": "trade", "sheet": "synthetic", "row_number": row,
                       "payload": {"broker_id": f"deal-{row}", "traded_at": when, "settled_at": when,
                                   "isin": omitted, "name": "Sold out", "quantity": quantity,
                                   "price": amount, "amount": amount, "commission": "0", "currency": "RUB"}})
        events.append({"event_type": "cash", "sheet": "synthetic", "row_number": row + 1,
                       "payload": {"kind": "trade_cash", "date": when,
                                   "amount": "-20" if quantity == "1" else "25", "currency": "RUB"}})
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "available"
    assert result["inferred_zero_count"] == 1
    assert result["realized"] == D("5")
    assert evaluate_account(account, events, reports, prices, None)["status"] == "unavailable"
    events[-2]["payload"]["quantity"] = "-0.5"
    assert evaluate_account(account, events, reports, prices, date(2025, 1, 1))["status"] == "unavailable"


def test_intermediate_report_balances_are_checked_against_full_ledger():
    account, events, reports, prices = _fixture()
    early = {**reports[0], "attempt_id": 2, "period_end": "2025-06-30"}
    late = {**reports[0], "attempt_id": 3, "period_start": "2025-07-01"}
    reports = [early, late]
    events.extend([
        {"event_type": "cash", "sheet": "synthetic", "row_number": 5,
         "payload": {"kind": "deposit", "date": "2025-04-01", "amount": "5", "currency": "RUB"}},
        {"event_type": "cash", "sheet": "synthetic", "row_number": 6,
         "payload": {"kind": "withdrawal", "date": "2025-08-01", "amount": "-5", "currency": "RUB"}},
    ])
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert "cash snapshots disagree with ledger movements between reports" in result["reasons"]
    assert "cash snapshots disagree with ledger movements between reports" in evaluate_account(
        account, events, reports, prices, None)["reasons"]

    events[-2:] = [
        {"event_type": "trade", "sheet": "synthetic", "row_number": 5,
         "payload": {"broker_id": "deal-2", "traded_at": "2025-04-01", "settled_at": "2025-04-01",
                     "isin": "RU000A000001", "name": "Synthetic", "quantity": "1", "price": "100",
                     "amount": "100", "commission": "0", "currency": "RUB"}},
        {"event_type": "trade", "sheet": "synthetic", "row_number": 6,
         "payload": {"broker_id": "deal-3", "traded_at": "2025-08-01", "settled_at": "2025-08-01",
                     "isin": "RU000A000001", "name": "Synthetic", "quantity": "-1", "price": "100",
                     "amount": "100", "commission": "0", "currency": "RUB"}},
    ]
    result = evaluate_account(account, events, reports, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert "position snapshots disagree with ledger trades between reports" in result["reasons"]


def test_account_metrics_list_exact_report_gaps_even_when_boundary_balances_match():
    account, events, reports, prices = _fixture()
    periods = [("2025-01-01", "2025-01-31"), ("2025-02-03", "2025-02-28"),
               ("2025-03-05", "2026-01-01")]
    fragmented = [{**reports[0], "attempt_id": number, "period_start": start, "period_end": end}
                  for number, (start, end) in enumerate(periods, 1)]
    result = evaluate_account(account, events, fragmented, prices, date(2025, 1, 1))
    assert result["status"] == "unavailable"
    assert result["gaps"] == [
        {"start": "2025-02-01", "end": "2025-02-02", "days": 2},
        {"start": "2025-03-01", "end": "2025-03-04", "days": 4},
    ]
    assert [reason for reason in result["reasons"] if reason.startswith("нет отчёта за")] == [
        "нет отчёта за 2025-02-01 — 2025-02-02 (2 дн.)",
        "нет отчёта за 2025-03-01 — 2025-03-04 (4 дн.)",
    ]
