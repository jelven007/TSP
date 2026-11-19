"""Take one private process-paired sample using trusted, read-only argv adapters.

Run on a scheduler; this command does not certify elapsed trading days. Adapters
must read the actual process endpoint, worker progress file or broker socket.
Never import metrics in a new interpreter and present them as live metrics.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from performance.summarize_observability import summarize
from release.common import atomic_json, object_hash


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp requires timezone")
    return parsed.timestamp()


def normalize(value, *, name, now, max_age):
    if "observability" in value:
        age = instant(now) - instant(value["captured_at"])
        if (value["kind"] != name or value["healthy"] is not True
                or not 0 <= age <= max_age):
            raise ValueError("stale, unhealthy or mismatched worker")
        value = value["observability"]
    if value.get("type") == "result":
        value = value["payload"]
    if not value["process"]["instance_id"]:
        raise ValueError("missing process identity")
    return {key: value[key] for key in (
        "process", "runtime_process", "totals", "stages", "public_query_cache",
    ) if key in value}


def stage_pair(before, after, name):
    last = after["stages"][name]
    first = before["stages"].get(name)
    if first is None:
        first = {"calls": 0, "duration_ms": {"sum": 0, "histogram": {
            "bounds_ms": last["duration_ms"]["histogram"]["bounds_ms"],
            "counts": [0] * len(last["duration_ms"]["histogram"]["counts"]),
        }}}
    return tuple({
        "process": snapshot.get("runtime_process", snapshot["process"]),
        "totals": {"requests": metric["calls"], "duration_ms": metric["duration_ms"]},
    } for snapshot, metric in ((before, first), (after, last)))


def window(before, after, *, max_gap):
    gap = instant(after["at"]) - instant(before["at"])
    result = {"started_at": before["at"], "ended_at": after["at"],
              "gap_seconds": gap, "targets": {}}
    for name in before["targets"].keys() | after["targets"].keys():
        try:
            if not 0 < gap <= max_gap:
                raise ValueError("sampling_gap")
            first, last = before["targets"][name], after["targets"][name]
            if first["status"] != "passed" or last["status"] != "passed":
                raise ValueError("missing_sample")
            a, b = first["metrics"], last["metrics"]
            for key in ("process", "runtime_process"):
                if key in a or key in b:
                    if a[key]["instance_id"] != b[key]["instance_id"]:
                        raise ValueError("process_reset")
                    if b[key]["uptime_seconds"] <= a[key]["uptime_seconds"]:
                        raise ValueError("stale_sample")
            if set(a.get("stages", {})) - set(b.get("stages", {})):
                raise ValueError("stage_counters_disappeared")
            row = {"status": "passed", "stages": {
                stage: summarize([stage_pair(a, b, stage)]) for stage in b.get("stages", {})
            }}
            if "totals" in a or "totals" in b:
                row["http"] = summarize([(a, b)])
            result["targets"][name] = row
        except (KeyError, TypeError, ValueError):
            result["targets"][name] = {"status": "discontinuous"}
    return result


def read_adapter(argv):
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv)):
        raise ValueError("adapter requires argv array")
    # No shell, command arguments or private stderr in the evidence.
    with tempfile.TemporaryFile() as output:
        subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL, timeout=30, check=True)
        if output.tell() > 1024 * 1024:
            raise ValueError("observation is too large")
        output.seek(0)
        return json.load(output)


def collect(config, previous=None):
    def now():
        return datetime.now(UTC).isoformat()

    result = {"at": now(), "targets": {}, "business_clock": {"status": "not_configured"}}
    for target in config["targets"]:
        name = target["name"]
        try:
            value = read_adapter(target["argv"])
            if "process_samples" in value:
                values = value["process_samples"]
                if len(values) != target["expected_processes"]:
                    raise ValueError("missing process samples")
                metrics = [normalize(row, name=name, now=now(),
                                     max_age=target.get("max_age_seconds", 30)) for row in values]
                identities = [row["process"]["instance_id"] for row in metrics]
                if len(set(identities)) != len(identities):
                    raise ValueError("duplicate process samples")
                result["targets"].update({
                    f"{name}/{identity}": {"status": "passed", "metrics": row, "at": now()}
                    for identity, row in zip(identities, metrics, strict=True)
                })
            else:
                metrics = normalize(value, name=name, now=now(),
                                    max_age=target.get("max_age_seconds", 30))
                result["targets"][name] = {"status": "passed", "metrics": metrics, "at": now()}
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
            result["targets"][name] = {"status": "failed", "at": now()}
    if config.get("business_clock_argv"):
        try:
            clock = read_adapter(config["business_clock_argv"])
            age = instant(now()) - instant(clock["captured_at"])
            if not 0 <= age <= config["max_gap_seconds"]:
                raise ValueError("stale business clock")
            result["business_clock"] = {key: clock[key] for key in (
                "captured_at", "trading_date", "calendar_source", "quote_at",
                "minute_at", "pool_date", "public_release_id",
            )}
            for key in ("is_trading_day", "pool_dates", "minute_clock_kind"):
                if key in clock:
                    result["business_clock"][key] = clock[key]
            result["business_clock"]["status"] = "passed"
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
            result["business_clock"] = {"status": "failed"}
    result["finished_at"] = now()
    return result, window(previous, result, max_gap=config["max_gap_seconds"]) if previous else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    names = [target["name"] for target in config["targets"]]
    if not names or len(set(names)) != len(names):
        parser.error("nonempty unique targets required")
    gap = config["max_gap_seconds"]
    if not isinstance(gap, (int, float)) or not math.isfinite(gap) or not 1 <= gap <= 3600:
        parser.error("max_gap_seconds must be 1..3600")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.output / "collector.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        latest = args.output / "latest.json"
        previous = json.loads(latest.read_text()) if latest.exists() else None
        fingerprint = object_hash(config)
        if previous and previous["config_sha256"] != fingerprint:
            parser.error("changed configuration requires a new output directory")
        sample, delta = collect(config, previous)
        sample["config_sha256"] = fingerprint
        for name, value in (("samples", sample), ("windows", delta)):
            if value is not None:
                with (args.output / f"{name}.jsonl").open("a") as stream:
                    stream.write(json.dumps(value, allow_nan=False) + "\n")
        atomic_json(latest, sample)
    print(json.dumps({"targets": {k: v["status"] for k, v in sample["targets"].items()},
                      "business_clock": sample["business_clock"]["status"]}))


if __name__ == "__main__":
    main()
