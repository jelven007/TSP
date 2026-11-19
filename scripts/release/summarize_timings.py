"""Summarize saved release receipts; never run plans or certify deployment.

Pass one latest receipt per environment/plan for the same release. Pipeline span
includes pauses. New receipts also time input validation, probes and postchecks;
older receipts may omit them. Unattributed time is not automatically idle time.
Hosts must have aligned clocks.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.common import identity
from release.pipeline import PHASES


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("receipt timestamps require timezone offsets")
    return parsed.timestamp()


def seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid elapsed seconds")
    return value


def union_seconds(intervals):
    total = 0.0
    right = None
    for start, end in sorted(intervals):
        total += max(0, end - (max(start, right) if right is not None else start))
        right = max(end, right) if right is not None else end
    return total


def summarize(receipts):
    if not receipts:
        raise ValueError("at least one receipt required")
    seen = set()
    release = None
    spans = []
    active = []
    phases = {}
    reconciliations = []
    evidence = []
    incomplete = 0
    passed = 0
    for record in receipts:
        identity(record["version"], record["revision"])
        current = (record["version"], record["revision"])
        key = (record["environment"], record["plan_sha256"])
        if (record["format"] != 1 or not all(key) or key in seen
                or (release is not None and current != release)):
            raise ValueError("duplicate or incompatible release receipts")
        seen.add(key)
        release = current
        start, end = instant(record["started_at"]), instant(record["ended_at"])
        if end <= start:
            raise ValueError("invalid pipeline interval")
        spans.append((start, end))
        passed += record["status"] == "passed"

        def interval(first, last, start=start, end=end):
            if first < start or last > end or last < first:
                raise ValueError("step interval outside pipeline span")
            return first, last

        for row in record.get("evidence_checks", []):
            if "ended_at" not in row:
                incomplete += 1
                continue
            seconds(row["elapsed_seconds"])
            bounds = interval(instant(row["started_at"]), instant(row["ended_at"]))
            evidence.append(bounds)
            active.append(bounds)

        for row in record["steps"].values():
            for attempt in [*row.get("prior_attempts", []), row]:
                phase = attempt["phase"]
                if phase not in PHASES:
                    raise ValueError("unknown release phase")
                bucket = phases.setdefault(phase, {
                    "attempts": 0, "failed_attempts": 0,
                    "summed_elapsed_seconds": 0.0, "intervals": [],
                })
                bucket["attempts"] += 1
                bucket["failed_attempts"] += attempt["status"] == "failed"
                if "ended_at" in attempt and "elapsed_seconds" in attempt:
                    bounds = interval(instant(attempt["started_at"]), instant(attempt["ended_at"]))
                    bucket["summed_elapsed_seconds"] += seconds(attempt["elapsed_seconds"])
                    bucket["intervals"].append(bounds)
                    active.append(bounds)
                else:
                    incomplete += 1
                if attempt.get("reconciled"):
                    duration = seconds(attempt["reconcile_elapsed_seconds"])
                    finish = instant(attempt["reconciled_at"])
                    bounds = interval(finish - duration, finish)
                    active.append(bounds)
                    reconciliations.append(bounds)
    span = max(end for _, end in spans) - min(start for start, _ in spans)
    active_seconds = union_seconds(active)
    return {
        "schema_version": 1, "version": release[0], "revision": release[1],
        "receipt_count": len(receipts), "passed_receipts": passed,
        "pipeline_span_seconds": round(span, 6),
        "recorded_step_active_seconds": round(active_seconds, 6),
        "unattributed_seconds": round(span - active_seconds, 6),
        "reconciliation_seconds": round(union_seconds(reconciliations), 6),
        "evidence_seconds": round(union_seconds(evidence), 6),
        "incomplete_attempts": incomplete,
        "phases": {
            phase: {**{key: value for key, value in bucket.items() if key != "intervals"},
                    "active_seconds": round(union_seconds(bucket["intervals"]), 6)}
            for phase, bucket in phases.items()
        },
        "deployment_verified": False,
        "limits": [
            "Cross-host wall-clock alignment must be verified separately.",
            "Old receipts may omit input validation, probes and resume checks.",
            "Preparation covers validated pipeline entry; work before invoking it is not timed.",
            "Phase active times overlap across hosts; do not add them to infer wall time.",
            "No argv, command output, private paths or environment names are copied.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        result = summarize([json.loads(path.read_text()) for path in args.receipt])
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(1, f"Invalid release timing evidence ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
