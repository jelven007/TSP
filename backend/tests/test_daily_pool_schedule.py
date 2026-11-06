"""Two independent premarket batches, with exact session inputs and durable retries."""
# ruff: noqa: F401,F811 -- reuse the strategy fixture.
from datetime import UTC, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from test_daily_stock_pools import BF, DAY, PRIOR, engine, frames

from app.services.daily_stock_pools import DailyStockPools
from app.strategy.engine import StrategyDataContext


def wall(hour, minute=0):
    return datetime(2026, 10, 26, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_slots_calendar_timezone_and_next_due():
    from app.services.daily_pool_schedule import due_slot, next_check

    assert due_slot(wall(7, 59)) is None
    assert due_slot(wall(8)) == "08:00"
    assert due_slot(wall(8, 59).astimezone(UTC)) == "08:00"
    assert due_slot(wall(9)) == "09:00"
    assert due_slot(wall(20)) == "09:00"
    assert due_slot(wall(8).replace(day=25)) is None
    assert due_slot(wall(8).replace(month=10, day=1)) is None
    assert next_check(wall(7, 59).replace(second=50)) == wall(8)
    assert next_check(wall(8, 59).replace(second=50)) == wall(9)


def test_premarket_uses_exact_prior_raw_price_but_current_scope(engine):
    from app.strategy.daily_pool import build_pool

    history = frames().filter(pl.col("date") == PRIOR).with_columns(
        pl.col("close").alias("raw_close"), pl.lit(1.0).alias("close"),
        pl.lit(False).alias("is_risk_warning"))
    current = pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH", "600002.SH"],
        "date": [DAY] * 3, "is_risk_warning": [False, True, False],
    })
    config = {**BF, "data_reference": "signal_day", "exclude_st": True, "exclude_suspended": True}
    context = StrategyDataContext("stock", "1d", DAY, current=current, pool_history=history)
    pool = build_pool(engine, "sample", context, overrides={"basic_filter": config}, premarket=True)
    assert pool["symbols"] == ["600000.SH"]
    assert pool["reference_date"] == str(PRIOR)
    assert pool["unknown_suspension_count"] == 1
    assert config["data_reference"] == "signal_day"
    with pytest.raises(ValueError, match="参考"):
        build_pool(engine, "sample", context, overrides={"basic_filter": config})


def test_premarket_context_ignores_same_day_prices_and_loads_previous_session(monkeypatch):
    from app.services.screener import ScreenerService

    instruments = pl.DataFrame({"symbol": ["600000.SH"], "name": ["当前名称"], "float_shares": [1e9]})
    reference = frames().filter(pl.col("date") == PRIOR)
    service = ScreenerService(SimpleNamespace(
        get_instruments_asset=lambda _: instruments,
        get_daily_close_inputs=lambda _: (reference, reference.select("symbol", "close")),
    ))
    calls = []
    monkeypatch.setattr(service, "_load_enriched_history",
                        lambda day, count: calls.append((day, count)) or frames())
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *_: pytest.fail("today quote dependency"))
    context = service.build_daily_pool_context(DAY, current=frames(), premarket=True)
    assert calls == [(PRIOR, 1)]
    assert context.pool_history["date"].unique().to_list() == [PRIOR]
    assert context.current["date"].to_list() == [DAY]
    assert "close" not in context.current.columns


def test_real_parquet_premarket_reference_excludes_future_day(engine, tmp_path, monkeypatch):
    from app.services.screener import ScreenerService
    from app.strategy.daily_pool import build_pool
    from app.tickflow.repository import DataStore, KlineRepository, enriched_dirname

    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    directory = tmp_path / enriched_dirname("stock") / f"date={PRIOR}"
    directory.mkdir(parents=True)
    data = frames().with_columns(
        pl.col("close").alias("raw_close"), pl.col("close").alias("open"),
        pl.col("close").alias("high"), pl.col("close").alias("low"),
        pl.lit(1e7).alias("amount"), pl.lit(1.0).alias("turnover_rate"))
    data.write_parquet(directory / "sample.parquet")
    reference = data.filter(pl.col("date") == PRIOR)
    for table, frame in (
        ("kline_daily", reference),
        ("kline_minute", reference.select("symbol", "close").with_columns(
            pl.lit(datetime.combine(PRIOR, time(15))).alias("datetime"),
        )),
    ):
        part = tmp_path / table / f"date={PRIOR}"
        part.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(part / "part.parquet")
    instruments = pl.DataFrame({"symbol": ["600000.SH", "600001.SH", "600002.SH"],
                                "name": ["A", "B", "C"], "float_shares": [1e9] * 3})
    monkeypatch.setattr(repo, "get_instruments_asset", lambda _: instruments)
    try:
        context = ScreenerService(repo).build_daily_pool_context(DAY, premarket=True)
        assert context.pool_history["date"].unique().to_list() == [PRIOR]
        pool = build_pool(engine, "sample", context, overrides={"basic_filter": BF}, premarket=True)
        assert pool["symbols"] == ["600000.SH", "600001.SH"]
    finally:
        store.db.close()


def test_local_slots_retry_failed_only_and_survive_restart(engine, tmp_path):
    from app.services.daily_stock_pools import generate_scheduled_local

    context = StrategyDataContext("stock", "1d", DAY,
        current=frames().filter(pl.col("date") == DAY), pool_history=frames())
    calls = []
    service = SimpleNamespace(build_daily_pool_context=lambda *a, **k: calls.append(k) or context)
    first = generate_scheduled_local(engine, service, tmp_path, now=wall(8))
    assert first["saved"] == ["sample"]
    record8 = DailyStockPools(tmp_path).read("sample", DAY)
    assert record8["generation_slot"] == "08:00"
    assert generate_scheduled_local(engine, service, tmp_path, now=wall(8, 1))["saved"] == []
    engine.get("sample").basic_filter = {**BF, "float_cap_max": 11e9}
    second = generate_scheduled_local(engine, service, tmp_path, now=wall(9))
    assert second["saved"] == ["sample"]
    record9 = DailyStockPools(tmp_path).read("sample", DAY)
    assert record9["id"] != record8["id"]
    assert record9["symbols"] == ["600000.SH"]
    assert record9["generation_slot"] == "09:00"
    assert DailyStockPools(tmp_path).read("sample", DAY, record_id=record8["id"]) is None
    assert len(calls) == 2
    assert generate_scheduled_local(engine, service, tmp_path, now=wall(9, 1))["saved"] == []


def test_local_failed_batch_retries_and_late_start_marks_missed_slot(engine, tmp_path):
    from app.services.daily_stock_pools import generate_scheduled_local

    engine.get("sample").basic_filter = BF
    broken = StrategyDataContext("stock", "1d", DAY, current=pl.DataFrame({"symbol": ["600000.SH"]}))
    service = SimpleNamespace(build_daily_pool_context=lambda *a, **k: broken)
    assert set(generate_scheduled_local(engine, service, tmp_path, now=wall(9))["errors"]) == {"sample"}
    assert DailyStockPools(tmp_path).job(f"{DAY}:08:00")["state"] == "interrupted"
    good = StrategyDataContext("stock", "1d", DAY, current=frames(), pool_history=frames())
    service.build_daily_pool_context = lambda *a, **k: good
    assert generate_scheduled_local(engine, service, tmp_path, now=wall(9, 1))["saved"] == ["sample"]
    assert DailyStockPools(tmp_path).job(f"{DAY}:09:00")["state"] == "succeeded"
