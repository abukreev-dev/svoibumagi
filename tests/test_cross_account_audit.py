"""Synthetic contract identity audit controls. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import date
from decimal import Decimal
import hashlib
import json

from scripts import audit_cross_account_reports
from svoibumagi.reports import CashMovement, Location, ParsedReport


def test_audit_detects_same_code_with_conflicting_account_types_and_duplicate_iis(tmp_path, monkeypatch):
    specifications = [
        ("brokerage", "same-code", "1"),
        ("iis", "same-code", "2"),
        ("iis", "other-code", "2"),
    ]
    items = []
    reports = {}
    for index, (kind, key, amount) in enumerate(specifications):
        path = tmp_path / f"synthetic-{index}.html"
        path.write_text(f"synthetic-{index}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        report = ParsedReport("sber", "synthetic", date(2025, 1, 1), date(2025, 1, 31),
                              account_key=key, account_kind=kind)
        report.cash_movements.append(CashMovement(Location("cash", 1), date(2025, 1, 15),
                                                  "deposit", Decimal(amount), "RUB"))
        reports[path] = report
        items.append({"path": str(path), "sha256": digest, "broker": "sber", "account_key": key,
                      "account_kind": kind, "period_start": "2025-01-01", "period_end": "2025-01-31"})
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema": 2, "reports": items}))
    monkeypatch.setattr(audit_cross_account_reports, "parse_sber", lambda path: reports[path])

    result = audit_cross_account_reports.audit(registry)
    assert result["reports_scanned"] == 3
    assert len(result["cross_account_matches"]) == 1
    assert len(result["identity_kind_conflicts"]) == 1
    assert {item["account_kind"] for item in result["identity_kind_conflicts"][0]["reports"]} == {
        "brokerage", "iis"
    }
