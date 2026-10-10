"""Exercise the private web import journey on two manually controlled real samples.

The scratch database is deleted after the run. No source paths or amounts are printed.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from svoibumagi.auth import hash_password
from svoibumagi.storage import Storage
from svoibumagi.web import create_app


def smoke(registry_path: Path, controls_path: Path) -> dict[str, int]:
    registry = {item["sha256"]: item for item in json.loads(registry_path.read_text())["reports"]}
    cases = json.loads(controls_path.read_text())["cases"]
    sber = next((case for case in cases if case["broker"] == "sber"
                 and len(case["trades"]) >= 10
                 and any(row["kind"] == "tax_withheld" for row in case["cash_movements"])), None)
    alfa = next((case for case in cases if case["broker"] == "alfa" and len(case["trades"]) >= 10), None)
    if sber is None or alfa is None:
        raise ValueError("Manual controls need one Sber tax report and one Alfa report with ten trades")
    chosen = [sber, alfa]
    counts = {}
    with tempfile.TemporaryDirectory(prefix="web-sample-smoke-", dir=registry_path.parent) as directory:
        root = Path(directory)
        app = create_app(root, hash_password("temporary-sample-audit-password"), secure_cookie=False)
        with TestClient(app, follow_redirects=False) as client:
            client.get("/login")
            login_csrf = client.cookies.get("sb_login_csrf")
            response = client.post("/login", data={"csrf": login_csrf,
                                                    "password": "temporary-sample-audit-password"})
            if response.status_code != 303:
                raise ValueError("Scratch login failed")
            store = Storage(root)
            csrf = store.get_session_csrf(client.cookies.get("sb_session"))
            if not csrf:
                raise ValueError("Scratch session is missing")
            for case in chosen:
                source = registry[case["sha256"]]
                broker = case["broker"]
                account_kind = source["account_kind"] or "brokerage"
                response = client.post("/accounts", data={"csrf": csrf, "name": "Scratch " + broker,
                                                          "broker": broker, "kind": account_kind})
                if response.status_code != 303:
                    raise ValueError(f"{broker} scratch account creation failed")
                account_id = next(item["id"] for item in store.list_accounts() if item["broker"] == broker)
                path = Path(source["path"])
                with path.open("rb") as stream:
                    response = client.post("/imports", data={"csrf": csrf, "account_id": str(account_id)},
                                           files={"report": (path.name, stream, "application/octet-stream")})
                if response.status_code != 303:
                    raise ValueError(f"{broker} upload failed")
                url = response.headers["location"]
                if client.get(url).status_code != 200:
                    raise ValueError(f"{broker} preview failed")
                attempt_id = int(url.rsplit("/", 1)[-1])
                review = store.review_attempt(attempt_id)
                if review["ambiguous_events"] or review["preview"]["summary_trades"]:
                    raise ValueError(f"{broker} sample needs a separate source decision")
                decisions = {"csrf": csrf}
                for key in review["required_decisions"]:
                    decisions[f"reason::{key}"] = "Temporary manually controlled source audit"
                if client.post(url + "/confirm", data=decisions).status_code != 303:
                    raise ValueError(f"{broker} confirmation failed")
                events = store.list_events(account_id)
                expected = len(case["trades"]) + len(case["cash_movements"])
                if len(events) != expected:
                    raise ValueError(f"{broker} event count differs from manual control")
                if events:
                    downloaded = client.get(f"/operations/{events[0]['id']}/source")
                    if (downloaded.status_code != 200
                            or hashlib.sha256(downloaded.content).hexdigest() != case["sha256"]):
                        raise ValueError(f"{broker} source download differs")
                counts[broker] = len(events)
            for endpoint in ("/export/operations.csv", "/export/positions.csv"):
                if client.get(endpoint).status_code != 200:
                    raise ValueError(f"Scratch export failed: {endpoint}")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    parser.add_argument("manual_controls", type=Path)
    args = parser.parse_args()
    result = smoke(args.registry, args.manual_controls)
    print("Private web smoke passed: " + ", ".join(f"{broker} {count} events" for broker, count in result.items()))
