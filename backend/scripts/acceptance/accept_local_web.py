"""Real loopback HTTP acceptance with disposable data and production routes.

Only collectors/schedules are replaced at their external boundary. Authentication,
repositories, pipeline job persistence, research workers and paper APIs run normally.
--serve keeps the same isolated application available for Browser QA.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import polars as pl
import uvicorn

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "isolated-web-acceptance"
CODE = '''import polars as pl
META = {"id":"custom_acceptance","name":"验收价格策略","description":"","tags":[],
        "params":[],"scoring":{},"asset_types":["stock"],"timeframes":["1d"]}
def filter(df, params):
    return pl.col("close") > 15
'''


def seed(root: Path) -> None:
    from app.enriched_generation import bump_enriched_generation
    from app.indicators.pipeline import compute_enriched

    rows = []
    for offset in range(140):
        day = date(2026, 6, 6) + timedelta(days=offset)
        if day.weekday() > 4:
            continue
        for symbol, initial in (("000001.SZ", 11.), ("600000.SH", 21.)):
            price = initial + offset * .003
            rows.append({
                "symbol": symbol, "date": day, "open": price, "high": price + .2,
                "low": price - .2, "close": price, "raw_open": price,
                "raw_close": price, "raw_high": price + .2, "raw_low": price - .2,
                "volume": 1000000., "amount": price * 1000000., "turnover_rate": 1.,
                "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
                "is_risk_warning": False, "instrument_status_known": True, "is_listed": True,
            })
    frame = pl.DataFrame(rows)
    enriched = compute_enriched(frame)
    for day in frame["date"].unique().to_list():
        for name in ("kline_daily", "kline_daily_enriched"):
            target = root / name / f"date={day}"
            target.mkdir(parents=True)
            values = enriched if name.endswith("enriched") else frame
            values.filter(pl.col("date") == day).write_parquet(target / "part.parquet")
    target = root / "instruments"
    target.mkdir()
    pl.DataFrame([
        {"symbol": symbol, "name": name, "code": symbol[:6], "exchange": symbol[-2:],
         "asset_type": "stock", "total_shares": 100000000., "float_shares": 100000000.,
         "listing_date": "2020-01-01", "as_of": date(2026, 10, 23)}
        for symbol, name in (("000001.SZ", "平安银行"), ("600000.SH", "浦发银行"))
    ]).write_parquet(target / "instruments.parquet")
    target = root / "strategies/custom"
    target.mkdir(parents=True)
    (target / "custom_acceptance.py").write_text(CODE, encoding="utf-8")
    bump_enriched_generation(root)


def fixture_sync(repo, capset, on_progress=None):
    from app.services.data_release import ensure_current_release

    if on_progress:
        on_progress("fixture", 100, "隔离行情样本已验证")
    repo.refresh_cache()
    release = ensure_current_release(repo.store.data_dir)
    return {"universe_size": 2, "data_release_id": release["release_id"], "fixture": True}


def isolated_app(root: Path, resources: ExitStack):
    from app.config import settings
    from app.plugins.rustdx.provider import RustdxProvider
    from app.services import preferences

    for name, value in {
        "data_dir": root, "platform_config_dir": None, "multitenancy_enabled": False,
        "local_admin_mode": False, "service_role": "standalone",
        "static_dir": ROOT / "frontend/dist", "auth_username": "admin",
        "auth_email": "", "auth_password": PASSWORD, "backtest_matrix_cache_prewarm": False,
    }.items():
        resources.enter_context(patch.object(settings, name, value))
    preferences.save({
        "data_sync_auto_enabled": False, "realtime_quotes_enabled": False,
        "minute_intraday_refresh": False,
    })
    # On-demand chart requests also go through the fixture, not remote providers.
    provider = RustdxProvider()
    daily = pl.read_parquet(str(root / "kline_daily/date=*/part.parquet"))

    def get_daily(symbols, start_time=None, end_time=None, *args, **kwargs):
        return daily.filter(pl.col("symbol").is_in(symbols))

    provider.get_daily = get_daily
    provider.get_realtime = lambda *args, **kwargs: []
    provider.get_minute = lambda *args, **kwargs: pl.DataFrame()
    provider.get_adj_factors = lambda *args, **kwargs: pl.DataFrame()
    provider.get_depth_batch = lambda *args, **kwargs: {}
    for name in ("app.data_providers.registry.get_provider", "app.data_providers.custom.get_provider"):
        resources.enter_context(patch(name, return_value=provider))
    # These substitutions are visible in evidence, never production configuration.
    for name in (
        "app.jobs.daily_pipeline.start_scheduler",
        "app.services.minute_refresh.MinuteRefreshService.start",
        "app.services.depth_service.DepthService.boot_check",
        "app.services.depth_service.DepthService.start_polling",
        "app.services.ext_pull.PullScheduler.start",
        "app.services.ext_pull.PullScheduler.refresh",
        "app.services.financial_sync.FinancialScheduler.start",
    ):
        resources.enter_context(patch(name, return_value=None))
    resources.enter_context(patch("app.jobs.daily_pipeline.run_now", fixture_sync))
    from app.main import app

    return app


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise TimeoutError("isolated acceptance did not reach the expected state")


def workflows(base: str) -> dict:
    with httpx.Client(base_url=base, timeout=120, trust_env=False) as client:
        assert client.get("/api/watchlist").status_code == 401
        login = client.post("/api/auth/login", json={"email": "admin", "password": PASSWORD})
        assert login.status_code == 200 and login.json()["user"]["role"] == "admin"
        assert client.get("/api/auth/status").json()["authenticated"]
        added = client.post("/api/watchlist", json={"symbol": "600000.SH", "note": "local-acceptance"})
        assert added.status_code == 200
        assert client.get("/api/watchlist").json()["symbols"][0]["note"] == "local-acceptance"
        assert client.get("/api/kline/daily", params={"symbol": "600000.SH", "days": 30}).status_code == 200
        job = client.post("/api/pipeline/run").json()["job_id"]
        terminal = wait_for(lambda: (
            row if (row := client.get("/api/pipeline/jobs/" + job).json())["status"]
            in {"succeeded", "failed", "cancelled"} else None
        ))
        assert terminal["status"] == "succeeded" and terminal["result"]["fixture"]
        quality = client.get("/api/data/status").json()["quality"]
        assert quality["latest_daily"]["coverage_ratio"] == 1
        config = {
            "strategy_id": "custom_acceptance", "start": "2026-07-01", "end": "2026-10-23",
            "overrides": json.dumps({"basic_filter": {
                "market_cap_min": None, "exclude_new_days": 0, "exclude_st": False,
            }}),
        }

        def research(params):
            response = client.get("/api/backtest/strategy/stream", params=params)
            assert response.status_code == 200 and "event: done" in response.text
            for block in response.text.replace("\r\n", "\n").split("\n\n"):
                if "event: done" in block:
                    return json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: ")))
            raise AssertionError("research result event missing")

        result = research(config)
        assert result["stats"]["n_trades"] > 0
        snapshot = result["input_snapshot"]["id"]
        replay = research({**config, "input_snapshot_id": snapshot, "experiment_id": "a" * 32})
        def business_stats(value):
            return {
                key: item for key, item in value.items() if key not in {"timing_ms", "worker"}
            }
        assert business_stats(replay["stats"]) == business_stats(result["stats"])
        assert replay["trades"] == result["trades"], (result["trades"][:1], replay["trades"][:1])
        new = research({**config, "experiment_id": "b" * 32})
        assert new["input_snapshot"]["id"] == snapshot
        account = client.post("/api/paper/account", json={"initial_cash": 100000, "account_id": "acceptance"})
        assert account.status_code == 200
        order = client.post("/api/paper/orders", params={"account": "acceptance"}, json={
            "symbol": "600000.SH", "side": "buy", "qty": 100, "ref_price": 22,
            "order_type": "next_open", "request_id": "local-order",
        })
        assert order.status_code == 200
        identifier = order.json()["order"]["id"]
        assert client.delete("/api/paper/orders/" + identifier, params={"account": "acceptance"}).status_code == 200
        before = client.get("/api/paper/overview", params={"account": "acceptance"}).json()
        assert client.post("/api/paper/rebuild", params={"account": "acceptance"}).status_code == 200
        after = client.get("/api/paper/overview", params={"account": "acceptance"}).json()
        assert before["cash"] == after["cash"] == 100000
        client.post("/api/auth/logout").raise_for_status()
        assert client.get("/api/watchlist").status_code == 401
        return {"login_logout": True, "watchlist": True, "fixture_sync": True,
                "quality": True, "real_research_trade_count": result["stats"]["n_trades"],
                "original_input_replay": True, "new_experiment": True,
                "paper_order_cancel_and_rebuild": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=39018)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.serve and args.output is None:
        parser.error("--output is required for acceptance")
    if os.environ.get("TICKFLOW_ENV_FILE") != "/dev/null":
        parser.error("set TICKFLOW_ENV_FILE=/dev/null to isolate credentials")
    report = {"status": "failed", "scope": "loopback_standalone_fixture",
              "limits": ["collectors disabled; sync uses a fixture", "no real-time fill or trading-day certification",
                         "tenant/SSE/cancellation covered separately by real PostgreSQL tests",
                         "no production deployment, TLS or sustained capacity certification"]}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tsp-web-") as directory, ExitStack() as resources:
        root = Path(directory)
        seed(root)
        app = isolated_app(root, resources)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
        if args.serve:
            server.run()
            return
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            wait_for(lambda: server.started)
            report["checks"] = workflows(f"http://127.0.0.1:{args.port}")
            report["status"] = "passed"
        except Exception as exc:
            report["failure_kind"] = type(exc).__name__
            raise
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            report["clean_shutdown"] = not thread.is_alive()
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
