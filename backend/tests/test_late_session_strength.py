"""尾盘走强的时间、完整股票池分母和双周期完成态 MA5 契约。"""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.api.signals import SignalModel
from app.strategy import custom_signals

DAY = date(2026, 9, 4)
TARGET = datetime(2026, 9, 4, 14, 30)
POOL = ["a", "b", "c", "d", "e"]


def definition(**params):
    return {
        "id": "late_session_strength", "name": "尾盘走强", "kind": "entry",
        "timeframe": "intraday", "template": "late_session_strength",
        "conditions": [], "enabled": True,
        "params": {"time": "14:30:00", "change_min_pct": 0.4, "change_max_pct": 4.0,
                   "pool_up_min_pct": 60.0, "ma5_1m_count": 7, "ma5_5m_count": 4, **params},
    }


def bars(end=TARGET):
    rows = []
    for symbol in POOL:
        for i in range(60):
            price = 10.1 + i * 0.001 if symbol in POOL[:3] else 9.9 - i * 0.001
            rows.append({
                "symbol": symbol, "datetime": end - timedelta(minutes=59-i),
                "open": price, "high": price, "low": price, "close": price,
                "volume": 100.0, "amount": price * 10000,
            })
    return pl.DataFrame(rows)


def evaluate(frame=None, sig=None, pool=POOL, prev=None, **kwargs):
    from app.strategy.late_session_strength import evaluate

    return evaluate(bars() if frame is None else frame, sig or definition(), day=DAY,
                    pool_symbols=pool, prev_close=prev or dict.fromkeys(POOL, 10.0), **kwargs)


def test_definition_validates_and_roundtrips_without_daily_expression(tmp_path):
    sig = definition()
    model = SignalModel(**sig).model_dump()
    assert model.get("template") == "late_session_strength"
    assert model.get("params") == sig["params"]
    custom_signals.validate(model)
    custom_signals.save_one(tmp_path, model)
    assert custom_signals.load_all(tmp_path) == [model]
    assert custom_signals.build_expressions([model]) == {}
    assert custom_signals.build_intraday_expressions([model]) == {}


def test_exact_time_and_sixty_percent_full_pool():
    result = evaluate()
    assert result.symbols == POOL[:3]
    assert result.pool_up_pct == pytest.approx(60)
    assert not result.diagnostics
    assert evaluate(sig=definition(pool_up_min_pct=60.01)).symbols == []
    assert evaluate(completed_through=TARGET - timedelta(seconds=1)).symbols == []


def test_unknown_quote_or_previous_close_cannot_shrink_denominator():
    result = evaluate(frame=bars().filter(pl.col("symbol") != "e"))
    assert result.symbols == []
    assert result.diagnostics
    result = evaluate(prev=dict.fromkeys(POOL[:4], 10.0))
    assert not result.symbols and result.diagnostics
    assert not evaluate(pool=[]).symbols


@pytest.mark.parametrize("pct,expected", [(0.4, True), (4, True), (0.399, False), (4.001, False)])
def test_return_bounds_are_inclusive(pct, expected):
    frame = bars()
    last = frame.filter(pl.col("symbol") == "a")["close"][-1]
    frame = frame.with_columns(
        pl.when(pl.col("symbol") == "a").then(pl.col("close") * (10 * (1 + pct/100) / last))
        .otherwise(pl.col("close")).alias("close"))
    assert ("a" in evaluate(frame=frame).symbols) is expected


def test_custom_time_counts_and_latest_completed_five_minute():
    end = TARGET + timedelta(minutes=2)
    frame = bars(end)
    assert evaluate(frame=frame, sig=definition(time="14:32:00", ma5_1m_count=3,
                                              ma5_5m_count=2)).symbols == POOL[:3]
    assert not evaluate(frame=bars().tail(10), sig=definition()).symbols


@pytest.mark.parametrize("mutation", ["flat", "gap", "duplicate", "nan", "five_minute_decline"])
def test_strict_ma_trends_and_data_gaps(mutation):
    frame = bars()
    if mutation == "flat":
        frame = frame.with_columns(pl.when(pl.col("symbol") == "a").then(10.15)
                                   .otherwise(pl.col("close")).alias("close"))
    elif mutation == "gap":
        frame = frame.filter(~((pl.col("symbol") == "a")
                               & (pl.col("datetime") == TARGET - timedelta(minutes=12))))
    elif mutation == "duplicate":
        frame = pl.concat([frame, frame.filter((pl.col("symbol") == "a")
                                               & (pl.col("datetime") == TARGET))])
    elif mutation == "nan":
        frame = frame.with_columns(pl.when((pl.col("symbol") == "a")
                                          & (pl.col("datetime") == TARGET - timedelta(minutes=3)))
                                   .then(float("nan")).otherwise(pl.col("close")).alias("close"))
    else:
        # Last 1m MA5 still rises, but prior 5m MA5 slopes down.
        frame = frame.with_columns(pl.when((pl.col("symbol") == "a")
                                          & (pl.col("datetime") < TARGET - timedelta(minutes=10)))
                                   .then(10.3).otherwise(pl.col("close")).alias("close"))
    assert "a" not in evaluate(frame=frame).symbols


def test_future_rows_cannot_change_signal_and_warmup_never_crosses_lunch():
    frame = bars()
    future = frame.with_columns((pl.col("datetime") + pl.duration(hours=1)).alias("datetime"),
                                pl.lit(5.0).alias("close"))
    assert evaluate(frame=pl.concat([frame, future])).symbols == POOL[:3]
    assert not evaluate(frame=bars(datetime(2026, 9, 4, 13, 10)),
                        sig=definition(time="13:10:00")).symbols


@pytest.mark.parametrize("patch", [
    {"time": "09:30:00"}, {"time": "14:30:01"}, {"change_min_pct": 5},
    {"pool_up_min_pct": 101}, {"ma5_1m_count": 1}, {"ma5_5m_count": 2.5},
    {"change_max_pct": float("nan")},
])
def test_invalid_parameters_rejected(patch):
    with pytest.raises(ValueError):
        custom_signals.validate(definition(**patch))


def test_engine_injects_scheduled_pulse_and_preserves_full_pool_scope(tmp_path):
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, definition())
    previous = pl.DataFrame({"symbol": POOL, "date": [DAY-timedelta(days=1)]*5,
                             "close": [99.0]*5, "raw_close": [10.0]*5})
    context = StrategyDataContext("stock", "1m", DAY, history=bars(), pool_history=previous)
    injected = engine._inject_intraday_signal_columns(
        bars(), context=context, pool_symbols=POOL)
    col = "csgi_late_session_strength"
    hits = injected.filter(pl.col(col))
    assert hits["symbol"].to_list() == POOL[:3]
    assert hits["datetime"].to_list() == [TARGET]*3
    assert engine._collect_signal_hits(hits, [col])[0]["signals"] == [col]
    incomplete = StrategyDataContext("stock", "1m", DAY,
                                    completed_through=TARGET-timedelta(seconds=1), pool_history=previous)
    assert not engine._inject_intraday_signal_columns(
        bars(), context=incomplete, pool_symbols=POOL)[col].any()


def test_replay_reads_full_saved_pool_before_filtering_requested_outputs(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router
    from app.services.daily_stock_pools import DailyStockPools
    from app.services.strategy_results import ResultIdentity

    # Persist the same pool the strategy execution used, not the requested subset.
    identity = ResultIdentity("s", "示例", "v1", "{}", "{}", "null", "stock", "1m")
    DailyStockPools(tmp_path).save(identity, {
        "strategy_id": "s", "as_of": str(DAY), "symbols": POOL, "total": 5,
    }, started_at=1)
    custom_signals.save_one(tmp_path, definition())
    calls = []

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_daily_batch(self, symbols, *args, **kwargs):
            calls.append(set(symbols))
            return pl.DataFrame({"symbol": POOL, "date": [DAY-timedelta(days=1)]*5,
                                 "close": [99.0]*5, "raw_close": [10.0]*5})

        def get_minute_batch(self, symbols, *args, **kwargs):
            calls.append(set(symbols))
            return bars().filter(pl.col("symbol").is_in(symbols))

    app = FastAPI()
    app.state.repo = Repo()
    app.include_router(router)
    client = TestClient(app)
    payload = {"signal_id": "late_session_strength", "start_date": str(DAY),
               "end_date": str(DAY), "symbols": ["a"], "strategy_id": "s"}
    response = client.post("/api/custom-signals/intraday/replay", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["triggers"] == [{"date": str(DAY), "time": "14:30:00", "symbol": "a"}]
    assert all(call == set(POOL) for call in calls)
    assert client.post("/api/custom-signals/intraday/replay",
                       json={**payload, "strategy_id": None}).status_code == 400


def test_signal_keeps_close_labels_when_minute_strategy_projects_display_columns(tmp_path):
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"], "timeframes": ["1m"], "execution_backend": "minute_filter"}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params):
    return df.group_by("symbol").agg(pl.col("datetime").max().alias("last_datetime"))
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, definition())
    prior = pl.DataFrame({"symbol": POOL, "date": [DAY-timedelta(days=1)]*5, "close": [10.0]*5})
    current = prior.with_columns(pl.lit(DAY).alias("date"))
    context = StrategyDataContext("stock", "1m", DAY, history=bars(), current=current, pool_history=prior)
    result = engine.run("sample", context, overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert {hit["symbol"] for hit in result.entry_signal_hits} == set(POOL[:3])
    engine.get("sample").meta["minute_execution"] = "momentum_capacity_v1"
    with pytest.raises(ValueError, match="专用"):
        engine.run("sample", context, overrides={"entry_signals": ["csgi_late_session_strength"]})


def test_beijing_time_is_preserved_for_timezone_aware_minute_rows():
    aware = bars().with_columns(pl.col("datetime").dt.replace_time_zone("Asia/Shanghai")
                                .dt.convert_time_zone("UTC"))
    assert evaluate(frame=aware).symbols == POOL[:3]


def test_minute_replay_gates_entry_and_retains_full_pool_for_requested_subset(tmp_path):
    from types import SimpleNamespace

    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.strategy.engine import StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"], "timeframes": ["1m"], "execution_backend": "minute_filter"}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params):
    return df.sort("datetime").group_by("symbol").last().rename({"datetime": "last_datetime"})
''')
    strategy_engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, definition())
    panel = pl.DataFrame({"symbol": POOL*2, "date": [DAY-timedelta(days=1)]*5 + [DAY]*5,
                          "close": [10.0]*10, "raw_close": [10.0]*10, "name": ["示例"]*10})
    calls = []

    class Repo:
        def list_minute_dates(self, *args):
            return [DAY]

        def get_minute_by_dates(self, symbols, *args):
            calls.append(set(symbols))
            return bars().filter(pl.col("symbol").is_in(symbols))

    replayer = MinuteSignalReplayer(SimpleNamespace(repo=Repo()), strategy_engine)
    result = replayer.replay(strategy_engine.get("sample"), panel=panel, start=DAY, end=DAY,
                             params={}, overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert {hit.symbol for hit in result.hits} == set(POOL[:3])
    subset = replayer.replay(strategy_engine.get("sample"), panel=panel, start=DAY, end=DAY,
                             params={}, symbols=["a"],
                             overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert [hit.symbol for hit in subset.hits] == ["a"]
    assert calls == [set(POOL), set(POOL)]


def test_other_numeric_selection_cannot_shrink_late_strength_sixty_percent_denominator(tmp_path):
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"], "timeframes": ["1m"], "execution_backend": "minute_filter"}
BASIC_FILTER = {"enabled": True, "target_mode": "all", "data_reference": "previous_trade_day",
                "float_cap_min": 5e9, "float_cap_max": 50e9, "price_min": 11}
def filter_minute_history(df, params):
    return df.group_by("symbol").agg(pl.col("datetime").max().alias("last_datetime"))
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, definition(pool_up_min_pct=60.01))
    # All five meet float cap; only three rising stocks meet the price filter.
    prior = pl.DataFrame({
        "symbol": POOL, "date": [DAY-timedelta(days=1)]*5,
        "close": [12.0]*3 + [10.0]*2, "raw_close": [12.0]*3 + [10.0]*2,
        "float_shares": [1e9]*5,
    })
    minute = bars().with_columns(
        pl.when(pl.col("symbol").is_in(POOL[:3])).then(pl.col("close")*1.2)
        .otherwise(pl.col("close")).alias("close"))
    context = StrategyDataContext("stock", "1m", DAY, history=minute,
        current=prior.with_columns(pl.lit(DAY).alias("date")), pool_history=prior)
    result = engine.run("sample", context, overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert result.daily_stock_pool["symbols"] == POOL
    assert result.daily_stock_pool["definition_version"] == "scope_float_cap_v2"
    assert result.entry_signal_hits == []  # 3/5=60%, even though selected candidates are all up.
