"""Build a private review of ambiguous overlaps. SPDX-License-Identifier: GPL-3.0-or-later"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from html import escape
import json
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from svoibumagi.alfa import parse_alfa
from svoibumagi.sber import parse_sber
from svoibumagi.source_match import raw_equivalent
from svoibumagi.storage import Storage


def _cell(value: object) -> str:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return escape(str(value))


def main() -> None:
    parser = argparse.ArgumentParser(description="Private overlap review; never auto-approves real imports")
    parser.add_argument("registry", type=Path, help="private/sample-registry.json")
    parser.add_argument("output", type=Path, help="private/overlap-review.html")
    parser.add_argument("--decisions-output", type=Path,
                        help="private/overlap-decisions.json with source-checked duplicate decisions")
    parser.add_argument("--overlap-decisions", type=Path,
                        help="apply source-checked duplicate decisions in the scratch audit")
    parser.add_argument("--summary-decisions", type=Path,
                        help="private/summary-decisions.json with explicit unkeyed-trade decisions")
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text())
    registry_by_sha = {item["sha256"]: item for item in registry["reports"]}
    for item in registry["reports"]:
        with Path(item["path"]).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered report changed; rebuild the private sample registry")
    overlap_decisions = {}
    if args.overlap_decisions:
        supplied = json.loads(args.overlap_decisions.read_text())
        if supplied.get("schema") != 1:
            raise ValueError("Unsupported overlap decision manifest")
        for decision in supplied["decisions"]:
            key = (decision["new_report_sha256"], decision["new_sheet"], decision["new_row"],
                   decision.get("component", ""))
            if key in overlap_decisions:
                raise ValueError("Duplicate overlap decision")
            overlap_decisions[key] = decision
    used_overlap_decisions = set()
    summary_decisions = {}
    if args.summary_decisions:
        supplied = json.loads(args.summary_decisions.read_text())
        if supplied.get("schema") != 1:
            raise ValueError("Unsupported summary decision manifest")
        for decision in supplied["decisions"]:
            key = (decision["report_sha256"], decision["sheet"], decision["row"])
            if key in summary_decisions:
                raise ValueError("Duplicate summary decision")
            summary_decisions[key] = decision
    used_summary_decisions = set()
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for item in registry["reports"]:
        key = item.get("account_key")
        kind = item.get("account_kind")
        if key is None:
            path = Path(item["path"])
            parsed = (parse_sber if item["broker"] == "sber" else parse_alfa)(path)
            key, kind = parsed.account_key, parsed.account_kind
        if key is None:
            raise ValueError("Report without a unique contract identity requires separate review")
        groups[(item["broker"], key, kind or "brokerage")].append(item)

    records: list[dict] = []
    summaries: list[dict] = []
    cross_checks: Counter[tuple[str, str, str]] = Counter()
    cross_differences: list[dict] = []
    source_row_count = 0
    for (broker, account_key, account_kind), reports in sorted(groups.items()):
        group_name = account_key[:12]
        with TemporaryDirectory(prefix="sb-overlap-review-") as temp:
            storage = Storage(Path(temp) / "private")
            account_id = storage.create_account(broker, account_kind, "Scratch account")
            attempt_names: dict[int, str] = {}
            attempt_sha: dict[int, str] = {}
            for item in sorted(reports, key=lambda x: (x["period_start"], x["period_end"], x["sha256"])):
                attempt_id, _ = storage.stage_file(account_id, Path(item["path"]))
                attempt_names[attempt_id] = Path(item["path"]).name
                attempt_sha[attempt_id] = item["sha256"]
                review = storage.review_attempt(attempt_id)
                candidates = {candidate["decision_key"]: candidate for candidate in storage._event_items(review["preview"])}
                raw_rows = {(row["sheet"], row["row"]): row["raw"] for row in review["diagnostic_rows"]}
                decisions = {key: {"action": "control_only" if key.startswith("summary:") else "acknowledge",
                                   "reason": "scratch audit only"}
                             for key in review["required_decisions"]}
                if args.summary_decisions:
                    for summary in review["preview"]["summary_trades"]:
                        loc = summary["location"]
                        manifest_key = (item["sha256"], loc["sheet"], loc["row"])
                        chosen = summary_decisions.get(manifest_key)
                        if chosen is None:
                            raise ValueError("Unkeyed trade has no source-bound decision")
                        digest = hashlib.sha256(json.dumps(summary, sort_keys=True, ensure_ascii=False,
                                                          separators=(",", ":")).encode()).hexdigest()
                        if chosen["event_payload_sha256"] != digest or chosen["action"] not in {"control_only", "promote"}:
                            raise ValueError("Unkeyed trade decision does not match source event")
                        if not str(chosen.get("reason", "")).strip():
                            raise ValueError("Unkeyed trade decision needs a reason")
                        key = f"summary:{loc['sheet']}:{loc['row']}"
                        decisions[key] = {"action": chosen["action"], "reason": chosen["reason"]}
                        used_summary_decisions.add(manifest_key)
                with storage._connect() as con:
                    source_id = con.execute("SELECT source_id FROM import_attempts WHERE id=?", (attempt_id,)).fetchone()[0]
                    used_events = {row[0] for row in con.execute(
                        "SELECT event_id FROM event_observations WHERE source_id=?", (source_id,)
                    )}
                for ambiguous in review["ambiguous_events"]:
                    candidate = candidates[ambiguous["key"]]
                    payload = json.loads(candidate["payload"])
                    manifest_key = (item["sha256"], candidate["sheet"], candidate["row"],
                                    candidate["component"])
                    chosen_overlap = overlap_decisions.get(manifest_key) if args.overlap_decisions else None
                    if args.overlap_decisions and chosen_overlap is None:
                        raise ValueError("Ambiguous event has no source-bound duplicate decision")
                    if chosen_overlap:
                        if (chosen_overlap["action"] != "skip"
                                or chosen_overlap["event_payload_sha256"] != hashlib.sha256(candidate["payload"].encode()).hexdigest()):
                            raise ValueError("Duplicate decision does not match source event")
                        with storage._connect() as con:
                            matched_ids = [event_id for event_id in ambiguous["existing_ids"] if con.execute(
                                "SELECT 1 FROM ledger_events e JOIN source_files s ON s.id=e.source_id "
                                "WHERE e.id=? AND s.sha256=? AND e.sheet=? AND e.row_number=?",
                                (event_id, chosen_overlap["existing_report_sha256"],
                                 chosen_overlap["existing_sheet"], chosen_overlap["existing_row"]),
                            ).fetchone()]
                        if len(matched_ids) != 1 or matched_ids[0] in used_events:
                            raise ValueError("Duplicate decision has no unused matching ledger source")
                        available = matched_ids[0]
                        used_overlap_decisions.add(manifest_key)
                    else:
                        available = next((event_id for event_id in ambiguous["existing_ids"] if event_id not in used_events), None)
                    existing_id = available or ambiguous["existing_ids"][0]
                    with storage._connect() as con:
                        old = con.execute(
                            "SELECT s.original_name,e.sheet,e.row_number,e.payload_json,e.source_id,s.sha256 FROM ledger_events e "
                            "JOIN source_files s ON s.id=e.source_id WHERE e.id=?", (existing_id,)
                        ).fetchone()
                        old_raw_row = con.execute(
                            "SELECT r.raw_json FROM source_rows r JOIN import_attempts a ON a.id=r.attempt_id "
                            "WHERE a.source_id=? AND r.sheet=? AND r.row_number=? "
                            "AND r.classification=? ORDER BY a.id LIMIT 1",
                            (old[4], old[1], old[2], "cash_movement"),
                        ).fetchone()
                    new_raw = raw_rows.get((candidate["sheet"], candidate["row"]), [])
                    old_raw = json.loads(old_raw_row[0]) if old_raw_row else []
                    if chosen_overlap:
                        event_date = payload["date"]
                        new_report = registry_by_sha[item["sha256"]]
                        old_report = registry_by_sha[old[5]]
                        if (not raw_equivalent(new_raw, old_raw)
                                or payload != json.loads(old[3])
                                or not (new_report["period_start"] <= event_date <= new_report["period_end"])
                                or not (old_report["period_start"] <= event_date <= old_report["period_end"])
                                or (len(ambiguous["existing_ids"]) > 1
                                    and (candidate["sheet"], candidate["row"]) != (old[1], old[2]))):
                            raise ValueError("Duplicate decision fails source-row or report-period checks")
                    records.append({
                        "broker": broker, "group": group_name,
                        "new_sha": item["sha256"], "existing_sha": old[5],
                        "new_file": Path(item["path"]).name,
                        "new_location": f"{candidate['sheet']}:{candidate['row']}",
                        "new_sheet": candidate["sheet"], "new_row": candidate["row"],
                        "component": candidate["component"],
                        "new_raw": new_raw,
                        "new_event": payload,
                        "existing_file": old[0], "existing_location": f"{old[1]}:{old[2]}",
                        "existing_sheet": old[1], "existing_row": old[2],
                        "existing_event": json.loads(old[3]), "existing_raw": old_raw,
                        "raw_equal": new_raw == old_raw,
                        "raw_equivalent": raw_equivalent(new_raw, old_raw),
                        "candidate_count": len(ambiguous["existing_ids"]),
                        "reason": ambiguous["reason"],
                        "scratch_action": "skip" if available is not None else "new",
                        "selected_event_id": available,
                    })
                    if ambiguous["reason"].startswith("same broker ID"):
                        raise ValueError("Conflicting broker ID requires adapter review")
                    if available is not None:
                        used_events.add(available)
                        decisions[ambiguous["key"]] = {
                            "action": "skip", "reason": chosen_overlap["reason"] if chosen_overlap else "scratch assumption; manual review required",
                            "existing_event_id": available,
                        }
                    else:
                        decisions[ambiguous["key"]] = {
                            "action": "new", "reason": "scratch one-to-one match unavailable; manual review required",
                        }
                storage.confirm_attempt(attempt_id, decisions)
            with storage._connect() as con:
                source_row_count += con.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0]
            ordered = sorted(storage.committed_reports(account_id),
                             key=lambda report: (report["period_end"], report["attempt_id"]))
            events = storage.list_events(account_id)
            cash_events = [event["payload"] for event in events if event["event_type"] == "cash"]
            trades = [event["payload"] for event in events if event["event_type"] == "trade"]
            for before, after in zip(ordered, ordered[1:]):
                before_end = date.fromisoformat(before["period_end"])
                after_end = date.fromisoformat(after["period_end"])
                if date.fromisoformat(after["period_start"]) > before_end + timedelta(days=1):
                    continue
                if len(before["cash_snapshots"]) == len(after["cash_snapshots"]) == 1:
                    left, right = before["cash_snapshots"][0], after["cash_snapshots"][0]
                    if left["currency"] == right["currency"]:
                        movement = sum((Decimal(event["amount"]) for event in cash_events
                                        if event["currency"] == left["currency"]
                                        and before_end < date.fromisoformat(event["date"]) <= after_end), Decimal(0))
                        difference = Decimal(left["end_amount"]) + movement - Decimal(right["end_amount"])
                        status = "matched" if abs(difference) <= Decimal("0.01") else "difference"
                        cross_checks[(broker, "cash", status)] += 1
                        if status == "difference":
                            cross_differences.append({"broker": broker, "group": group_name, "kind": "cash",
                                                      "key": left["currency"], "before": attempt_names[before["attempt_id"]],
                                                      "after": attempt_names[after["attempt_id"]],
                                                      "difference": str(difference)})
                left_positions: dict[str, Decimal] = defaultdict(Decimal)
                right_positions: dict[str, Decimal] = defaultdict(Decimal)
                for position in before["positions"]:
                    if position["isin"]:
                        left_positions[position["isin"]] += Decimal(position["end_quantity"])
                for position in after["positions"]:
                    if position["isin"]:
                        right_positions[position["isin"]] += Decimal(position["end_quantity"])
                for isin in sorted(left_positions.keys() & right_positions.keys()):
                    movement = sum((Decimal(trade["quantity"]) for trade in trades if trade["isin"] == isin
                                    and before_end < date.fromisoformat(trade["settled_at"]) <= after_end), Decimal(0))
                    difference = left_positions[isin] + movement - right_positions[isin]
                    status = "matched" if difference == 0 else "difference"
                    cross_checks[(broker, "position", status)] += 1
                    if status == "difference":
                        cross_differences.append({"broker": broker, "group": group_name, "kind": "position",
                                                  "key": isin, "before": attempt_names[before["attempt_id"]],
                                                  "after": attempt_names[after["attempt_id"]],
                                                  "difference": str(difference)})
            detailed = [event["payload"] for event in storage.list_events(account_id)
                        if event["event_type"] == "trade" and not event["payload"].get("aggregate_from_report")]
            for report in storage.committed_reports(account_id):
                for aggregate in report.get("summary_trades", []):
                    location = aggregate["location"]
                    manifest_key = (attempt_sha[report["attempt_id"]], location["sheet"], location["row"])
                    action = summary_decisions.get(manifest_key, {}).get("action", "control_only")
                    matching = [trade for trade in detailed
                                if (trade["traded_at"], trade["settled_at"], trade["isin"],
                                    Decimal(trade["quantity"]) > 0)
                                == (aggregate["traded_at"], aggregate["settled_at"], aggregate["isin"],
                                    Decimal(aggregate["quantity"]) > 0)]
                    quantity = sum((Decimal(trade["quantity"]) for trade in matching), Decimal(0))
                    amount = sum((Decimal(trade["amount"]) for trade in matching), Decimal(0))
                    effective_resolved = not any(
                        row["location"] == location and "trade without broker ID" in row["reason"]
                        for row in report["unparsed"]
                    )
                    summaries.append({
                        "broker": broker, "group": group_name,
                        "source": attempt_names[report["attempt_id"]],
                        "location": f"{aggregate['location']['sheet']}:{aggregate['location']['row']}",
                        "isin": aggregate["isin"], "traded_at": aggregate["traded_at"],
                        "settled_at": aggregate["settled_at"],
                        "aggregate_quantity": aggregate["quantity"], "aggregate_amount": aggregate["amount"],
                        "detailed_quantity": quantity, "detailed_amount": amount,
                        "detailed_count": len(matching),
                        "decision": action,
                        "effective_resolved": effective_resolved,
                        "status": "совпало по количеству и сумме" if (quantity == Decimal(aggregate["quantity"])
                                   and amount == Decimal(aggregate["amount"])) else "нет полного покрытия детализацией",
                    })

    if args.overlap_decisions and used_overlap_decisions != set(overlap_decisions):
        raise ValueError("Overlap manifest contains unused decisions")
    counts = Counter(record["broker"] for record in records)
    rows = []
    for number, record in enumerate(records, 1):
        new = f"{record['new_file']} · {record['new_location']}"
        old = f"{record['existing_file']} · {record['existing_location']}"
        fields = (number, record["broker"], record["group"], new, record["new_raw"],
                  record["new_event"], old, record["existing_raw"], record["existing_event"],
                  "да" if record["raw_equal"] else "нет",
                  "да" if record["raw_equivalent"] else "нет",
                  record["candidate_count"], record["reason"],
                  record["scratch_action"], record["selected_event_id"] or "—")
        rows.append("<tr>" + "".join(f"<td>{_cell(value)}</td>" for value in fields) + "</tr>")
    body = "\n".join(rows)
    summary_rows = []
    for number, record in enumerate(summaries, 1):
        fields = (number, record["broker"], record["group"], record["source"], record["location"],
                  record["isin"], record["traded_at"], record["settled_at"],
                  record["aggregate_quantity"], record["aggregate_amount"], record["detailed_quantity"],
                  record["detailed_amount"], record["detailed_count"], record["status"], record["decision"],
                  "да" if record["effective_resolved"] else "нет")
        summary_rows.append("<tr>" + "".join(f"<td>{_cell(value)}</td>" for value in fields) + "</tr>")
    summary_body = "\n".join(summary_rows)
    cross_rows = []
    for (broker, kind, status), count in sorted(cross_checks.items()):
        cross_rows.append("<tr>" + "".join(f"<td>{_cell(value)}</td>" for value in (broker, kind, status, count)) + "</tr>")
    difference_rows = []
    for item in cross_differences:
        difference_rows.append("<tr>" + "".join(f"<td>{_cell(value)}</td>" for value in item.values()) + "</tr>")
    document = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Закрытая сверка спорных строк</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.4rem;vertical-align:top;word-break:break-word}}
td:nth-child(5),td:nth-child(6),td:nth-child(8),td:nth-child(9){{max-width:24rem}}</style>
<h1>Спорные совпадения</h1>
<p>Только для локальной ручной проверки. Группы составлены по идентификатору договора внутри отчёта.
Временный прогон помечал пары как повторы для продолжения анализа; в пользовательскую базу эти решения не записаны.
Совпадение значений само по себе не доказывает повтор.</p>
<p>Сбер: {counts['sber']}; Альфа: {counts['alfa']}; всего: {len(records)}.
Исходные ячейки совпали у {sum(record['raw_equal'] for record in records)} пар; у
{sum(record['raw_equivalent'] for record in records)} пар значения ячеек эквивалентны с учётом формы XLS/XLSX;
у {sum(record['candidate_count'] > 1 for record in records)} строк более одного кандидата.</p>
<table><tr><th>№</th><th>Брокер</th><th>Группа</th><th>Новый источник</th><th>Исходная строка</th>
<th>Новое событие</th><th>Существующий источник</th><th>Его исходная строка</th><th>Существующее событие</th>
<th>Ячейки совпали</th><th>Значения эквивалентны</th><th>Кандидатов</th><th>Причина</th>
<th>Временное действие</th><th>ID пары</th></tr>
{body}</table>
<h2>Межотчётные проверки</h2>
<p>Сравниваются только смежные или перекрывающиеся отчёты и бумаги, присутствующие в обоих итоговых срезах.
Проверки используют временные решения о спорных денежных парах. Сохранено исходных классифицированных строк: {source_row_count}.</p>
<table><tr><th>Брокер</th><th>Контроль</th><th>Статус</th><th>Количество</th></tr>{''.join(cross_rows)}</table>
<table><tr><th>Брокер</th><th>Группа</th><th>Контроль</th><th>Валюта/ISIN</th><th>Предыдущий отчёт</th><th>Следующий отчёт</th><th>Разница</th></tr>{''.join(difference_rows)}</table>
<h2>Сделки без номера в исходном отчёте</h2>
<p>Сопоставление по дате сделки, дате расчёта, ISIN и направлению. Решение из закрытого манифеста
показывается отдельно; агрегат с действием `promote` записан в технический журнал с исходной строкой.
В расчётном срезе снято {sum(item['effective_resolved'] for item in summaries)} из {len(summaries)}
диагностических предупреждений об агрегатах после проверки решения и событий журнала.</p>
<table><tr><th>№</th><th>Брокер</th><th>Группа</th><th>Файл</th><th>Строка</th><th>ISIN</th>
<th>Дата сделки</th><th>Дата расчёта</th><th>Количество в отчёте</th><th>Сумма в отчёте</th>
<th>Количество детальных</th><th>Сумма детальных</th><th>Детальных строк</th><th>Статус</th><th>Решение</th><th>Разрешено в расчёте</th></tr>
{summary_body}</table></html>"""
    if args.summary_decisions and used_summary_decisions != set(summary_decisions):
        raise ValueError("Summary manifest contains unused decisions")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(document)
    os.chmod(args.output, 0o600)
    if args.decisions_output:
        decisions = []
        for record in records:
            date_of_event = record["new_event"]["date"]
            new_report = registry_by_sha[record["new_sha"]]
            old_report = registry_by_sha[record["existing_sha"]]
            in_both_periods = (new_report["period_start"] <= date_of_event <= new_report["period_end"]
                               and old_report["period_start"] <= date_of_event <= old_report["period_end"])
            if (not record["raw_equivalent"] or not in_both_periods or record["scratch_action"] != "skip"
                    or record["new_event"] != record["existing_event"]):
                raise ValueError("A proposed duplicate lacks matching source rows or overlapping dates")
            if (record["candidate_count"] > 1
                    and (record["new_sheet"], record["new_row"]) != (record["existing_sheet"], record["existing_row"])):
                raise ValueError("Repeated equal rows lack a stable same-location pairing")
            payload = json.dumps(record["new_event"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            decisions.append({
                "broker": record["broker"], "account_group_digest": record["group"],
                "new_report_sha256": record["new_sha"], "new_sheet": record["new_sheet"],
                "new_row": record["new_row"], "component": record["component"],
                "existing_report_sha256": record["existing_sha"],
                "existing_sheet": record["existing_sheet"], "existing_row": record["existing_row"],
                "event_payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "action": "skip", "reason": "same contract group, date in both periods, equivalent source cells; one-to-one source row pairing",
            })
        manifest = {"schema": 1, "source_registry_generated_at": registry["generated_at"],
                    "basis": "private source-row comparison; account ownership and history coverage remain separate",
                    "decisions": decisions}
        args.decisions_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.decisions_output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(args.decisions_output, 0o600)
    print(f"Wrote {len(records)} ambiguous pairs, {len(summaries)} unkeyed trade controls "
          f"({sum(item['effective_resolved'] for item in summaries)} resolved in account view), "
          f"{sum(cross_checks.values())} cross-report checks and {len(cross_differences)} differences to {args.output}")


if __name__ == "__main__":
    main()
