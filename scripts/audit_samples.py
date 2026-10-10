"""Create a private, source-preserving broker sample registry.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from svoibumagi.alfa import UnsupportedReport, parse_alfa
from svoibumagi.reconcile import reconcile
from svoibumagi.sber import parse_sber


def audit_directory(directory: Path) -> dict:
    records = []
    skipped = 0
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in {".html", ".htm", ".xls", ".xlsx"}:
            continue
        try:
            report = (parse_sber if suffix in {".html", ".htm"} else parse_alfa)(path)
        except UnsupportedReport:
            skipped += 1
            continue
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        checks = reconcile(report)
        records.append({
            "path": str(path.resolve()),
            "sha256": digest,
            "size": path.stat().st_size,
            "broker": report.broker,
            "account_key": report.account_key,
            "account_number": report.account_number,
            "account_kind": report.account_kind,
            "variant": report.variant,
            "period_start": report.period_start.isoformat(),
            "period_end": report.period_end.isoformat(),
            "trades": len(report.trades),
            "summary_trades": len(report.summary_trades),
            "cash_kinds": dict(sorted(Counter(item.kind for item in report.cash_movements).items())),
            "positions": len(report.positions),
            "cash_snapshots": len(report.cash_snapshots),
            "excluded_rows": len(report.excluded),
            "unparsed": [{"sheet": item.location.sheet, "row": item.location.row, "reason": item.reason}
                         for item in report.unparsed],
            "checks": dict(sorted(Counter((item.kind, item.status) for item in checks).items())),
        })
    # JSON object keys must be strings, not tuples.
    for record in records:
        record["checks"] = {f"{kind}:{status}": count for (kind, status), count in record["checks"].items()}
    return {"schema": 2, "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_directory": str(directory.resolve()), "recognized": len(records),
            "other_candidate_files": skipped, "reports": records}


def write_private_registry(registry: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, prefix=".registry-", suffix=".tmp",
                                     delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        json.dump(registry, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit local broker reports into a private JSON registry")
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error("directory does not exist")
    registry = audit_directory(args.directory)
    write_private_registry(registry, args.output)
    print(f"Recognized {registry['recognized']} reports; registry stored at {args.output}")


if __name__ == "__main__":
    main()
