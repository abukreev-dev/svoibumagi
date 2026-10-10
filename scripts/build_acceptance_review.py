"""Render a private, source-bound queue for broker report acceptance.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import Counter
from html import escape
import hashlib
import json
import os
from pathlib import Path
import sys

if __package__:
    from .check_manual_controls import check as check_manual_controls
else:
    from check_manual_controls import check as check_manual_controls


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell(value: object) -> str:
    return escape(str(value))


def build(registry_path: Path, controls_path: Path, overlap_path: Path,
          summary_path: Path) -> tuple[str, dict[str, int]]:
    registry = _read(registry_path)
    controls = _read(controls_path)
    overlaps = _read(overlap_path)
    summaries = _read(summary_path)
    if (registry.get("schema") != 2 or controls.get("schema") != 1
            or overlaps.get("schema") != 1 or summaries.get("schema") != 1):
        raise ValueError("Unsupported private audit schema")
    source_files = registry["reports"]
    by_sha = {item["sha256"]: item for item in source_files}
    copies = Counter(item["sha256"] for item in source_files)
    reports = list(by_sha.values())
    for item in source_files:
        with Path(item["path"]).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Registered report changed")
    control_by_sha = {item["sha256"]: item for item in controls["cases"]}
    controlled = set(control_by_sha)
    if len(controlled) != len(controls["cases"]) or controlled - by_sha.keys():
        raise ValueError("Manual control references an unknown or duplicate report")
    # A control only counts as reviewed when its source values still match.
    if check_manual_controls(registry_path, controls_path) != len(controlled):
        raise ValueError("Manual control count changed during acceptance review")
    overlap_counts: Counter[str] = Counter()
    for decision in overlaps["decisions"]:
        if decision["new_report_sha256"] not in by_sha or decision["existing_report_sha256"] not in by_sha:
            raise ValueError("Overlap decision references an unknown report")
        overlap_counts[decision["new_report_sha256"]] += 1
    summary_counts: Counter[str] = Counter()
    summary_actions: dict[tuple[str, str, int], str] = {}
    for decision in summaries["decisions"]:
        if decision["report_sha256"] not in by_sha:
            raise ValueError("Summary decision references an unknown report")
        key = (decision["report_sha256"], decision["sheet"], decision["row"])
        if key in summary_actions or decision["action"] not in {"control_only", "promote"}:
            raise ValueError("Duplicate or invalid summary decision")
        if not any((item["sheet"], item["row"]) == key[1:] for item in by_sha[key[0]]["unparsed"]):
            raise ValueError("Summary decision does not reference a diagnostic source row")
        summary_actions[key] = decision["action"]
        summary_counts[decision["report_sha256"]] += 1

    diagnostic_rows = [(item["sha256"], row["sheet"], row["row"])
                       for item in reports for row in item["unparsed"]]
    totals = {"source_files": len(source_files), "unique_reports": len(reports),
              "duplicate_files": len(source_files) - len(reports), "economic_controls": len(controlled),
              "direct_controls": sum(not item.get("equivalent_source") for item in controls["cases"]),
              "equivalent_source_controls": sum(bool(item.get("equivalent_source")) for item in controls["cases"]),
              "uncontrolled": len(reports) - len(controlled),
              "reports_with_unparsed": sum(bool(item["unparsed"]) for item in reports),
              "diagnostic_rows_with_private_decision": sum(key in summary_actions for key in diagnostic_rows),
              "diagnostic_rows_without_private_decision": sum(key not in summary_actions for key in diagnostic_rows),
              "reports_with_unavailable": sum(any(key.endswith(":unavailable") for key in item["checks"])
                                                for item in reports),
              "overlap_decisions": sum(overlap_counts.values()),
              "summary_decisions": sum(summary_counts.values())}
    ordered = sorted(reports, key=lambda item: (
        not bool(item["unparsed"]),
        not any(key.endswith(":unavailable") for key in item["checks"]),
        item["sha256"] in controlled,
        item["broker"], item["period_start"], item["sha256"],
    ))
    body = []
    for number, item in enumerate(ordered, 1):
        sha = item["sha256"]
        diagnostics = "; ".join(
            f"{row['sheet']}:{row['row']} — {row['reason']}"
            + (f" [решение во временной БД: {summary_actions[(sha, row['sheet'], row['row'])]}]"
               if (sha, row['sheet'], row['row']) in summary_actions else " [ожидает подтверждения]")
            for row in item["unparsed"]
        ) or "—"
        unavailable = "; ".join(f"{key}: {value}" for key, value in item["checks"].items()
                                if key.endswith(":unavailable")) or "—"
        fields = (number, item["broker"], item.get("account_kind") or "brokerage",
                  item["account_key"][:12] if item.get("account_key") else "—",
                  Path(item["path"]).name, copies[sha], f"{item['period_start']} — {item['period_end']}",
                  item["trades"], item["summary_trades"], item["positions"], item["cash_snapshots"],
                  ("по совпадению HTML" if control_by_sha[sha].get("equivalent_source")
                   else "по исходным ячейкам") if sha in controlled else "нет",
                  diagnostics, unavailable,
                  overlap_counts[sha], summary_counts[sha], sha[:12])
        body.append("<tr>" + "".join(f"<td>{_cell(field)}</td>" for field in fields) + "</tr>")
    counts = "; ".join(f"{_cell(key)}: {_cell(value)}" for key, value in totals.items())
    html = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Закрытая очередь приёмки отчётов</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.35rem;vertical-align:top;word-break:break-word}}
thead{{position:sticky;top:0;background:#fff}}input{{padding:.5rem;width:min(34rem,100%)}}</style>
<h1>Очередь приёмки отчётов</h1>
<p>Локальный отчёт по реестру и закрытым манифестам. Файл содержит имена источников и сведения о счетах;
не публиковать. Порядок: диагностические строки, недоступные проверки, отсутствие экономического контроля.
Решения по дублям и агрегатам применялись только во временной технической базе.</p>
<p>{counts}</p>
<p>Контроль по совпадению HTML подтверждает значения только после нормализации кода договора.
Он не устанавливает принадлежность договора. Диагностика, недоступные проверки и разрывы периодов
по-прежнему ограничивают расчёты. Разрывы дат см. в <code>coverage-review.html</code>.</p>
<label>Поиск по таблице <input id="search" type="search" placeholder="Файл, группа, причина"></label>
<div style="overflow:auto;max-height:75vh"><table id="reports"><thead><tr>
<th>№</th><th>Брокер</th><th>Тип</th><th>Группа</th><th>Файл</th><th>Копий</th><th>Период</th>
<th>Сделок</th><th>Без ID</th><th>Позиций</th><th>Денежных срезов</th><th>Экономический контроль</th>
<th>Диагностика</th><th>Недоступные проверки</th><th>Спорные пары</th><th>Решения по сделкам</th><th>SHA</th>
</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<script>const input=document.getElementById('search');input.addEventListener('input',()=>{{
 const q=input.value.toLocaleLowerCase();for(const row of document.querySelectorAll('#reports tbody tr'))
 row.hidden=!row.textContent.toLocaleLowerCase().includes(q);
}});</script></html>"""
    return html, totals


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit("usage: build_acceptance_review.py REGISTRY MANUAL_CONTROLS OVERLAP_DECISIONS SUMMARY_DECISIONS OUTPUT")
    html, totals = build(*(Path(value) for value in sys.argv[1:5]))
    output = Path(sys.argv[5])
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(html)
    os.chmod(output, 0o600)
    print(f"Wrote {totals['unique_reports']} unique reports from {totals['source_files']} files; "
          f"{totals['uncontrolled']} lack an economic control; "
          f"{totals['reports_with_unparsed']} have diagnostics; {totals['reports_with_unavailable']} have unavailable checks")


if __name__ == "__main__":
    main()
