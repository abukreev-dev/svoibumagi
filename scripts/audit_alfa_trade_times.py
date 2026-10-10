"""Compare Alfa trade execution times with raw XLS/XLSX cells.

Reads local private reports, prints aggregate counts only.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time
import hashlib
import json
from pathlib import Path
import re
import sys

from svoibumagi.alfa import _sheet_rows, parse_alfa


RAW_TIME = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")
RAW_DATE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")


def audit(registry_path: Path) -> dict:
    registry = json.loads(registry_path.read_text())
    if registry.get("schema") != 2:
        raise ValueError("Unsupported private registry schema")
    reports = {item["sha256"]: item for item in registry["reports"] if item["broker"] == "alfa"}
    trades_checked = 0
    inversions = 0
    for digest, item in reports.items():
        path = Path(item["path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Private source changed")
        rows, _ = _sheet_rows(path)
        parsed = parse_alfa(path)
        grouped = defaultdict(list)
        for trade in parsed.trades:
            raw = str(rows[trade.location.sheet][trade.location.row - 1][7])
            times = RAW_TIME.findall(raw)
            dates = RAW_DATE.findall(raw)
            if len(times) != 1 or len(dates) != 1:
                raise ValueError("Alfa trade row lacks one execution date and time")
            actual_time = time(*(int(part) for part in times[0])).isoformat()
            actual_date = datetime.strptime(dates[0], "%d.%m.%Y").date()
            if actual_time != trade.execution_time or actual_date != trade.traded_at:
                raise ValueError("Parsed Alfa execution timestamp differs from source row")
            grouped[(trade.settled_at, trade.traded_at, trade.isin)].append(
                (trade.location.row, actual_time, trade.quantity))
            trades_checked += 1
        for group in grouped.values():
            ordered = sorted(group)
            if (any(qty > 0 for _, _, qty in ordered) and any(qty < 0 for _, _, qty in ordered)
                    and any(left[1] > right[1] for left, right in zip(ordered, ordered[1:]))):
                inversions += 1
    return {"reports": len(reports), "trades_checked": trades_checked,
            "mixed_buy_sale_groups_with_row_time_inversion": inversions}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: audit_alfa_trade_times.py PRIVATE_REGISTRY")
    print(json.dumps(audit(Path(sys.argv[1])), sort_keys=True))
