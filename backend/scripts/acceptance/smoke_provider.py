"""Bounded read-only daily Provider smoke; never publishes business data."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl


def probe(provider_name: str, symbol: str) -> dict:
    from app.data_providers.registry import get_provider
    from app.market_time import cn_now

    provider = get_provider(provider_name)
    end = cn_now().replace(tzinfo=None)
    start = end - timedelta(days=7)
    try:
        frame = provider.get_daily([symbol], start, end, "stock")
        if not isinstance(frame, pl.DataFrame) or frame.is_empty():
            raise ValueError("empty daily response")
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        if not required <= set(frame.columns):
            raise ValueError("missing normalized daily columns")
        assert not any(frame.select(sorted(required)).null_count().row(0))
        assert set(frame["symbol"].to_list()) == {symbol}
        for column in ("open", "high", "low", "close"):
            assert frame.select((pl.col(column).is_finite() & (pl.col(column) > 0)).all()).item()
        assert frame.select((pl.col("high") >= pl.max_horizontal("open", "close", "low")).all()).item()
        assert frame.select((pl.col("low") <= pl.min_horizontal("open", "close", "high")).all()).item()
        assert frame.select((pl.col("volume").is_finite() & (pl.col("volume") >= 0)).all()).item()
        days = frame["date"].cast(pl.Date)
        assert days.min() >= start.date() and days.max() <= end.date()
        assert frame.unique(["symbol", "date"]).height == frame.height
        return {"status": "passed", "rows": frame.height,
                "first_date": str(days.min()), "last_date": str(days.max()),
                "normalized_ohlcv": True, "source_clock_verified": False,
                "historical_pit_verified": False}
    finally:
        close = getattr(provider, "close", None)
        if close:
            close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="rustdx")
    parser.add_argument("--symbol", default="000001.SZ")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.symbol not in {"000001.SZ", "600000.SH"} or not 5 <= args.timeout <= 60:
        parser.error("select a known stock and timeout in [5,60]")
    if args.child:
        args.output.write_text(json.dumps(probe(args.provider, args.symbol)) + "\n")
        return
    started = time.monotonic()
    report = {"status": "failed", "provider": args.provider, "symbol": args.symbol,
              "checked_at": datetime.now().astimezone().isoformat(),
              "scope": "single_stock_seven_calendar_days_read_only",
              "limits": ["no publish, all-market coverage or freshness certification",
                         "no historical PIT certification"]}
    with tempfile.TemporaryDirectory(prefix="tsp-provider-smoke-") as directory:
        result = Path(directory) / "result.json"
        env = {**os.environ, "DATA_DIR": directory, "PLATFORM_CONFIG_DIR": "",
               "TICKFLOW_ENV_FILE": "/dev/null", "MULTITENANCY_ENABLED": "false",
               "SERVICE_ROLE": "standalone", "RUSTDX_WORKERS": "1"}
        try:
            child = subprocess.run([
                sys.executable, "-m", "scripts.acceptance.smoke_provider",
                "--child", "--provider", args.provider, "--symbol", args.symbol,
                "--output", str(result),
            ], env=env, capture_output=True, timeout=args.timeout)
            if child.returncode:
                report["failure_kind"] = "provider_process_failed"
            else:
                report.update(json.loads(result.read_text()))
        except subprocess.TimeoutExpired:
            report["failure_kind"] = "timeout"
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
