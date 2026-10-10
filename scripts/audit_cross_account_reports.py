"""Find exact economic report duplicates assigned to different contracts.

Real paths and account hashes are written only to an ignored private file.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from svoibumagi.alfa import parse_alfa
from svoibumagi.sber import parse_sber


ECONOMIC_FIELDS = ("trades", "summary_trades", "positions", "cash_snapshots",
                   "cash_movements", "cash_flow_subtotals", "unparsed", "excluded")


def audit(registry_path: Path) -> dict:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("schema") != 2:
        raise ValueError("Unsupported private registry schema")
    unique = {item["sha256"]: item for item in registry["reports"]}
    groups: dict[tuple[str, str, str, str, str], list[dict]] = defaultdict(list)
    identities: dict[tuple[str, str], list[dict]] = defaultdict(list)
    payloads: dict[str, str] = {}
    for digest, item in unique.items():
        path = Path(item["path"])
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                raise ValueError("Registered report changed")
        report = (parse_sber if item["broker"] == "sber" else parse_alfa)(path)
        if report.account_key != item["account_key"] or report.account_kind != item["account_kind"]:
            raise ValueError("Registered contract identity changed")
        economic = {field: [asdict(row) for row in getattr(report, field)] for field in ECONOMIC_FIELDS}
        canonical = json.dumps(economic, ensure_ascii=False, sort_keys=True, default=str,
                               separators=(",", ":"))
        economic_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if economic_sha in payloads and payloads[economic_sha] != canonical:
            raise ValueError("Economic signature collision")
        payloads[economic_sha] = canonical
        key = (item["broker"], item["account_kind"], item["period_start"],
               item["period_end"], economic_sha)
        reference = {"sha256": digest, "path": str(path), "account_key": item["account_key"],
                     "account_kind": item["account_kind"], "period": [item["period_start"], item["period_end"]]}
        groups[key].append(reference)
        identities[(item["broker"], item["account_key"])].append(reference)
    matches = []
    for (broker, kind, start, end, economic_sha), reports in sorted(groups.items()):
        if len({item["account_key"] for item in reports}) > 1:
            matches.append({"broker": broker, "account_kind": kind, "period": [start, end],
                            "economic_sha256": economic_sha, "reports": reports,
                            "decision": "verify_contract_identity_before_combining_accounts"})
    kind_conflicts = []
    for (broker, account_key), reports in sorted(identities.items()):
        if len({item["account_kind"] for item in reports}) > 1:
            kind_conflicts.append({"broker": broker, "account_key": account_key, "reports": reports,
                                   "decision": "verify_account_type_and_contract_identity"})
    return {"schema": 2, "reports_scanned": len(unique), "cross_account_matches": matches,
            "identity_kind_conflicts": kind_conflicts}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: audit_cross_account_reports.py PRIVATE_REGISTRY PRIVATE_OUTPUT")
    result = audit(Path(sys.argv[1]))
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, prefix=".account-audit-", suffix=".tmp",
                                     delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Scanned {result['reports_scanned']} unique reports; "
          f"found {len(result['cross_account_matches'])} cross-contract economic matches and "
          f"{len(result['identity_kind_conflicts'])} contract-type conflicts")


if __name__ == "__main__":
    main()
