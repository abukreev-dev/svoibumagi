"""Compare parser output to privately transcribed report controls.

The control JSON and real reports must stay in a Git-ignored private directory.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

from svoibumagi.alfa import parse_alfa
from svoibumagi.reconcile import reconcile
from svoibumagi.sber import parse_sber


def _location(item) -> str:
    return f"{item.location.sheet}:{item.location.row}"


def _number(value) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def check(registry_path: Path, controls_path: Path) -> int:
    if not __debug__:
        raise RuntimeError("Manual control validation requires Python assertions")
    registry = json.loads(registry_path.read_text())
    controls = json.loads(controls_path.read_text())
    if controls.get("schema") != 1:
        raise ValueError("Unsupported private control schema")
    by_sha = {item["sha256"]: item for item in registry["reports"]}
    controlled_by_sha = {item["sha256"]: item for item in controls["cases"]}
    for control in controls["cases"]:
        item = by_sha[control["sha256"]]
        path = Path(item["path"])
        with path.open("rb") as stream:
            actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        assert actual_sha == control["sha256"], f"Source file changed: {path.name}"
        equivalence = control.get("equivalent_source")
        if equivalence:
            prior_sha = equivalence["sha256"]
            assert prior_sha != control["sha256"] and prior_sha in controlled_by_sha
            assert not controlled_by_sha[prior_sha].get("equivalent_source"), "Equivalent control must reference a direct control"
            prior_path = Path(by_sha[prior_sha]["path"])
            with prior_path.open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == prior_sha
            current_code = equivalence["current_contract_code"]
            prior_code = equivalence["prior_contract_code"]
            assert current_code and prior_code and current_code != prior_code
            current_html = path.read_text(encoding="utf-8")
            prior_html = prior_path.read_text(encoding="utf-8")
            assert current_html.count(current_code) > 0 and prior_html.count(prior_code) > 0
            assert current_html.replace(current_code, "ACCOUNT_ALIAS") == prior_html.replace(prior_code, "ACCOUNT_ALIAS"), (
                f"Economic source differs from controlled report: {path.name}")
        report = (parse_sber if item["broker"] == "sber" else parse_alfa)(path)
        assert report.broker == control["broker"]
        assert [report.period_start.isoformat(), report.period_end.isoformat()] == control["period"]
        assert Counter((row.isin, _number(row.start_quantity), _number(row.end_quantity)) for row in report.positions) == Counter(
            (isin, _number(values[0]), _number(values[1])) for isin, values in control["positions"].items()
        ), f"Position control mismatch: {path.name}"
        assert Counter((row.currency, _number(row.start_amount), _number(row.end_amount)) for row in report.cash_snapshots) == Counter(
            [("RUB", _number(control["cash"]["start"]), _number(control["cash"]["end"]))]
        ), f"Cash balance control mismatch: {path.name}"
        assert Counter((_location(row), row.isin, _number(row.quantity), _number(row.amount), _number(row.commission),
                        row.traded_at.isoformat(), row.settled_at.isoformat()) for row in report.trades) == Counter(
            (row["location"], row["isin"], _number(row["quantity"]), _number(row["amount"]),
             _number(row["commission"]), row["traded_at"], row["settled_at"]) for row in control["trades"]
        ), f"Trade control mismatch: {path.name}"
        if any("accrued_interest" in row for row in control["trades"]):
            assert Counter((_location(row), _number(row.accrued_interest)) for row in report.trades) == Counter(
                (row["location"], _number(row["accrued_interest"])) for row in control["trades"]
            ), f"Trade accrued-interest control mismatch: {path.name}"
        if "accrued_interest_net" in control:
            accrued_net = sum(((row.accrued_interest or Decimal(0)) * (-1 if row.quantity > 0 else 1)
                               for row in report.trades + report.summary_trades), Decimal(0))
            accrued_net += sum((row.amount for row in report.cash_movements
                                if row.kind == "accrued_interest"), Decimal(0))
            assert _number(accrued_net) == _number(control["accrued_interest_net"]), (
                f"Trade accrued-interest net mismatch: {path.name}")
        assert Counter((_location(row), row.isin, _number(row.quantity), _number(row.amount), _number(row.commission),
                        row.traded_at.isoformat(), row.settled_at.isoformat()) for row in report.summary_trades) == Counter(
            (row["location"], row["isin"], _number(row["quantity"]), _number(row["amount"]),
             _number(row["commission"]), row["traded_at"], row["settled_at"])
            for row in control.get("summary_trades", [])
        ), f"Unkeyed trade control mismatch: {path.name}"
        assert Counter((_location(row), row.kind, _number(row.amount),
                        _number(row.withheld_tax) if row.withheld_tax is not None else None, row.component)
                       for row in report.cash_movements) == Counter(
            (row["location"], row["kind"], _number(row["amount"]),
             _number(row["withheld_tax"]) if row.get("withheld_tax") is not None else None,
             row.get("component", ""))
            for row in control["cash_movements"]
        ), f"Cash movement control mismatch: {path.name}"
        assert Counter((_location(row), row.kind, _number(row.amount)) for row in report.cash_flow_subtotals) == Counter(
            (row["location"], row["kind"], _number(row["amount"]))
            for row in control.get("cash_flow_subtotals", [])
        ), f"Cash flow subtotal mismatch: {path.name}"
        assert sum(row.reason not in {"instrument reference used for ISIN mapping; no ledger event",
                                      "structural Sber row; no ledger event",
                                      "broker subtotal; see source_total checks",
                                      "portfolio overview; see source_total checks",
                                      "Alfa report heading or column labels; no ledger event",
                                      "ISIN continuation used by preceding position; no ledger event",
                                      "Alfa position table footnote; no ledger event",
                                      "Alfa cash movement subtotal; see source_total check",
                                      "Alfa portfolio value subtotal; see source_total checks",
                                      "Alfa tax liability summary; arithmetic check only, no ledger event",
                                      "Alfa tax liability heading; no ledger event",
                                      "Alfa trade table footer note; no ledger event",
                                      "Alfa empty trade sheet heading; no ledger event",
                                      "Alfa period cash and portfolio summary; see source_total checks",
                                      "Alfa cash section label; no ledger event",
                                      "Alfa market access period metadata; no security operation",
                                      "Alfa empty non-trading sheet heading; no security operation"}
                   for row in report.excluded) == control["excluded_rows"]
        assert Counter(f"{row.kind}:{row.status}" for row in reconcile(report) if row.kind != "source_total") == Counter(control["checks"])
    return len(controls["cases"])


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: check_manual_controls.py REGISTRY PRIVATE_CONTROLS")
    count = check(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Checked {count} private report control cases")
