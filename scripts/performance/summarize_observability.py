"""Merge per-process HTTP histogram deltas from saved observability snapshots.

Each --pair is the start/end snapshot for one process in the same wall-clock
observation window. This offline tool never fetches URLs or credentials.
"""
from __future__ import annotations

import argparse
import json
import math
from itertools import pairwise
from pathlib import Path


def _histogram(snapshot):
    aggregate = snapshot["totals"]
    histogram = aggregate["duration_ms"]["histogram"]
    bounds, counts = histogram["bounds_ms"], histogram["counts"]
    if (not bounds or any(not math.isfinite(n) or n < 0 for n in bounds)
            or any(a >= b for a, b in pairwise(bounds))
            or len(counts) != len(bounds) + 1
            or any(type(n) is not int or n < 0 for n in counts)
            or sum(counts) != aggregate["requests"]):
        raise ValueError("invalid histogram bounds or counts")
    return bounds, counts


def summarize(pairs, *, population="http"):
    """Return exact count/sum deltas and percentile bucket intervals, not fake P95s."""
    bounds = None
    merged = []
    total_ms = 0.0
    instances = set()
    windows = []
    measurement = None
    for before, after in pairs:
        if population != "http":
            if population not in {"stream", "stream_first_byte"}:
                raise ValueError("unknown measurement population")
            selected = []
            for snapshot in (before, after):
                stream = snapshot["streams"]
                selected.append({
                    **(stream["first_byte"] if population == "stream_first_byte" else stream),
                    "measurement": "sse_first_byte_v1" if population == "stream_first_byte" else "sse_lifetime_v1",
                })
            before, after = selected
        prior_measurement = before.get("measurement", "http_completion_v1")
        next_measurement = after.get("measurement", "http_completion_v1")
        if prior_measurement != next_measurement or measurement not in (None, next_measurement):
            raise ValueError("HTTP measurement populations differ")
        measurement = next_measurement
        first, last = before["process"], after["process"]
        instance = first["instance_id"]
        duration = last["uptime_seconds"] - first["uptime_seconds"]
        if (not instance or instance != last["instance_id"] or instance in instances
                or not math.isfinite(duration) or duration <= 0):
            raise ValueError("reset, duplicate process or invalid observation interval")
        instances.add(instance)
        prior_bounds, prior_counts = _histogram(before)
        next_bounds, next_counts = _histogram(after)
        if prior_bounds != next_bounds or (bounds is not None and bounds != next_bounds):
            raise ValueError("histogram boundaries differ")
        if bounds is None:
            bounds = next_bounds
            merged = [0] * len(next_counts)
        delta = [b - a for a, b in zip(prior_counts, next_counts, strict=True)]
        delta_ms = after["totals"]["duration_ms"]["sum"] - before["totals"]["duration_ms"]["sum"]
        if any(n < 0 for n in delta) or not math.isfinite(delta_ms) or delta_ms < 0:
            raise ValueError("counters decreased within one observation instance")
        merged = [a + b for a, b in zip(merged, delta, strict=True)]
        total_ms += delta_ms
        windows.append(duration)
    if bounds is None:
        raise ValueError("at least one snapshot pair is required")
    requests = sum(merged)

    def percentile_bucket(quantile):
        if not requests:
            return None
        count = 0
        for index, value in enumerate(merged):
            count += value
            if count >= requests * quantile:
                return {
                    "lower": bounds[index - 1] if index else 0,
                    "upper": bounds[index] if index < len(bounds) else None,
                }
        raise ValueError("invalid quantile")

    return {
        "measurement": measurement,
        "processes": len(instances), "window_seconds": windows,
        "requests": requests, "sum_ms": total_ms,
        "average_ms": total_ms / requests if requests else None,
        "histogram": {"bounds_ms": bounds, "counts": merged},
        **{f"p{percentile}_bucket_ms": percentile_bucket(percentile / 100)
           for percentile in (50, 95, 99)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", nargs=2, action="append", required=True, metavar=("BEFORE", "AFTER"))
    parser.add_argument("--population", choices=("http", "stream", "stream_first_byte"), default="http")
    args = parser.parse_args()
    try:
        pairs = [(json.loads(Path(a).read_text()), json.loads(Path(b).read_text())) for a, b in args.pair]
        print(json.dumps(summarize(pairs, population=args.population), indent=2, allow_nan=False))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(1, f"Invalid observation window: {exc}\n")


if __name__ == "__main__":
    main()
