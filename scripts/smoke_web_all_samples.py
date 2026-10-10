"""Exercise every privately controlled report through the web import flow.

Each contract group uses a disposable database. Decisions are source-bound;
the run does not approve account ownership or missing reporting periods.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import hashlib
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from fastapi.testclient import TestClient

from scripts.check_manual_controls import check as check_controls
from scripts.audit_source_provenance import audit as audit_source_provenance
from svoibumagi.auth import hash_password
from svoibumagi.backup import create_backup, restore_backup
from svoibumagi.source_match import raw_equivalent
from svoibumagi.storage import Storage
from svoibumagi.web import create_app


def _manifest(path: Path, expected: int) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema") != expected:
        raise ValueError("Unsupported private decision manifest")
    return data


def smoke(registry_path: Path, controls_path: Path, overlap_path: Path,
          summary_path: Path,
          group_hook: Callable[[Storage, int, list[dict]], None] | None = None) -> dict[str, int]:
    registry = _manifest(registry_path, 2)
    controls = _manifest(controls_path, 1)
    overlaps = _manifest(overlap_path, 1)
    summaries = _manifest(summary_path, 1)
    check_controls(registry_path, controls_path)
    by_sha = {item["sha256"]: item for item in registry["reports"]}
    control_by_sha = {item["sha256"]: item for item in controls["cases"]}
    if len(by_sha) != len(controls["cases"]):
        raise ValueError("Unique reports and manual controls differ")
    overlap_by_key = {}
    for item in overlaps["decisions"]:
        key = (item["new_report_sha256"], item["new_sheet"], item["new_row"], item["component"])
        if key in overlap_by_key:
            raise ValueError("Duplicate overlap manifest key")
        overlap_by_key[key] = item
    summary_by_key = {}
    for item in summaries["decisions"]:
        key = (item["report_sha256"], item["sheet"], item["row"])
        if key in summary_by_key:
            raise ValueError("Duplicate summary manifest key")
        summary_by_key[key] = item

    groups = defaultdict(list)
    for item in by_sha.values():
        if not item.get("account_key"):
            raise ValueError("Report has no account identity")
        groups[(item["broker"], item["account_key"], item.get("account_kind") or "brokerage")].append(item)
    used_overlaps, used_summaries = set(), set()
    totals = {"reports": 0, "groups": len(groups), "observations": 0,
              "restored_groups": 0,
              "overlap_decisions": 0, "summary_decisions": 0}
    password = "temporary-private-web-audit-password"
    for (broker, _, account_kind), reports in sorted(groups.items()):
        with TemporaryDirectory(prefix="web-all-samples-", dir=registry_path.parent) as directory:
            root = Path(directory)
            app = create_app(root, hash_password(password), secure_cookie=False)
            with TestClient(app, follow_redirects=False) as client:
                client.get("/login")
                response = client.post("/login", data={"csrf": client.cookies.get("sb_login_csrf"),
                                                       "password": password})
                if response.status_code != 303:
                    raise ValueError("Scratch login failed")
                storage = Storage(root)
                csrf = storage.get_session_csrf(client.cookies.get("sb_session"))
                if not csrf:
                    raise ValueError("Scratch session is missing")
                response = client.post("/accounts", data={"csrf": csrf, "name": "Scratch account",
                                                          "broker": broker, "kind": account_kind})
                if response.status_code != 303:
                    raise ValueError("Scratch account creation failed")
                account_id = storage.list_accounts()[0]["id"]
                source_numbers: set[str] = set()
                for source in sorted(reports, key=lambda item: (item["period_start"],
                                                                 item["period_end"], item["sha256"])):
                    path = Path(source["path"])
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                            raise ValueError("Private source changed")
                    with path.open("rb") as stream:
                        response = client.post("/imports", data={"csrf": csrf, "account_id": str(account_id)},
                                               files={"report": (path.name, stream, "application/octet-stream")})
                    if response.status_code != 303:
                        raise ValueError("Scratch upload failed")
                    url = response.headers["location"]
                    if client.get(url).status_code != 200:
                        raise ValueError("Scratch preview failed")
                    attempt_id = int(url.rsplit("/", 1)[-1])
                    review = storage.review_attempt(attempt_id)
                    preview = review["preview"]
                    if preview["account_key"] != source["account_key"]:
                        raise ValueError("Preview contract identity differs from registry")
                    if not preview.get("account_number"):
                        raise ValueError("Source account number is missing from preview")
                    source_numbers.add(preview["account_number"])
                    candidates = {item["decision_key"]: item for item in storage._event_items(preview)}
                    raw_rows = {(row["sheet"], row["row"]): row["raw"] for row in review["diagnostic_rows"]}
                    form = {"csrf": csrf}
                    for key in review["required_decisions"]:
                        form[f"reason::{key}"] = "Scratch web audit; unresolved status remains visible"
                    for trade in preview["summary_trades"]:
                        loc = trade["location"]
                        manifest_key = (source["sha256"], loc["sheet"], loc["row"])
                        decision = summary_by_key.get(manifest_key)
                        digest = hashlib.sha256(json.dumps(trade, sort_keys=True, ensure_ascii=False,
                                                          separators=(",", ":")).encode()).hexdigest()
                        if (decision is None or decision["event_payload_sha256"] != digest
                                or decision["action"] not in {"control_only", "promote"}
                                or not str(decision.get("reason", "")).strip()):
                            raise ValueError("Unkeyed trade lacks a verified source decision")
                        key = f"summary:{loc['sheet']}:{loc['row']}"
                        form[f"action::{key}"] = decision["action"]
                        form[f"reason::{key}"] = decision["reason"]
                        used_summaries.add(manifest_key)
                        totals["summary_decisions"] += 1
                    for ambiguous in review["ambiguous_events"]:
                        if ambiguous.get("blocking"):
                            raise ValueError("Cross-account conflict in isolated contract group")
                        candidate = candidates[ambiguous["key"]]
                        manifest_key = (source["sha256"], candidate["sheet"], candidate["row"],
                                        candidate["component"])
                        decision = overlap_by_key.get(manifest_key)
                        if (decision is None or decision["action"] != "skip"
                                or decision["event_payload_sha256"] != hashlib.sha256(
                                    candidate["payload"].encode()).hexdigest()
                                or not str(decision.get("reason", "")).strip()):
                            raise ValueError("Ambiguous event lacks a verified source decision")
                        current_raw = raw_rows.get((candidate["sheet"], candidate["row"]))
                        if current_raw is None:
                            raise ValueError("Ambiguous source cells are unavailable")
                        with storage._connect() as con:
                            matches = []
                            for event_id in ambiguous["existing_ids"]:
                                old = con.execute(
                                    "SELECT s.sha256,e.sheet,e.row_number,e.payload_json,e.source_id "
                                    "FROM ledger_events e JOIN source_files s ON s.id=e.source_id WHERE e.id=?",
                                    (event_id,),
                                ).fetchone()
                                if (old is None or (old[0], old[1], old[2]) != (
                                        decision["existing_report_sha256"], decision["existing_sheet"],
                                        decision["existing_row"])):
                                    continue
                                old_raw = con.execute(
                                    "SELECT r.raw_json FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id "
                                    "WHERE a.source_id=? AND r.sheet=? AND r.row_number=? "
                                    "AND r.classification='cash_movement' ORDER BY a.id LIMIT 1",
                                    (old[4], old[1], old[2]),
                                ).fetchone()
                                if old_raw and raw_equivalent(current_raw, json.loads(old_raw[0])):
                                    matches.append((event_id, json.loads(old[3])))
                        event = json.loads(candidate["payload"])
                        day = event["date"]
                        previous = by_sha[decision["existing_report_sha256"]]
                        if (len(matches) != 1 or matches[0][1] != event
                                or not (source["period_start"] <= day <= source["period_end"])
                                or not (previous["period_start"] <= day <= previous["period_end"])
                                or (len(ambiguous["existing_ids"]) > 1
                                    and (candidate["sheet"], candidate["row"]) != (
                                        decision["existing_sheet"], decision["existing_row"]))):
                            raise ValueError("Duplicate decision fails source, payload or period checks")
                        form[f"action::{ambiguous['key']}"] = "skip"
                        form[f"reason::{ambiguous['key']}"] = decision["reason"]
                        form[f"existing::{ambiguous['key']}"] = str(matches[0][0])
                        used_overlaps.add(manifest_key)
                        totals["overlap_decisions"] += 1
                    if client.post(url + "/confirm", data=form).status_code != 303:
                        raise ValueError("Scratch web confirmation failed")
                    with storage._connect() as con:
                        source_id = con.execute("SELECT source_id FROM import_attempts WHERE id=?",
                                                (attempt_id,)).fetchone()[0]
                        observations = con.execute(
                            "SELECT COUNT(*) FROM event_observations WHERE source_id=?", (source_id,)
                        ).fetchone()[0]
                        first_event = con.execute(
                            "SELECT o.event_id,s.sha256 FROM event_observations o "
                            "JOIN ledger_events e ON e.id=o.event_id "
                            "JOIN source_files s ON s.id=e.source_id "
                            "WHERE o.source_id=? ORDER BY o.event_id LIMIT 1",
                            (source_id,),
                        ).fetchone()
                    expected = len(preview["trades"]) + len(preview["cash_movements"]) + sum(
                        form.get(f"action::summary:{item['location']['sheet']}:{item['location']['row']}") == "promote"
                        for item in preview["summary_trades"])
                    if observations != expected:
                        raise ValueError("Scratch observations differ from preview events")
                    if first_event:
                        downloaded = client.get(f"/operations/{first_event[0]}/source")
                        if (downloaded.status_code != 200
                                or hashlib.sha256(downloaded.content).hexdigest() != first_event[1]):
                            raise ValueError("Scratch source download differs from ledger source")
                    totals["reports"] += 1
                    totals["observations"] += observations
                def csv_rows(endpoint: str) -> list[dict[str, str]]:
                    response = client.get(endpoint)
                    if response.status_code != 200:
                        raise ValueError("Scratch CSV export failed")
                    return list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"))))

                account = storage.list_accounts()[0]
                if (len(source_numbers) != 1 or account["account_number"] not in source_numbers
                        or account["account_number_conflict"]):
                    raise ValueError("Committed source account numbers disagree")
                source_number = account["account_number"]

                operation_rows = csv_rows("/export/operations.csv")
                if (len(operation_rows) != len(storage.list_events(account_id))
                        or any(row["source_sha256"] not in {item["sha256"] for item in reports}
                               or row["source_row_status"] != "matched"
                               or row["account_number"] != source_number for row in operation_rows)):
                    raise ValueError("Operations CSV differs from ledger or source registry")
                latest = storage.latest_snapshots()[0]
                with storage._connect() as con:
                    latest_sha = con.execute(
                        "SELECT s.sha256 FROM import_attempts a JOIN source_files s ON s.id=a.source_id "
                        "WHERE a.id=?", (latest["attempt_id"],),
                    ).fetchone()[0]
                control = control_by_sha[latest_sha]
                positions = csv_rows("/export/positions.csv")
                actual_positions = Counter((row["isin"], Decimal(row["quantity"])) for row in positions)
                expected_positions = Counter((isin, Decimal(values[1])) for isin, values in control["positions"].items())
                if (actual_positions != expected_positions
                        or any(row["report_date"] != control["period"][1]
                               or row["account_number"] != source_number for row in positions)):
                    raise ValueError("Positions CSV differs from latest report control")
                valuations = csv_rows("/export/valuations.csv")
                if (Counter((row["isin"], Decimal(row["quantity"])) for row in valuations) != expected_positions
                        or any(row["account_number"] != source_number for row in valuations)):
                    raise ValueError("Valuations CSV quantities differ from latest report control")
                metrics = csv_rows("/export/metrics.csv")
                account_metrics = [row for row in metrics if row["scope"] == "account"]
                if (len(account_metrics) != 1 or account_metrics[0]["account_number"] != source_number
                        or not account_metrics[0]["cash_rub"]
                        or Decimal(account_metrics[0]["cash_rub"]) != Decimal(control["cash"]["end"])):
                    raise ValueError("Metrics CSV cash differs from latest broker control")
                if audit_source_provenance(storage.db_path)["status"] != "matched":
                    raise ValueError("Committed ledger has an event or observation without a source row")
                if group_hook is not None:
                    group_hook(storage, account_id, reports)
                archive = Path(directory) / "scratch-backup.zip"
                create_backup(storage, archive)
                restored = restore_backup(archive, Path(directory) / "restored")
                if (len(restored.committed_reports(account_id)) != len(reports)
                        or len(restored.list_events(account_id)) != len(storage.list_events(account_id))):
                    raise ValueError("Restored report or ledger count differs")
                with restored._connect() as con:
                    restored_shas = {row[0] for row in con.execute("SELECT sha256 FROM source_files")}
                if restored_shas != {item["sha256"] for item in reports}:
                    raise ValueError("Restored source SHA set differs")
                if restored.list_inactive_periods(account_id) != storage.list_inactive_periods(account_id):
                    raise ValueError("Restored owner confirmations differ")
                if restored.list_accounts()[0]["account_number"] != source_number:
                    raise ValueError("Restored source account number differs")
                restored_snapshot = restored.latest_snapshots()[0]
                if (restored_snapshot["period_end"] != control["period"][1]
                        or Counter((row["isin"], Decimal(row["end_quantity"]))
                                   for row in restored_snapshot["positions"]) != expected_positions):
                    raise ValueError("Restored final positions differ from broker control")
                totals["restored_groups"] += 1
    if used_overlaps != set(overlap_by_key) or used_summaries != set(summary_by_key):
        raise ValueError("Some private decisions were not exercised")
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    parser.add_argument("manual_controls", type=Path)
    parser.add_argument("overlap_decisions", type=Path)
    parser.add_argument("summary_decisions", type=Path)
    args = parser.parse_args()
    result = smoke(args.registry, args.manual_controls, args.overlap_decisions, args.summary_decisions)
    print("Private full web smoke passed: " + ", ".join(f"{key}={value}" for key, value in result.items()))
