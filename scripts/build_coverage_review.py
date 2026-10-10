"""Private report-period coverage audit. SPDX-License-Identifier: GPL-3.0-or-later"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re

from bs4 import BeautifulSoup

from svoibumagi.alfa import _sheet_rows, parse_alfa
from svoibumagi.reports import account_key
from svoibumagi.sber import CONTRACT, parse_sber


ALFA_AGREEMENT = re.compile(r"№\s*(\d+)\s+от\s+\d{2}\.\d{2}\.\d{4}")


def _source_account_number(item: dict) -> str:
    """Return the source's local account number, never the file name or digest."""
    path = Path(item["path"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("Registered source changed")
    if item["broker"] == "sber":
        text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        identities = {(match.group("code").upper(), "iis" if match.group("iis") else "brokerage")
                      for match in CONTRACT.finditer(text)}
        if len(identities) != 1:
            raise ValueError("Sber source has no unique account number")
        number, kind = next(iter(identities))
        if kind != item.get("account_kind"):
            raise ValueError("Sber source kind differs from registry")
        reference = number
    elif item["broker"] == "alfa":
        rows, _ = _sheet_rows(path)
        agreements = {match.group() for row in rows[" Движение ДС"][:8] for value in row
                      if (match := ALFA_AGREEMENT.fullmatch(str(value).strip()))}
        if len(agreements) != 1:
            raise ValueError("Alfa source has no unique agreement number")
        reference = next(iter(agreements))
        number = ALFA_AGREEMENT.fullmatch(reference).group(1)
    else:
        raise ValueError("Unknown broker in coverage review")
    if account_key(item["broker"], reference) != item["account_key"]:
        raise ValueError("Source account number differs from registry identity")
    return number


def _merge_periods(reports: list[dict]) -> tuple[list[tuple[date, date]], list[tuple[date, date]]]:
    merged: list[tuple[date, date]] = []
    for report in sorted(reports, key=lambda item: (item["period_start"], item["period_end"])):
        start = date.fromisoformat(report["period_start"])
        end = date.fromisoformat(report["period_end"])
        if end < start:
            raise ValueError("Report period ends before it starts")
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    gaps = [(left[1] + timedelta(days=1), right[0] - timedelta(days=1))
            for left, right in zip(merged, merged[1:])]
    return merged, gaps


def _boundary_values(parsed, *, opening: bool) -> tuple[tuple, tuple] | None:
    if not parsed.cash_snapshots:
        return None
    positions: dict[str, Decimal] = defaultdict(Decimal)
    cash: dict[str, Decimal] = defaultdict(Decimal)
    for row in parsed.positions:
        positions[row.isin] += row.start_quantity if opening else row.end_quantity
    for row in parsed.cash_snapshots:
        cash[row.currency] += row.start_amount if opening else row.end_amount
    # Brokers may print or omit a zero-quantity instrument at either boundary.
    return (tuple(sorted((isin, quantity) for isin, quantity in positions.items() if quantity != 0)),
            tuple(sorted((currency, amount) for currency, amount in cash.items() if amount != 0)))


def _boundary_comparison(left: list[dict], right: list[dict], parse) -> tuple[str, str]:
    left_values = {_boundary_values(parse(item), opening=False) for item in left}
    right_values = {_boundary_values(parse(item), opening=True) for item in right}
    if len(left_values) != 1 or len(right_values) != 1 or None in left_values | right_values:
        return "недоступно", "недоступно"
    left_positions, left_cash = next(iter(left_values))
    right_positions, right_cash = next(iter(right_values))
    return ("совпали" if left_positions == right_positions else "отличаются",
            "совпали" if left_cash == right_cash else "отличаются")


def _gap_dated_rows(items: list[dict], start: date, end: date, parse) -> str:
    """Describe parsed rows dated inside a missing period in adjacent reports only."""
    reports = [parse(item) for item in items]
    trade = any(start <= row.settled_at <= end
                for report in reports for row in report.trades + report.summary_trades)
    cash = any(start <= row.date <= end for report in reports for row in report.cash_movements)
    return f"сделки: {'есть' if trade else 'нет'}; деньги: {'есть' if cash else 'нет'}"


def _sber_pending_rows(parsed, html: bytes, start: date, end: date) -> list[tuple[str, Decimal, Decimal]]:
    """Read planned settlements from raw trade rows excluded at report end."""
    tables = BeautifulSoup(html, "html.parser").find_all("table")
    cells = lambda row: [cell.get_text(" ", strip=True).replace("\u00a0", " ")
                         for cell in row.find_all(["td", "th"], recursive=False)]
    code_to_isin = {}
    for table in tables:
        trs = table.find_all("tr")
        header = cells(trs[0]) if trs else []
        if header and header[0] == "Наименование" and "ISIN ценной бумаги" in header:
            for tr in trs[2:]:
                row = cells(tr)
                if len(row) >= 3 and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", row[2]):
                    code_to_isin[row[1]] = row[2]
    number = lambda value: Decimal(value.replace(" ", "").replace(",", ".")) if value.strip() else Decimal(0)
    pending = []
    for excluded in parsed.excluded:
        if not excluded.reason.startswith("not executed at report end:"):
            continue
        table_index = int(excluded.location.sheet.removeprefix("table:"))
        row = cells(tables[table_index].find_all("tr")[excluded.location.row - 1])
        if len(row) != 16:
            raise ValueError("Sber pending trade row changed since parsing")
        settled = datetime.strptime(row[1], "%d.%m.%Y").date()
        if not start <= settled <= end:
            continue
        isin = row[4] if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", row[4]) else code_to_isin.get(row[4])
        if not isin or row[6] not in {"Покупка", "Продажа"}:
            raise ValueError("Sber pending trade identity is unavailable")
        sign = 1 if row[6] == "Покупка" else -1
        quantity = sign * number(row[7])
        cash = -sign * (number(row[9]) + number(row[10])) - number(row[11]) - number(row[12])
        pending.append((isin, quantity, cash))
    return pending


def _sber_pending_boundary(left: list[dict], right: list[dict], start: date, end: date, parse) -> tuple[int | str, str]:
    if len(left) != 1 or len(right) != 1:
        return "—", "неоднозначные соседние файлы"
    path = Path(left[0]["path"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != left[0]["sha256"]:
        raise ValueError("Registered Sber source changed")
    earlier, later = parse(left[0]), parse(right[0])
    before, after = _boundary_values(earlier, opening=False), _boundary_values(later, opening=True)
    if before is None or after is None:
        return "—", "нет граничного остатка"
    pending = _sber_pending_rows(earlier, raw, start, end)
    if not pending:
        return 0, "нет кандидатов"
    old_positions, old_cash = map(dict, before)
    new_positions, new_cash = map(dict, after)
    position_delta = {key: new_positions.get(key, 0) - old_positions.get(key, 0)
                      for key in old_positions.keys() | new_positions.keys()}
    cash_delta = {key: new_cash.get(key, 0) - old_cash.get(key, 0)
                  for key in old_cash.keys() | new_cash.keys()}
    pending_positions: dict[str, Decimal] = defaultdict(Decimal)
    for isin, quantity, _ in pending:
        pending_positions[isin] += quantity
    same_positions = {key: value for key, value in pending_positions.items() if value} == \
        {key: value for key, value in position_delta.items() if value}
    pending_cash = sum((amount for _, _, amount in pending), Decimal(0))
    same_cash = ({"RUB": pending_cash} if pending_cash else {}) == \
        {key: value for key, value in cash_delta.items() if value}
    if same_positions and same_cash:
        status = "позиции и деньги"
    elif same_positions:
        status = "только позиции"
    elif same_cash:
        status = "только деньги"
    else:
        status = "не совпадает"
    return len(pending), status


def main() -> None:
    parser = argparse.ArgumentParser(description="Private report coverage review")
    parser.add_argument("registry", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text())
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for report in registry["reports"]:
        key = report.get("account_key")
        if not key:
            raise ValueError("Registry has a report without contract identity; rerun the sample audit")
        groups[(report["broker"], key, report.get("account_kind") or "unknown")].append(report)

    rows = []
    gap_rows = []
    boundary_counts = Counter()
    parsed_cache = {}

    def parse(item: dict):
        digest = item["sha256"]
        if digest not in parsed_cache:
            path = Path(item["path"])
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError(f"Source file changed since registry: {path.name}")
            parser = parse_sber if item["broker"] == "sber" else parse_alfa
            parsed_cache[digest] = parser(path)
        return parsed_cache[digest]

    for (broker, key, kind), reports in sorted(groups.items()):
        merged, gaps = _merge_periods(reports)
        earliest_start = merged[0][0]
        earliest_files = [item for item in reports if item["period_start"] == earliest_start.isoformat()]
        opening_zero = True
        for item in earliest_files:
            parsed = parse(item)
            if not parsed.cash_snapshots or any(snapshot.start_amount != Decimal(0) for snapshot in parsed.cash_snapshots):
                opening_zero = False
            if any(position.start_quantity != Decimal(0) for position in parsed.positions):
                opening_zero = False
        categories = Counter()
        for item in reports:
            if item["unparsed"]:
                categories["unparsed"] += 1
            if any(check.endswith(":unavailable") for check in item["checks"]):
                categories["unavailable"] += 1
            categories["summary_trades"] += item.get("summary_trades", 0)
        numbers = {_source_account_number(item) for item in reports}
        if len(numbers) != 1:
            raise ValueError("One account group has multiple source account numbers")
        label = next(iter(numbers))
        values = (broker, kind, label, len(reports), merged[0][0], merged[-1][1],
                  len(merged), len(gaps), "да" if opening_zero else "нет",
                  categories["unparsed"], categories["unavailable"], categories["summary_trades"])
        rows.append("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in values) + "</tr>")
        for start, end in gaps:
            left_end = start - timedelta(days=1)
            right_start = end + timedelta(days=1)
            left = [item for item in reports if item["period_end"] == left_end.isoformat()]
            right = [item for item in reports if item["period_start"] == right_start.isoformat()]
            positions, cash = _boundary_comparison(left, right, parse)
            left_rows = _gap_dated_rows(left, start, end, parse)
            right_rows = _gap_dated_rows(right, start, end, parse)
            pending_count, pending_match = (_sber_pending_boundary(left, right, start, end, parse)
                                            if broker == "sber" else ("—", "не применяется"))
            boundary_counts[(positions, cash)] += 1
            gap_rows.append("<tr>" + "".join(f"<td>{escape(str(value))}</td>"
                                                 for value in (broker, kind, label, start, end,
                                                               (end - start).days + 1, positions, cash,
                                                               left_rows, right_rows, pending_count,
                                                               pending_match)) + "</tr>")
    document = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Закрытая карта периодов отчётов</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.45rem;text-align:left}}</style>
<h1>Покрытие отчётами</h1>
<p>Группировка по договору и типу счёта из содержимого файла. Нулевой остаток в начале самого раннего
предоставленного отчёта не подтверждает дату открытия счёта. Разрывы периодов требуют отчёта за пропущенные дни
или другого независимого подтверждения отсутствия операций. Сравнение остатков на границах разрыва показывает
только изменение между двумя датами: совпадение не доказывает отсутствия промежуточных операций. Результат не является приёмкой импорта.</p>
<p>Строки с датой внутри разрыва ищутся только в соседних файлах: сделки по дате расчёта, денежные движения по дате
движения. «Нет» означает отсутствие прочитанной строки в этих файлах, а не отсутствие операции у брокера.</p>
<p>Отложенные сделки Сбера берутся отдельно из исходных строк предыдущего файла со статусом «не исполнена на конец
отчёта» и плановой датой расчёта внутри разрыва. Совпадение их количеств и расчётных денег с границами не
подтверждает фактическое исполнение и не закрывает разрыв истории.</p>
<table><tr><th>Брокер</th><th>Тип</th><th>Номер договора/счёта</th><th>Файлов</th><th>Первое начало</th><th>Последний конец</th>
<th>Непрерывных отрезков</th><th>Разрывов</th><th>Начало нулевое</th><th>Файлов с неизвестными строками</th>
<th>Файлов с недоступными проверками</th><th>Сделок без ID</th></tr>{''.join(rows)}</table>
<h2>Пропущенные интервалы</h2>
<table><tr><th>Брокер</th><th>Тип</th><th>Номер договора/счёта</th><th>С</th><th>По</th><th>Дней</th>
<th>Позиции на границах</th><th>Деньги на границах</th><th>Строки предыдущего файла в разрыве</th>
<th>Строки следующего файла в разрыве</th><th>Отложенные сделки предыдущего файла</th>
<th>Совпадение отложенных с разницей границ</th></tr>
{''.join(gap_rows)}</table></html>"""
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(document)
    os.chmod(args.output, 0o600)
    print(f"Wrote {len(groups)} account groups and {len(gap_rows)} gaps to {args.output}; "
          f"boundary balances: {dict(sorted(boundary_counts.items()))}")


if __name__ == "__main__":
    main()
