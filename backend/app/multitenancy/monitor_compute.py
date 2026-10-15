"""Adapter around the original monitor engine for explicit public/private inputs."""
from __future__ import annotations

import time
from datetime import datetime

import polars as pl

from app.market_time import cn_now, cn_today
from app.multitenancy.market import decode_frame
from app.strategy import custom_signals
from app.strategy.intraday_signals import IntradaySignalEvaluator
from app.strategy.monitor import MonitorRuleEngine


class SectorSnapshots:
    def __init__(self, values):
        self.values = {
            key: {**value, "window_changes": {int(k): v for k, v in value.get("window_changes", {}).items()}}
            for key, value in values.items()
        }

    def build_snapshots(self, _stock, _indices, targets, _windows, **_kwargs):
        return {target["key"]: self.values[target["key"]] for target in targets if target["key"] in self.values}


def needs_sandbox(rules):
    return any(
        rule.get("type") == "strategy" or any(
            str(condition.get("field", "")).startswith(("csg_", "csgi_"))
            for condition in rule.get("conditions", [])
        ) for rule in rules
    )


def fresh_frame(frame, now=None):
    field = "quote_ts" if "quote_ts" in frame.columns else "timestamp"
    if field not in frame.columns:
        return frame.head(0)
    stamp = pl.col(field).cast(pl.Float64, strict=False)
    seconds = pl.when(stamp > 1e12).then(stamp / 1000).otherwise(stamp)
    age = (time.time() if now is None else now) - seconds
    return frame.filter(age.is_between(-2, 15))


def evaluate_snapshot(payload, repo, strategy_engine=None, private_root=None):
    snapshot = payload["snapshot"]
    engine = MonitorRuleEngine(groups={key: frozenset(value) for key, value in payload.get("groups", {}).items()})
    previous = payload.get("state", {})
    engine.restore_state(previous.get("engine"))
    engine.set_rules(payload["rules"])
    engine.set_name_map(snapshot.get("names", {}))
    if strategy_engine is not None:
        from app.services.screener import ScreenerService

        engine.set_strategy_engine(strategy_engine)
        engine.set_data_dir(private_root)
        engine.set_history_loader(ScreenerService(repo)._load_enriched_history)
        engine.set_history_loader_etf(ScreenerService(repo, asset_type="etf")._load_enriched_history)
    engine.set_sector_monitor_service(SectorSnapshots(snapshot.get("sectors", {})))
    intraday = IntradaySignalEvaluator()
    intraday._last_bar = {
        tuple(key): datetime.fromisoformat(value) for key, value in previous.get("intraday", [])
    }
    events, execution_events, current_frames = [], [], {}
    if snapshot["status"].get("is_trading_hours"):
        for asset in ("stock", "etf", "index"):
            if snapshot.get("days", {}).get(asset) != cn_today().isoformat():
                continue
            frame = fresh_frame(decode_frame(snapshot["frames"][asset]))
            if frame.is_empty():
                continue
            if strategy_engine is not None:
                expressions = custom_signals.build_expressions(
                    [item for item in payload.get("signals", []) if item.get("timeframe", "daily") != "intraday"],
                    allow_shift=False,
                )
                frame = custom_signals.inject(frame, expressions)
            symbols = engine.intraday_signal_symbols(asset)
            if symbols:
                # Read the platform's published minute partitions only. No
                # provider fallback is allowed in an API/tenant worker.
                minutes = repo.get_minute_batch(sorted(symbols), cn_today(), asset_type=asset)
                references = {}
                for row in frame.filter(pl.col("symbol").is_in(sorted(symbols))).iter_rows(named=True):
                    close, change = row.get("close"), row.get("change_pct")
                    reference = row.get("prev_close")
                    if reference is None and close is not None and change is not None and change > -1:
                        reference = close / (1 + change)
                    if reference is not None:
                        references[row["symbol"]] = reference
                signals = intraday.evaluate(
                    minutes, symbols=symbols, prev_close=references, asset_type=asset,
                    now=cn_now(), signals=[item for item in payload.get("signals", []) if item.get("timeframe") == "intraday"],
                )
                frame = intraday.inject(frame, signals)
            current_frames[asset] = frame
            events.extend(engine.evaluate(
                frame, asset_type=asset, reset_strategy_results=asset == "stock",
                execution_events=execution_events,
            ))
        events.extend(engine.evaluate_sectors(
            current_frames.get("stock", pl.DataFrame()), decode_frame(snapshot["indices"]),
        ))
        stock = current_frames.get("stock", pl.DataFrame())
        fresh_symbols = set(stock["symbol"].to_list()) if "symbol" in stock.columns else set()
        events.extend(engine.evaluate_abnormal([
            row for row in snapshot.get("abnormal", []) if row.get("symbol") in fresh_symbols
        ]))
        events.extend(engine.evaluate_date_rules())
    return {
        "events": events, "execution_events": execution_events, "results": engine.latest_strategy_results(),
        "state": {
            "engine": engine.export_state(),
            "intraday": [[list(key), value.isoformat()] for key, value in intraday._last_bar.items()],
        },
    }
