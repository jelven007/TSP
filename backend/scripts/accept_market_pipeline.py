"""Bounded live historical-data probe over the real repository and HTTP bridge.

This is a local acceptance fixture, not a production service. It never writes to
the input data directory. Live quote freshness and trading-day soak acceptance
must be measured separately; historical bars cannot certify either.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def frame_hash(frame):
    return hashlib.sha256(frame.sort(["symbol", "date"]).write_json().encode()).hexdigest()


def serve(app, port):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=2,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"acceptance HTTP server did not start on {port}")
        time.sleep(0.05)
    return server, thread


def stop_server(resource):
    server, thread = resource
    server.should_exit = True
    thread.join(timeout=8)
    if thread.is_alive():
        raise RuntimeError("acceptance HTTP server did not stop")


def run(args):
    args.run_dir.mkdir(parents=True, exist_ok=False)
    import httpx
    import polars as pl
    from fastapi import FastAPI
    from pydantic import SecretStr

    from app.api import intraday, kline
    from app.cell import platform_market
    from app.data_providers import custom
    from app.data_providers.normalizer import normalize_daily
    from app.indicators.pipeline import compute_enriched
    from app.multitenancy.market import PublicMarket
    from app.multitenancy.quote_bridge import PublicQuoteBridge
    from app.multitenancy.quote_markers import QuoteMarkerBridge
    from app.parquet import scan_daily_parquet
    from app.services.quote_service import QuoteService
    from app.tickflow.repository import DataStore, KlineRepository

    origin = args.run_dir / "market"
    origin.mkdir()
    instruments = pl.read_parquet(
        args.data_dir / "instruments/instruments.parquet",
    ).filter(pl.col("symbol").is_in(args.symbols))
    if set(instruments["symbol"].to_list()) != set(args.symbols):
        raise ValueError("requested securities must exist in the input universe")
    (origin / "instruments").mkdir()
    instruments.write_parquet(origin / "instruments/instruments.parquet")
    os.environ["TSP_PLATFORM_MARKET_TOKEN"] = secrets.token_urlsafe(32)
    custom.load_all()
    if not custom.provider_has_dataset(args.provider, "daily"):
        raise ValueError("selected provider has no daily capability")
    provider = custom.get_provider(args.provider)
    samples = []
    evidence = {
        "mode": "live_historical_bars",
        "symbols": args.symbols,
        "start": str(args.end - timedelta(days=210)),
        "end": str(args.end),
        "source_freshness_certified": False,
        "trading_day_soak_certified": False,
        "samples": samples,
        "status": "running",
    }
    with ExitStack() as resources:
        resources.callback(custom.close_all)
        store = DataStore(origin)
        resources.callback(store.db.close)
        repo = KlineRepository(store)
        repo._refresh_instruments()
        service = QuoteService()
        service.set_repo(repo)
        platform = FastAPI()
        platform.state.repo = repo
        platform.state.quote_service = service
        platform.include_router(platform_market.router)
        platform_server = serve(platform, args.platform_port)
        resources.callback(stop_server, platform_server)
        reader_store = DataStore(origin, read_only=True)
        resources.callback(reader_store.db.close)
        reader = KlineRepository(reader_store)
        reader._refresh_instruments()
        market = PublicMarket(SimpleNamespace(
            market_url=f"http://127.0.0.1:{args.platform_port}",
            market_token=SecretStr(os.environ["TSP_PLATFORM_MARKET_TOKEN"]),
        ))
        bridge = PublicQuoteBridge(reader, market)
        markers = QuoteMarkerBridge(
            f"http://127.0.0.1:{args.platform_port}",
            os.environ["TSP_PLATFORM_MARKET_TOKEN"], on_event=bridge.mark_event,
        )
        bridge.attach_markers(markers)
        bridge.start()
        resources.callback(bridge.close)
        api = FastAPI()
        api.state.repo = reader
        api.state.quote_service = bridge
        api.include_router(kline.router)
        api.include_router(intraday.router)

        @api.middleware("http")
        async def refresh(request, call_next):
            if request.url.path != "/api/intraday/stream":
                import asyncio

                await asyncio.to_thread(bridge.ensure_fresh)
            return await call_next(request)

        @api.get("/acceptance/evidence")
        def current_evidence():
            return evidence

        api_server = serve(api, args.api_port)
        resources.callback(stop_server, api_server)
        client = resources.enter_context(httpx.Client(
            base_url=f"http://127.0.0.1:{args.api_port}", timeout=30, trust_env=False,
        ))
        params = {
            "symbol": args.symbols[0],
            "start_date": evidence["start"], "end_date": evidence["end"],
        }
        for iteration in range(args.rounds):
            sample = {"round": iteration + 1}
            started = time.perf_counter()
            frame = provider.get_daily(
                args.symbols,
                datetime.combine(args.end - timedelta(days=210), day_time.min),
                datetime.combine(args.end, day_time.max),
            )
            sample["provider_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            frame = normalize_daily(frame, source=args.provider)
            if frame.is_empty() or set(frame["symbol"]) != set(args.symbols):
                raise ValueError("incomplete historical provider response")
            if frame["date"].max() > args.end:
                raise ValueError("provider returned a date outside the requested window")
            sample["normalization_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            repo.append_daily(frame)
            sample["daily_write_ms"] = (time.perf_counter() - started) * 1000
            persisted = scan_daily_parquet(
                str(origin / "kline_daily/**/*.parquet"),
            ).select(frame.columns).collect()
            if frame_hash(frame) != frame_hash(persisted):
                raise ValueError("daily repository round-trip changed source values")
            started = time.perf_counter()
            # No factor evidence was supplied. This probe deliberately computes
            # unadjusted technical values, and records that limitation.
            enriched = compute_enriched(frame, instruments=instruments)
            sample["indicators_ms"] = (time.perf_counter() - started) * 1000
            latest = enriched.filter(pl.col("date") == enriched["date"].max())
            if latest.height != len(args.symbols) or latest["ma60"].null_count():
                raise ValueError("missing latest rows or MA60 history")
            started = time.perf_counter()
            repo.append_enriched(enriched)
            repo.flush_live_enriched(latest)
            sample["enriched_write_ms"] = (time.perf_counter() - started) * 1000
            generation = bridge.update_generation
            started = time.perf_counter()
            service._broadcast_quote_updated()
            deadline = time.monotonic() + 10
            while bridge.update_generation <= generation:
                if time.monotonic() > deadline:
                    raise RuntimeError("platform SSE marker was not received")
                time.sleep(0.01)
            sample["marker_http_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            # Expire only the platform's one-second snapshot memo for this
            # explicit acceptance cycle, never modify the live application.
            platform.state.full_public_snapshot = None
            bridge.ensure_fresh()
            received, received_day = reader.get_enriched_latest()
            if frame_hash(received.select(latest.columns)) != frame_hash(latest):
                raise ValueError("HTTP Arrow snapshot differs from repository")
            sample["snapshot_http_decode_ms"] = (time.perf_counter() - started) * 1000
            reader.rebuild_views()
            started = time.perf_counter()
            response = client.get("/api/kline/daily", params=params)
            response.raise_for_status()
            payload = response.json()
            rows = payload["rows"]
            if not rows or rows[-1]["date"][:10] != str(received_day):
                raise ValueError("public Kline API returned the wrong final day")
            expected = latest.filter(pl.col("symbol") == args.symbols[0])["close"][0]
            if abs(rows[-1]["close"] - expected) > 1e-6:
                raise ValueError("public Kline API changed the latest price")
            sample["api_http_ms"] = (time.perf_counter() - started) * 1000
            sample.update(rows=frame.height, latest_day=str(received_day), price=expected)
            samples.append(sample)
            evidence.update(
                adjustment="raw_without_supplied_factors",
                frame_sha256=frame_hash(frame),
                enriched_sha256=frame_hash(latest),
                snapshot_fetches=bridge.snapshot_fetches,
                frame_decodes=bridge.frame_decodes,
                marker_reconnects=markers.reconnects,
            )
            (args.run_dir / "result.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            )
            print(f"round {iteration + 1}: {frame.height} dated rows verified", flush=True)
            time.sleep(args.interval)
        evidence["status"] = "completed"
        (args.run_dir / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )
        print("bounded probe complete; HTTP fixture remains for browser verification", flush=True)
        time.sleep(args.serve_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--provider", default="rustdx")
    parser.add_argument("--symbols", default="600036.SH,000001.SZ,688981.SH,300750.SZ")
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--platform-port", type=int, default=3060)
    parser.add_argument("--api-port", type=int, default=3061)
    parser.add_argument("--serve-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100 or not 0 <= args.interval <= 60:
        parser.error("rounds must be 1..100 and interval 0..60 seconds")
    if not 0 <= args.serve_seconds <= 1800:
        parser.error("serve-seconds must be 0..1800")
    args.symbols = list(dict.fromkeys(args.symbols.split(",")))
    if not 1 <= len(args.symbols) <= 20:
        parser.error("select between 1 and 20 securities")
    # All implicit configuration writes belong to the new fixture as well.
    os.environ["DATA_DIR"] = str(args.run_dir.resolve() / "config")
    os.environ["TICKFLOW_ENV_FILE"] = str(args.run_dir.resolve() / ".env")
    run(args)


if __name__ == "__main__":
    main()
