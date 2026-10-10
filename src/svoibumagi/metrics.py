"""Account-level calculations with explicit completeness gates. SPDX-License-Identifier: GPL-3.0-or-later"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from .calculations import IncompleteHistory, PrincipalAction, fifo_with_principal, xirr
from .prices import MAX_PRICE_AGE_DAYS, price_age_days
from .reports import Location, Trade


def _trade(event: dict) -> Trade:
    data = event["payload"]
    return Trade(
        Location(event["sheet"], event["row_number"]), data.get("broker_id"),
        date.fromisoformat(data["traded_at"]), date.fromisoformat(data["settled_at"]),
        data["isin"], data["name"], Decimal(data["quantity"]), Decimal(data["price"]),
        Decimal(data["amount"]), Decimal(data["commission"]), data["currency"],
        Decimal(data.get("accrued_interest", "0")),
        event.get("execution_time"), event.get("execution_time_required", False),
    )


def _position_quantities(positions: list[dict]) -> dict[str, Decimal]:
    quantities: dict[str, Decimal] = {}
    for position in positions:
        isin = position.get("isin")
        if isin:
            quantities[isin] = quantities.get(isin, Decimal(0)) + Decimal(position["end_quantity"])
    return quantities


def _cash_balances(snapshots: list[dict]) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    for snapshot in snapshots:
        currency = snapshot["currency"]
        balances[currency] = balances.get(currency, Decimal(0)) + Decimal(snapshot["end_amount"])
    return balances


def _opening_balances(rows: list[dict], key: str, field: str) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    for row in rows:
        identity = row.get(key)
        if identity:
            balances[identity] = balances.get(identity, Decimal(0)) + Decimal(row[field])
    return {identity: amount for identity, amount in balances.items() if amount != 0}


def _position_boundary_balances(rows: list[dict], field: str) -> dict[tuple[str, str], Decimal]:
    balances: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (row["isin"], row["currency"])
        balances[key] = balances.get(key, Decimal(0)) + Decimal(row[field])
    return {key: amount for key, amount in balances.items() if amount != 0}


def _event_dates(event: dict) -> tuple[date, ...]:
    payload = event["payload"]
    if event["event_type"] == "trade":
        return date.fromisoformat(payload["traded_at"]), date.fromisoformat(payload["settled_at"])
    return (date.fromisoformat(payload["date"]),)


def _unparsed_may_affect_gap(report: dict, start: date, end: date) -> bool:
    """Keep an unresolved source row blocking unless a parsed row dates it outside the gap."""
    for unknown in report.get("unparsed", []):
        location = unknown.get("location")
        if not location:
            return True
        located = [row for key in ("trades", "summary_trades", "cash_movements")
                   for row in report.get(key, []) if row.get("location") == location]
        if not located:
            return True
        for row in located:
            fields = ("traded_at", "settled_at") if "settled_at" in row else ("date",)
            if any(start <= date.fromisoformat(row[field]) <= end for field in fields):
                return True
    return False


def _idle_gap_boundaries_match(start: date, end: date, previous: dict, current: dict,
                               events: list[dict]) -> bool:
    """Require equal adjacent balances and no known or unresolved movement in the gap."""
    before_cash = previous["cash_snapshots"]
    after_cash = current["cash_snapshots"]
    before_positions = previous["positions"]
    after_positions = current["positions"]
    return (
        bool(before_cash and after_cash)
        and bool(before_positions and after_positions)
        and all(row.get("isin") and row.get("currency") for row in before_positions + after_positions)
        and all(row.get("currency") for row in before_cash + after_cash)
        and _position_boundary_balances(before_positions, "end_quantity")
        == _position_boundary_balances(after_positions, "start_quantity")
        and _opening_balances(before_cash, "currency", "end_amount")
        == _opening_balances(after_cash, "currency", "start_amount")
        and not any(start <= day <= end for event in events for day in _event_dates(event))
        and not _unparsed_may_affect_gap(previous, start, end)
        and not _unparsed_may_affect_gap(current, start, end)
        and not any(start <= date.fromisoformat(trade[field]) <= end
                    for report in (previous, current) for trade in report.get("summary_trades", [])
                    for field in ("traded_at", "settled_at"))
    )


def _uncovered_gap(start: date, end: date, previous: dict, current: dict,
                   events: list[dict], inactive_periods: list[dict]) -> list[tuple[date, date]]:
    """Subtract owner-confirmed idle days only when both report boundaries support them."""
    if not _idle_gap_boundaries_match(start, end, previous, current, events):
        return [(start, end)]
    covered = sorted((max(start, date.fromisoformat(item["start_date"])),
                      min(end, date.fromisoformat(item["end_date"])))
                     for item in inactive_periods if item.get("revoked_at") is None
                     and item["start_date"] <= end.isoformat()
                     and item["end_date"] >= start.isoformat())
    remaining = []
    cursor = start
    for begin, finish in covered:
        if begin > cursor:
            remaining.append((cursor, begin - timedelta(days=1)))
        cursor = max(cursor, finish + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        remaining.append((cursor, end))
    return remaining


def _tax_source(event: dict) -> tuple:
    payload = event["payload"]
    return (event.get("source_type"), event.get("sha256"), event.get("sheet"),
            event.get("row_number"), payload["date"], payload["currency"])


def recorded_income(events: list[dict], as_of: date | None) -> dict[str, Decimal]:
    """Signed RUB cash components in the committed ledger through a report date."""
    totals = {key: Decimal(0) for key in ("dividend_gross_known", "dividend_tax_unknown",
                                                "dividend_with_tax_unpaired", "dividend_net",
                                                "coupon", "tax_withheld", "accrued_interest_net",
                                                "coupon_net_of_accrued_interest", "fees_paid_cash")}
    if as_of is None:
        return totals
    for event in events:
        if event["event_type"] != "trade":
            continue
        payload = event["payload"]
        if (payload["currency"] != "RUB" or "accrued_interest" not in payload
                or date.fromisoformat(payload["settled_at"]) > as_of):
            continue
        accrued = Decimal(payload["accrued_interest"])
        totals["accrued_interest_net"] += accrued * (-1 if Decimal(payload["quantity"]) > 0 else 1)
    tax_pairs = Counter((_tax_source(event), -Decimal(event["payload"]["amount"]))
                        for event in events if event["event_type"] == "cash"
                        and event["payload"]["kind"] == "tax_withheld"
                        and event["payload"].get("component") == "dividend_tax"
                        and event["payload"]["currency"] == "RUB"
                        and date.fromisoformat(event["payload"]["date"]) <= as_of)
    for event in events:
        if event["event_type"] != "cash":
            continue
        payload = event["payload"]
        if payload["currency"] != "RUB" or date.fromisoformat(payload["date"]) > as_of:
            continue
        kind = payload["kind"]
        amount = Decimal(payload["amount"])
        if kind == "dividend":
            withholding = payload.get("withheld_tax")
            if withholding is None:
                key = "dividend_tax_unknown"
            elif Decimal(withholding) == 0:
                key = "dividend_gross_known"
            else:
                pair = (_tax_source(event), Decimal(withholding))
                if tax_pairs[pair]:
                    tax_pairs[pair] -= 1
                    key = "dividend_gross_known"
                else:
                    key = "dividend_with_tax_unpaired"
            totals[key] += amount
        elif kind in {"dividend_net", "coupon"}:
            totals[kind] += amount
        elif kind == "accrued_interest":
            totals["accrued_interest_net"] += amount
        elif kind == "tax_withheld":
            totals[kind] -= amount
        elif kind in {"fee", "trade_fee"}:
            totals["fees_paid_cash"] -= amount
    totals["coupon_net_of_accrued_interest"] = totals["coupon"] + totals["accrued_interest_net"]
    return totals


def principal_actions_from_events(events: list[dict], account_id: int) -> tuple[list[PrincipalAction], list[str]]:
    event_by_id = {str(event.get("id")): event for event in events}
    actions = []
    reasons = []
    linked_cash_ids = set()
    for event in events:
        payload = event["payload"]
        if event["event_type"] != "cash" or payload.get("kind") != "principal_action":
            continue
        reference = str(payload.get("cash_event_id"))
        linked = event_by_id.get(reference)
        fraction = Decimal(payload["principal_fraction"])
        when = date.fromisoformat(payload["date"])
        if (linked is None or linked["event_type"] != "cash"
                or linked["account_id"] != account_id
                or linked["payload"]["kind"] != "principal_return"
                or linked["payload"]["date"] != payload["date"]
                or linked["payload"]["currency"] != "RUB"
                or Decimal(linked["payload"]["amount"]) <= 0
                or not Decimal(0) < fraction <= Decimal(1)
                or reference in linked_cash_ids):
            reasons.append("principal action has no unique matching cash return")
            continue
        linked_cash_ids.add(reference)
        actions.append(PrincipalAction(when, payload["isin"], fraction,
                                       Decimal(linked["payload"]["amount"])))
    if any(event["event_type"] == "cash" and event["payload"].get("kind") == "principal_return"
           and str(event.get("id")) not in linked_cash_ids for event in events):
        reasons.append("principal return has no matching amortization or redemption action")
    return actions, reasons


def evaluate_account(account: dict, events: list[dict], reports: list[dict],
                     prices: dict[str, dict], history_from: date | None,
                     accounting: tuple | None = None, accounting_error: str | None = None,
                     transfer_prices: dict[int, dict] | None = None,
                     inactive_periods: list[dict] | None = None) -> dict:
    """Calculate only where provenance, coverage and terminal value are demonstrable."""
    reasons: list[str] = []
    if not reports:
        return {"account": account, "status": "unavailable", "reasons": ["no committed reports"],
                "as_of": None, "cash": None, "securities": None, "terminal": None,
                "cost_basis": None, "realized": None, "xirr": None, "xirr_status": "unavailable",
                "combined_xirr_ready": False,
                "flows": [], "external_flows": [],
                "in_kind_transfer_flows": [], "unpriced_in_kind_transfers": sum(
                    event["event_type"] == "security_transfer" for event in events),
                "gaps": [], "confirmable_gaps": [], "income": recorded_income(events, None)}
    ordered = sorted(reports, key=lambda r: (r["period_start"], r["period_end"], r["attempt_id"]))
    latest = max(ordered, key=lambda r: (r["period_end"], r["attempt_id"]))
    as_of = date.fromisoformat(latest["period_end"])
    first = ordered[0]
    if account.get("account_number_conflict"):
        reasons.append("committed reports disagree on the source account number")
    for period in inactive_periods or []:
        if period.get("revoked_at") is not None:
            continue
        period_start = date.fromisoformat(period["start_date"])
        period_end = date.fromisoformat(period["end_date"])
        if (period_start <= as_of and period_end >= date.fromisoformat(first["period_start"])
                and any(period_start <= day <= period_end for event in events for day in _event_dates(event))):
            reasons.append(f"подтверждение отсутствия операций за {period_start} — {period_end} "
                           "противоречит проведённой операции")
    if any(event.get("source_type") == "broker" and event.get("source_row_verified") is False
           for event in events):
        reasons.append("broker event has no preserved matching source row")
    for event in events:
        if event.get("source_type") == "manual":
            payload = event["payload"]
            effective_date = date.fromisoformat(payload["settled_at"] if event["event_type"] == "trade" else payload["date"])
            if not date.fromisoformat(first["period_start"]) <= effective_date <= as_of:
                reasons.append("manual correction is outside committed report coverage")
    if history_from is None:
        reasons.append("full history from account opening has not been confirmed")
    elif first["period_start"] != history_from.isoformat():
        reasons.append("confirmed history start does not match first report")
    if not first["cash_snapshots"] or any(Decimal(s["start_amount"]) != 0 for s in first["cash_snapshots"]):
        reasons.append("first report has no confirmed zero opening cash balance")
    if any(Decimal(s["start_quantity"]) != 0 for s in first["positions"]):
        reasons.append("first report has opening positions without cost basis")
    covered_through = date.fromisoformat(first["period_end"])
    gaps = []
    confirmable_gaps = []
    previous_boundary = first
    for report in ordered[1:]:
        start = date.fromisoformat(report["period_start"])
        end = date.fromisoformat(report["period_end"])
        if start > covered_through + timedelta(days=1):
            gap_start, gap_end = covered_through + timedelta(days=1), start - timedelta(days=1)
            boundaries_match = _idle_gap_boundaries_match(gap_start, gap_end, previous_boundary, report, events)
            for missing_start, missing_end in _uncovered_gap(
                    gap_start, gap_end, previous_boundary, report, events, inactive_periods or []):
                gap = {"start": missing_start.isoformat(), "end": missing_end.isoformat(),
                       "days": (missing_end - missing_start).days + 1}
                gaps.append(gap)
                if boundaries_match:
                    confirmable_gaps.append(gap)
                reasons.append(f"нет отчёта за {missing_start} — {missing_end} ({gaps[-1]['days']} дн.)")
        if end > covered_through:
            covered_through = end
            previous_boundary = report
    trades = [_trade(e) for e in events if e["event_type"] == "trade"]
    if account["broker"] == "sber" and any(
        event["event_type"] == "trade" and event.get("source_type") == "broker"
        and "accrued_interest" not in event["payload"] for event in events
    ):
        reasons.append("legacy Sber trade lacks a verified accrued-interest field")
    principal_actions, action_reasons = principal_actions_from_events(events, account["id"])
    reasons.extend(action_reasons)
    try:
        if accounting_error:
            raise IncompleteHistory(accounting_error)
        if accounting is None and any(event["event_type"] == "security_transfer" for event in events):
            raise IncompleteHistory("security transfer requires cross-account lot replay")
        lots, sales, principal_results = accounting if accounting is not None else fifo_with_principal(trades, principal_actions)
        accounting_ready = True
    except (IncompleteHistory, ValueError) as exc:
        reasons.append(f"FIFO unavailable: {exc}")
        lots, sales, principal_results = {}, [], []
        accounting_ready = False
    position_movements = [(trade.settled_at, trade.isin, trade.quantity) for trade in trades]
    position_movements.extend((item.action.effective_at, item.action.isin, item.quantity_change)
                              for item in principal_results if item.quantity_change)
    position_movements.extend((date.fromisoformat(event["payload"]["date"]), event["payload"]["isin"],
                               Decimal(event["payload"]["quantity"]))
                              for event in events if event["event_type"] == "security_transfer")
    cash_events_for_checks = [e["payload"] for e in events if e["event_type"] == "cash"]
    by_end = sorted(ordered, key=lambda report: (report["period_end"], report["attempt_id"]))
    for previous, current in zip(by_end, by_end[1:]):
        previous_end = date.fromisoformat(previous["period_end"])
        current_end = date.fromisoformat(current["period_end"])
        if date.fromisoformat(current["period_start"]) > previous_end + timedelta(days=1):
            continue
        before_cash = _cash_balances(previous["cash_snapshots"])
        after_cash = _cash_balances(current["cash_snapshots"])
        for currency in before_cash.keys() & after_cash.keys():
            movement = sum((Decimal(event["amount"]) for event in cash_events_for_checks
                            if event["currency"] == currency
                            and previous_end < date.fromisoformat(event["date"]) <= current_end), Decimal(0))
            if abs(before_cash[currency] + movement - after_cash[currency]) > Decimal("0.01"):
                reasons.append("cash snapshots disagree with ledger movements between reports")
        before_positions = _position_quantities(previous["positions"])
        after_positions = _position_quantities(current["positions"])
        for isin in before_positions.keys() & after_positions.keys():
            movement = sum((quantity for day, key, quantity in position_movements if key == isin
                            and previous_end < day <= current_end), Decimal(0))
            if before_positions[isin] + movement != after_positions[isin]:
                reasons.append("position snapshots disagree with ledger trades between reports")
                break
    inferred_zero: list[str] = []
    can_infer_from_history = (history_from is not None and first["period_start"] == history_from.isoformat()
                              and not reasons and all(not report["unparsed"] for report in ordered))
    for report in ordered:
        if report["unparsed"]:
            reasons.append("at least one report has unresolved rows or reconciliation")
        for check in report["checks"]:
            if check["status"] == "matched":
                continue
            # A report-level missing row stays unavailable. Complete, zero-opening
            # history can independently prove zero at this date for account metrics.
            if (can_infer_from_history and check["kind"] == "position"
                    and check["status"] == "unavailable"
                    and check["reason"] == "instrument absent from report position snapshot"
                    and any(trade.isin == check["key"] and trade.settled_at <= date.fromisoformat(report["period_end"])
                            for trade in trades)
                    and sum((quantity for day, key, quantity in position_movements
                             if key == check["key"] and day <= date.fromisoformat(report["period_end"])),
                            Decimal(0)) == 0):
                inferred_zero.append(f"{check['key']} on {report['period_end']}")
                continue
            reasons.append("at least one report has unresolved rows or reconciliation")

    if can_infer_from_history:
        for report in ordered:
            cutoff = date.fromisoformat(report["period_end"])
            reported_positions = _position_quantities(report["positions"])
            for isin, reported in reported_positions.items():
                calculated = sum((quantity for day, key, quantity in position_movements
                                  if key == isin and day <= cutoff), Decimal(0))
                if calculated != reported:
                    reasons.append("ledger positions do not match an intermediate reported balance")
                    break
            reported_cash = _cash_balances(report["cash_snapshots"])
            for currency, reported in reported_cash.items():
                calculated = sum((Decimal(event["amount"]) for event in cash_events_for_checks
                                  if event["currency"] == currency and date.fromisoformat(event["date"]) <= cutoff), Decimal(0))
                if abs(calculated - reported) > Decimal("0.01"):
                    reasons.append("ledger cash does not match an intermediate reported balance")
                    break

    cash_snapshots = latest["cash_snapshots"]
    if any(snapshot["currency"] != "RUB" for report in ordered for snapshot in report["cash_snapshots"]):
        reasons.append("non-RUB cash needs currency conversion")
    if any(position["currency"] != "RUB" and
           (Decimal(position["start_quantity"]) != 0 or Decimal(position["end_quantity"]) != 0)
           for report in ordered for position in report["positions"]):
        reasons.append("non-RUB securities need currency conversion")
    non_rub_cash = any(snapshot["currency"] != "RUB" for snapshot in cash_snapshots)
    cash = (sum((Decimal(s["end_amount"]) for s in cash_snapshots), Decimal(0))
            if cash_snapshots and not non_rub_cash else None)
    if not cash_snapshots:
        reasons.append("latest report has no cash snapshot")
    elif non_rub_cash:
        reasons.append("latest report has non-RUB cash without a confirmed conversion")
    positions = latest["positions"]
    non_rub_positions = any(position["currency"] != "RUB" and Decimal(position["end_quantity"]) != 0
                            for position in positions)
    securities = Decimal(0)
    missing_prices = []
    for position in positions:
        qty = Decimal(position["end_quantity"])
        if qty == 0:
            continue
        if position["currency"] != "RUB":
            continue
        quote = prices.get(position["isin"]) if position["isin"] else None
        reported_value = position.get("reported_value_rub")
        if (reported_value is not None and not
                (quote and quote.get("source") == "manual" and quote["as_of"] == as_of.isoformat())):
            securities += Decimal(reported_value)
            continue
        if (quote is None or price_age_days(quote["as_of"], as_of) < 0
                or price_age_days(quote["as_of"], as_of) > MAX_PRICE_AGE_DAYS):
            missing_prices.append(position["isin"] or position["name"])
            continue
        securities += qty * Decimal(quote["amount"])
    if missing_prices:
        reasons.append(f"missing or stale price on valuation date for {len(missing_prices)} positions")
    terminal = securities + cash if cash is not None and not missing_prices and not non_rub_positions else None

    if any(trade.currency != "RUB" for trade in trades):
        reasons.append("non-RUB trades need currency conversion")
    realized = cost_basis = None
    if accounting_ready:
        realized = sum((sale.result for sale in sales), Decimal(0)) + sum(
            (item.realized_result for item in principal_results), Decimal(0))
        cost_basis = sum((lot.cost for bucket in lots.values() for lot in bucket), Decimal(0))
        reported_quantities = _position_quantities(positions)
        for isin in set(lots) | set(reported_quantities):
            calculated = sum((lot.quantity for lot in lots.get(isin, [])), Decimal(0))
            if calculated != reported_quantities.get(isin, Decimal(0)):
                reasons.append("FIFO lot quantities do not match latest report")
                break

    cash_records = [e for e in events if e["event_type"] == "cash"]
    cash_events = [e["payload"] for e in cash_records]
    unallocated_fee_reason = "broker fee is recorded by day without trade allocation; FIFO result is unavailable"
    unpriced_transfer_reason = "security transfer has no confirmed RUB market price on its date"
    if any(event["kind"] == "fee" and Decimal(event["amount"]) != 0 for event in cash_events):
        reasons.append(unallocated_fee_reason)
    tax_rows = [event for event in cash_records if event["payload"]["kind"] == "tax_withheld"
                and event["payload"].get("component") == "dividend_tax"]
    used_taxes: set[int] = set()
    for dividend in cash_records:
        payload = dividend["payload"]
        withheld = Decimal(payload.get("withheld_tax") or "0")
        if payload["kind"] != "dividend" or withheld <= 0:
            continue
        matches = [index for index, tax in enumerate(tax_rows)
                   if _tax_source(tax) == _tax_source(dividend)
                   and Decimal(tax["payload"]["amount"]) == -withheld]
        if len(matches) != 1 or matches[0] in used_taxes:
            reasons.append("dividend withholding has no matching separate tax event")
        else:
            used_taxes.add(matches[0])
    if len(used_taxes) != len(tax_rows):
        reasons.append("withheld tax event has no matching dividend")
    if any(event["currency"] != "RUB" for event in cash_events):
        reasons.append("non-RUB cash movements need currency conversion")
    if any(e["kind"] == "transfer_unclassified" for e in cash_events):
        reasons.append("cash transfer direction and counterparty are unclassified")
    if any(e["kind"] in {"internal_in", "internal_out"} and not e.get("paired_event_id") for e in cash_events):
        reasons.append("internal transfer has no paired event")
    if cash is not None:
        calculated_cash = sum((Decimal(e["amount"]) for e in cash_events), Decimal(0))
        if calculated_cash != cash:
            reasons.append("ledger cash movements do not match latest reported balance")
    flows = []
    external_flows = []
    in_kind_transfer_flows = []
    unpriced_in_kind_transfers = 0
    for event in cash_events:
        if event["kind"] in {"deposit", "withdrawal", "internal_in", "internal_out"}:
            flow = (date.fromisoformat(event["date"]), -Decimal(event["amount"]))
            flows.append(flow)
            if event["kind"] in {"deposit", "withdrawal"}:
                external_flows.append(flow)
    for event in events:
        if event["event_type"] != "security_transfer":
            continue
        payload = event["payload"]
        when = date.fromisoformat(payload["date"])
        quote = (transfer_prices or {}).get(payload["transfer_id"])
        if (quote is None or price_age_days(quote["as_of"], when) < 0
                or price_age_days(quote["as_of"], when) > MAX_PRICE_AGE_DAYS):
            reasons.append(unpriced_transfer_reason)
            unpriced_in_kind_transfers += 1
            continue
        transfer_flow = (when, -Decimal(payload["quantity"]) * Decimal(quote["amount"]))
        flows.append(transfer_flow)
        in_kind_transfer_flows.append(transfer_flow)
    rate = None
    combined_xirr_ready = terminal is not None and not any(
        reason not in {unallocated_fee_reason, unpriced_transfer_reason} for reason in reasons)
    if not [reason for reason in reasons if reason != unallocated_fee_reason] and terminal is not None:
        try:
            rate = xirr(flows + [(as_of, terminal)])
        except IncompleteHistory as exc:
            reasons.append(str(exc))
    status = "available" if not reasons else "unavailable"
    return {"account": account, "status": status, "reasons": list(dict.fromkeys(reasons)), "as_of": as_of,
            "cash": cash, "securities": securities if not missing_prices and not non_rub_positions else None,
            "terminal": terminal,
            "cost_basis": cost_basis if status == "available" else None,
            "realized": realized if status == "available" else None, "xirr": rate,
            "xirr_status": "available" if rate is not None else "unavailable",
            "combined_xirr_ready": combined_xirr_ready,
            "flows": flows, "external_flows": external_flows,
            "in_kind_transfer_flows": in_kind_transfer_flows,
            "unpriced_in_kind_transfers": unpriced_in_kind_transfers,
            "inferred_zero_count": len(inferred_zero),
            "gaps": gaps, "confirmable_gaps": confirmable_gaps,
            "income": recorded_income(events, as_of)}


def evaluate_combined(accounts: list[dict]) -> dict:
    if not accounts:
        return {"status": "unavailable", "reasons": ["no accounts"], "as_of": None,
                "terminal": None, "xirr": None, "xirr_status": "unavailable"}
    if any(not item.get("combined_xirr_ready", item.get("xirr") is not None) for item in accounts):
        return {"status": "unavailable", "reasons": ["at least one account has incomplete accounting"],
                "as_of": None, "terminal": None, "xirr": None, "xirr_status": "unavailable"}
    dates = {item["as_of"] for item in accounts}
    if len(dates) != 1:
        return {"status": "unavailable", "reasons": ["account valuation dates differ"],
                "as_of": None, "terminal": None, "xirr": None, "xirr_status": "unavailable"}
    as_of = next(iter(dates))
    terminal = sum((item["terminal"] for item in accounts), Decimal(0))
    flows = [flow for item in accounts for flow in item["external_flows"]]
    try:
        rate = xirr(flows + [(as_of, terminal)])
    except IncompleteHistory as exc:
        return {"status": "unavailable", "reasons": [str(exc)], "as_of": as_of,
                "terminal": terminal, "xirr": None, "xirr_status": "unavailable"}
    income_keys = recorded_income([], None)
    income = {key: sum((item["income"][key] for item in accounts), Decimal(0)) for key in income_keys}
    complete = all(item["status"] == "available" for item in accounts)
    return {"status": "available" if complete else "unavailable",
            "reasons": [] if complete else ["at least one account has an unavailable individual calculation"],
            "as_of": as_of, "terminal": terminal, "xirr": rate, "xirr_status": "available",
            "cash": sum((item["cash"] for item in accounts), Decimal(0)),
            "securities": sum((item["securities"] for item in accounts), Decimal(0)),
            "cost_basis": (sum((item["cost_basis"] for item in accounts), Decimal(0)) if complete else None),
            "realized": (sum((item["realized"] for item in accounts), Decimal(0)) if complete else None),
            "income": income}
