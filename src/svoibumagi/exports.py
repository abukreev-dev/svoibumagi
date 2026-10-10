"""Safe CSV exports. SPDX-License-Identifier: GPL-3.0-or-later"""

import csv
from datetime import date
from decimal import Decimal
from io import StringIO
import unicodedata


def _text(value) -> str:
    text = "" if value is None else str(value)
    index = 0
    while index < len(text) and (text[index].isspace() or unicodedata.category(text[index]) in {"Cc", "Cf"}):
        index += 1
    prefix = text[:index]
    initial = unicodedata.normalize("NFKC", text[index:index + 1])
    if any(char in "\t\r\n\x00" for char in prefix) or (initial and initial[0] in "=+-@"):
        return "'" + text
    return text


def rub_text(value: Decimal) -> str:
    """Round a final RUB amount for display, leaving accounting values unchanged."""
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("Non-finite RUB amount")
    return format(amount, ".2f")


def operations_csv(events: list[dict], account_numbers: dict[int, str | None] | None = None) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["id", "account_id", "type", "date", "settlement_date", "isin", "name", "quantity", "amount", "commission", "accrued_interest", "currency", "source_file", "source_sheet", "source_row", "broker_event_id", "source_kind", "paired_event_id", "source_sha256", "instrument_id", "source_type", "manual_reason", "withheld_tax_rub", "principal_fraction", "linked_cash_event_id", "peer_account_id", "source_row_status", "execution_time", "account_number"])
    for event in events:
        payload = event["payload"]
        writer.writerow([
            event["id"], event["account_id"], event["event_type"], payload.get("traded_at") or payload.get("date"),
            payload.get("settled_at"), _text(payload.get("isin")), _text(payload.get("name") or payload.get("kind")),
            payload.get("quantity"), payload.get("amount"), payload.get("commission"), payload.get("accrued_interest"), _text(payload.get("currency")),
            _text(event["original_name"]), _text(event["sheet"]), event["row_number"],
            _text(payload.get("broker_id")), _text(payload.get("source_kind")), payload.get("paired_event_id"),
            _text(event.get("sha256")), event.get("instrument_id"), _text(event.get("source_type", "broker")),
            _text(event.get("source_reason", "")), payload.get("withheld_tax"),
            payload.get("principal_fraction"), _text(payload.get("cash_event_id")), payload.get("peer_account_id"),
            ("manual" if event.get("source_type") == "manual" else
             "matched" if event.get("source_row_verified") is True else
             "missing" if event.get("source_row_verified") is False else "unknown"),
            _text(event.get("execution_time")), _text((account_numbers or {}).get(event["account_id"])),
        ])
    return stream.getvalue().encode("utf-8-sig")


def positions_csv(snapshots: list[dict]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["account_id", "account", "report_date", "instrument", "isin", "quantity", "status", "account_number"])
    for snapshot in snapshots:
        status = "incomplete" if (snapshot["unparsed"] or not snapshot["checks"]
                                  or any(check["status"] != "matched" for check in snapshot["checks"])) else "report_matched"
        for position in snapshot["positions"]:
            writer.writerow([snapshot["account"]["id"], _text(snapshot["account"]["name"]), snapshot["period_end"],
                             _text(position["name"]), _text(position["isin"]), position["end_quantity"], status,
                             _text(snapshot["account"].get("account_number"))])
    return stream.getvalue().encode("utf-8-sig")


def valuations_csv(estimate: dict) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["account_id", "account", "report_date", "isin", "instrument", "quantity",
                     "price_rub", "price_date", "price_source", "value_rub", "status", "method", "valuation_date",
                     "account_number", "report_check_status", "position_date_status", "price_status"])
    for row in estimate["positions"]:
        quote = row["price"]
        if row.get("price_status") == "currency_conversion_required":
            status = "currency_conversion_required"
        elif row.get("price_status") == "stale":
            status = "stale_price"
        elif row["value"] is None:
            status = "unavailable"
        elif row["account_incomplete"]:
            status = "incomplete_report"
        elif estimate.get("as_of") and row["report_date"] != estimate["as_of"]:
            status = "stale_position"
        elif row["quantity"] == 0:
            status = "zero_position"
        elif row.get("price_status") == "broker_report":
            status = "broker_report_value"
        else:
            status = "priced"
        writer.writerow([row["account"]["id"], _text(row["account"]["name"]), row["report_date"],
                         _text(row["isin"]), _text(row["name"] + (f" ({row['currency']})"
                                                               if row.get("currency", "RUB") != "RUB" else "")), row["quantity"],
                         quote["amount"] if quote else "",
                         quote["as_of"] if quote else row["report_date"] if row.get("price_status") == "broker_report" else "",
                         _text(quote["source"]) if quote else "broker_report" if row.get("price_status") == "broker_report" else "",
                         row["value"], status,
                         "broker_report_end_value" if row.get("price_status") == "broker_report" else "report_quantity_times_rub_price",
                         estimate.get("as_of") or "",
                         _text(row["account"].get("account_number")),
                         "incomplete" if row["account_incomplete"] else "matched",
                         "stale" if estimate.get("as_of") and row["report_date"] != estimate["as_of"] else "current",
                         row.get("price_status", "unknown")])
    for account in estimate.get("unreported_accounts", []):
        writer.writerow([account["id"], _text(account["name"]), "", "", "", "", "", "", "", "",
                         "unavailable_no_report", "", estimate.get("as_of") or "",
                         _text(account.get("account_number")), "unavailable", "unavailable", "unavailable"])
    return stream.getvalue().encode("utf-8-sig")


def metrics_csv(accounts: list[dict], combined: dict) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    income_keys = ("dividend_gross_known", "dividend_tax_unknown", "dividend_with_tax_unpaired",
                   "dividend_net", "coupon", "tax_withheld", "accrued_interest_net",
                   "coupon_net_of_accrued_interest", "fees_paid_cash")
    writer.writerow(["scope", "account_id", "account", "as_of", "status", "cash_rub", "securities_rub",
                     "terminal_rub", "open_fifo_cost_rub", "realized_fifo_rub", "xirr_annual",
                     "inferred_zero_positions", "reasons", "method", *[f"{key}_rub" for key in income_keys],
                     "in_kind_transfer_flow_rub", "xirr_status", "account_number"])
    for metric in accounts:
        writer.writerow(["account", metric["account"]["id"], _text(metric["account"]["name"]),
                         metric["as_of"], metric["status"], metric["cash"] if metric.get("cash") is not None else "",
                         metric["securities"] if metric.get("securities") is not None else "",
                         metric["terminal"] if metric.get("terminal") is not None else "",
                         rub_text(metric["cost_basis"]) if metric["status"] == "available" else "",
                         rub_text(metric["realized"]) if metric["status"] == "available" else "",
                         metric["xirr"] if metric.get("xirr") is not None else "",
                         metric.get("inferred_zero_count", 0), _text("; ".join(metric["reasons"])),
                         "FIFO_by_settlement_date;XIRR_account_boundary_cash_and_in_kind_flows_plus_terminal",
                         *[metric.get("income", {}).get(key, "") for key in income_keys],
                         ("" if metric.get("unpriced_in_kind_transfers") else
                          sum((amount for _, amount in metric.get("in_kind_transfer_flows", [])), 0)),
                         metric.get("xirr_status", "available" if metric.get("xirr") is not None else "unavailable"),
                         _text(metric["account"].get("account_number"))])
    writer.writerow(["combined", "", "", combined["as_of"], combined["status"],
                     combined.get("cash") if combined.get("cash") is not None else "",
                     combined.get("securities") if combined.get("securities") is not None else "",
                     combined.get("terminal") if combined.get("terminal") is not None else "",
                     rub_text(combined["cost_basis"]) if combined["status"] == "available" else "",
                     rub_text(combined["realized"]) if combined["status"] == "available" else "",
                     combined["xirr"] if combined.get("xirr") is not None else "", "",
                     _text("; ".join(combined["reasons"])), "XIRR_external_flows_plus_terminal",
                     *[(sum((item["income"][key] for item in accounts), 0) if combined.get("xirr") is not None else "")
                       for key in income_keys], "",
                     combined.get("xirr_status", "available" if combined.get("xirr") is not None else "unavailable"), ""])
    return stream.getvalue().encode("utf-8-sig")


def gaps_csv(metrics: list[dict], confirmations: dict[int, list[dict]]) -> bytes:
    """Export uncovered dates separately from owner statements about idle dates."""
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["account_id", "account_number", "account_number_status", "broker", "kind",
                     "record_type", "start_date", "end_date", "days", "status", "reason",
                     "confirmed_at", "revoked_at"])
    for metric in metrics:
        account = metric["account"]
        number_status = ("ambiguous" if account.get("account_number_conflict") else
                         "known" if account.get("account_number") else "unknown")
        prefix = [account["id"], _text(account.get("account_number")), number_status,
                  _text(account["broker"]), _text(account["kind"])]
        confirmable = {(gap["start"], gap["end"]) for gap in metric.get("confirmable_gaps", [])}
        for gap in metric.get("gaps", []):
            writer.writerow([*prefix, "uncovered_gap", gap["start"], gap["end"], gap["days"],
                             ("needs_owner_confirmation" if (gap["start"], gap["end"]) in confirmable
                              else "needs_source_or_review"), "", "", ""])
        for period in confirmations.get(account["id"], []):
            start, end = period["start_date"], period["end_date"]
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            writer.writerow([*prefix, "owner_statement", start, end, days,
                             "revoked" if period.get("revoked_at") else "owner_confirmed",
                             _text(period.get("reason")), _text(period.get("confirmed_at")),
                             _text(period.get("revoked_at"))])
    return stream.getvalue().encode("utf-8-sig")
