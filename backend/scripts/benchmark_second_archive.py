"""Offline synthetic archive benchmark; never connects to a market data source.

Run from backend: .venv/bin/python -m scripts.benchmark_second_archive
All shards live in a temporary directory and are removed when the run exits.
"""
from __future__ import annotations

import argparse
import json
import random
import resource
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from app.backtest.second_replay import SecondQuoteReplay, session_windows
from app.services.second_quote_archive import SecondQuoteArchive


def benchmark(symbol_count: int, rounds: int) -> dict:
    rng = random.Random(20261011)
    day = date(2026, 1, 5)
    start = session_windows(day)[0][0]
    symbols = [f"{600000 + i:06d}.SH" for i in range(symbol_count)]
    prices = [round(rng.uniform(2, 500), 2) for _ in symbols]
    volumes = [float(rng.randint(1000, 1_000_000)) for _ in symbols]
    amounts = [price * volume * 100 for price, volume in zip(prices, volumes, strict=True)]
    submit_ms = []
    with tempfile.TemporaryDirectory(prefix="second-archive-benchmark-") as folder:
        root = Path(folder)
        writer = SecondQuoteArchive(root, enabled=lambda: True, retention_days=lambda: 0)
        started = time.perf_counter()
        try:
            for capture in range(rounds):
                stamp = start + capture * 1000
                records = []
                for i, symbol in enumerate(symbols):
                    delta = rng.randint(0, 100)
                    volumes[i] += delta
                    amounts[i] += delta * prices[i] * 100
                    records.append({
                        "symbol": symbol, "last_price": prices[i], "prev_close": prices[i],
                        "open": prices[i], "high": prices[i] + .01, "low": prices[i] - .01,
                        "volume": volumes[i], "amount": amounts[i],
                        "timestamp": stamp, "source_timestamp": stamp,
                        "source_time": f"09:{30 + capture // 60:02d}:{capture % 60:02d}",
                        "provider_version": "synthetic-v1",
                        "bid1": prices[i] - .01, "ask1": prices[i] + .01,
                        "bid1_volume": rng.randint(1, 1000), "ask1_volume": rng.randint(1, 1000),
                    })
                before = time.perf_counter()
                writer.submit({"stock": records}, provider="synthetic",
                              capture_id=capture + 1, collected_at_ms=stamp, trade_date=day)
                submit_ms.append((time.perf_counter() - before) * 1000)
        finally:
            drained = writer.stop(timeout=60)
        write_elapsed = time.perf_counter() - started
        if not drained:
            raise RuntimeError("synthetic writer did not drain")
        state = writer.status()
        size = sum(path.stat().st_size for path in root.rglob("*.parquet"))
        before = time.perf_counter()
        quality = SecondQuoteReplay(root).validate(
            day, day, symbols=symbols, start_ms=start, end_ms=start + (rounds - 1) * 1000,
        )
        validation_s = time.perf_counter() - before
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "kind": "synthetic_offline_not_market_sla", "symbols": symbol_count, "rounds": rounds,
            "write_wall_s_including_input_generation": round(write_elapsed, 3),
            "submit_p50_ms": round(statistics.median(submit_ms), 3),
            "submit_p95_ms": round(sorted(submit_ms)[int((len(submit_ms) - 1) * .95)], 3),
            "parquet_bytes": size,
            "bytes_per_written_row": round(size / max(1, state["written_rows"]), 3),
            "process_peak_rss_mib": round(rss / (1024**2 if sys.platform == "darwin" else 1024), 2),
            "validation_s": round(validation_s, 3), "quality_status": quality["status"],
            "archive": state,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=5000)
    parser.add_argument("--rounds", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.symbols <= 10_000 or not 2 <= args.rounds <= 120:
        parser.error("symbols must be 1..10000 and rounds 2..120")
    print(json.dumps(benchmark(args.symbols, args.rounds), ensure_ascii=False, indent=2))
