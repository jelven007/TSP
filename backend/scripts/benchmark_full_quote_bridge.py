"""Measure marker-driven full-product quote snapshot work with synthetic frames.

Run from backend:
    uv run --no-sync python -m scripts.benchmark_full_quote_bridge
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import date
from pathlib import Path

import polars as pl

from app.multitenancy.market import encode_frame
from app.multitenancy.quote_bridge import PublicQuoteBridge
from app.tickflow.repository import DataStore, KlineRepository


class SyntheticMarket:
    def __init__(self, snapshot: dict):
        self.value = snapshot
        self.calls = 0

    def snapshot(self) -> dict:
        self.calls += 1
        return self.value

    def close(self) -> None:
        return


class ConnectedMarkers:
    def start(self) -> None:
        return

    def close(self) -> None:
        return

    def status(self) -> dict:
        return {"connected": True}


def _snapshot() -> dict:
    frame = encode_frame(pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2026, 10, 16)],
        "close": [10.0],
    }))
    return {
        "sequence": 1,
        "captured_at": time.time(),
        "status": {
            "symbol_count": 1,
            "quote_age_ms": 0,
            "is_trading_hours": True,
        },
        "frames": {"stock": frame, "etf": frame, "index": frame},
        "indices": encode_frame(pl.DataFrame()),
    }


def _scenario(workers: int, active_workers: int, markers: int, requests_per_marker: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="full-quote-bridge-") as folder:
        resources = []
        for index in range(workers):
            worker_dir = Path(folder) / f"worker-{index}"
            worker_dir.mkdir()
            store = DataStore(worker_dir, read_only=True)
            market = SyntheticMarket(_snapshot())
            bridge = PublicQuoteBridge(KlineRepository(store), market)
            bridge.attach_markers(ConnectedMarkers())
            bridge.update(market.value)
            resources.append((store, market, bridge))

        baseline_fetches = sum(bridge.snapshot_fetches for _, _, bridge in resources)
        baseline_decodes = sum(bridge.frame_decodes for _, _, bridge in resources)
        started = time.perf_counter()
        for _ in range(markers):
            for _, _, bridge in resources:
                bridge.mark_event("quotes_updated")
            for _, _, bridge in resources[:active_workers]:
                for _ in range(requests_per_marker):
                    bridge.ensure_fresh()
        elapsed_ms = (time.perf_counter() - started) * 1000

        fetches = sum(bridge.snapshot_fetches for _, _, bridge in resources) - baseline_fetches
        decodes = sum(bridge.frame_decodes for _, _, bridge in resources) - baseline_decodes
        for store, _, bridge in resources:
            bridge.close()
            store.db.close()
        return {
            "measured": True,
            "workers": workers,
            "active_workers": active_workers,
            "markers": markers,
            "requests_per_marker": requests_per_marker,
            "snapshot_fetches": fetches,
            "frame_decodes": decodes,
            "fetches_per_marker": fetches / markers,
            "decodes_per_marker": decodes / markers,
            "execution_ms": round(elapsed_ms, 3),
        }


def benchmark(workers: int, markers: int, requests_per_marker: int) -> dict:
    frames_per_snapshot = 4
    return {
        "method": {
            "marker_rate_hz": 1,
            "frames_per_snapshot": frames_per_snapshot,
            "startup_refresh_excluded": True,
            "note": "New-path counts execute PublicQuoteBridge; old-path counts are the removed 1 Hz loop model.",
        },
        "old_fixed_poll_model": {
            "measured": False,
            "workers": workers,
            "snapshot_fetches": workers * markers,
            "frame_decodes": workers * markers * frames_per_snapshot,
            "fetches_per_second_at_1hz": workers,
            "decodes_per_second_at_1hz": workers * frames_per_snapshot,
        },
        "new_marker_driven": {
            "idle": _scenario(workers, 0, markers, requests_per_marker),
            "one_active_worker": _scenario(workers, 1, markers, requests_per_marker),
            "all_active_workers": _scenario(workers, workers, markers, requests_per_marker),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--markers", type=int, default=60)
    parser.add_argument("--requests-per-marker", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.workers < 1 or args.markers < 1 or args.requests_per_marker < 1:
        parser.error("workers, markers and requests-per-marker must be positive")
    result = benchmark(args.workers, args.markers, args.requests_per_marker)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
