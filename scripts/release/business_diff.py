"""Explain business hash changes through exact audit chains; never write databases."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


def time_value(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps require timezone")
    return parsed


def compare(before: dict, after: dict, audit: list[dict]) -> dict:
    if not before.get("scope") or before["scope"] != after.get("scope"):
        raise ValueError("snapshot scope mismatch")
    if before.get("transaction") != "repeatable-read" or after.get("transaction") != "repeatable-read":
        raise ValueError("snapshot transaction is not repeatable-read")
    start, end = time_value(before["captured_at"]), time_value(after["captured_at"])
    if end < start:
        raise ValueError("snapshot time order")
    for snapshot in (before, after):
        for resource, digest in snapshot["rows"].items():
            if not resource or resource == "*" or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid resource hash")
    changes = []
    for resource in sorted(before["rows"].keys() | after["rows"].keys()):
        initial, final = before["rows"].get(resource), after["rows"].get(resource)
        if initial == final:
            continue
        cursor, chain = initial, []
        events = [event for event in audit if event.get("resource") == resource]
        valid = True
        last_time = start
        for event in events:
            try:
                when = time_value(event["at"])
                if (not last_time <= when <= end or not event.get("actor_id")
                        or not event.get("request_id") or not event.get("action")
                        or event.get("before") != cursor):
                    valid = False
                    break
                cursor = event["after"]
                if cursor is not None and not re.fullmatch(r"[0-9a-f]{64}", cursor):
                    valid = False
                    break
                chain.append({"request_id": event["request_id"], "action": event["action"],
                              "at": event["at"]})
                last_time = when
            except (ValueError, KeyError, TypeError):
                valid = False
                break
        changes.append({
            "resource": resource, "before": initial, "after": final,
            "kind": "added" if initial is None else "deleted" if final is None else "modified",
            "explained": bool(events) and valid and cursor == final,
            "audit_chain": chain,
        })
    return {"status": "passed" if all(row["explained"] for row in changes) else "blocked",
            "scope": before["scope"], "changes": changes, "database_writes": False,
            "limits": ["hash audit must be collected with the business transaction",
                       "existing request/action-only logs cannot certify row hashes or operator identity"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "after", "audit", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()),
                     json.loads(args.audit.read_text()))
    # Output is controlled evidence, not a public audit export.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from release.common import atomic_json
    atomic_json(args.output, result)
    print(f"Business diff: {result['status']}; {len(result['changes'])} changed resources")
    if result["status"] != "passed":
        parser.exit(1)


if __name__ == "__main__":
    main()
