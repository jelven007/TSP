"""启动后一分钟的新契约, 冻结六字段定义仍使用旧检查分钟。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_late_session_strength import DAY, POOL, TARGET, bars, definition, evaluate

CHECK = TARGET + timedelta(minutes=1)


def delayed(**params):
    return definition(**{"evaluation_delay_minutes": 1, **params})


def test_waits_for_completed_check_minute_and_ignores_future():
    sig = delayed()
    assert evaluate(frame=bars(), sig=sig, completed_through=TARGET).symbols == []
    assert evaluate(frame=bars(), sig=sig, completed_through=CHECK-timedelta(seconds=1)).symbols == []
    assert evaluate(frame=bars(), sig=sig, completed_through=CHECK).symbols == POOL[:3]
    future = bars().filter(pl.col("datetime") == TARGET).with_columns(
        (pl.col("datetime") + pl.duration(minutes=1)).alias("datetime"), pl.lit(5.).alias("close"))
    assert evaluate(frame=pl.concat([bars(), future]), sig=sig).symbols == POOL[:3]
    assert evaluate(frame=bars().filter(pl.col("datetime") != TARGET), sig=sig).diagnostics


def test_breadth_and_price_bounds_use_completed_launch_slice_full_pool():
    # The completed launch-labelled slice has three rising stocks; later all five rise.
    future = bars().filter(pl.col("datetime") == TARGET).with_columns(
        (pl.col("datetime") + pl.duration(minutes=1)).alias("datetime"), pl.lit(10.2).alias("close"))
    frame = pl.concat([bars(), future])
    result = evaluate(frame=frame, sig=delayed())
    assert result.pool_up_pct == 60
    assert result.symbols == POOL[:3]
    assert evaluate(frame=frame, sig=delayed(pool_up_min_pct=60.01)).symbols == []
    missing = frame.filter(~((pl.col("symbol") == "e") & (pl.col("datetime") == TARGET)))
    assert evaluate(frame=missing, sig=delayed()).diagnostics
    over = frame.with_columns(pl.when((pl.col("symbol") == "a") & (pl.col("datetime") == TARGET))
                             .then(10.401).otherwise(pl.col("close")).alias("close"))
    assert "a" not in evaluate(frame=over, sig=delayed()).symbols


def test_one_minute_seven_values_and_five_minute_four_values():
    # A drop in the unused MA5(14:10) does not add a fifth comparison.
    frame = bars().with_columns(
        pl.when((pl.col("symbol") == "a") & (pl.col("datetime") == TARGET-timedelta(minutes=40)))
        .then(10.3).otherwise(pl.col("close")).alias("close"))
    assert "a" in evaluate(frame=frame, sig=delayed()).symbols
    assert "a" not in evaluate(frame=frame, sig=delayed(ma5_5m_count=5)).symbols
    # MA5(14:30)==MA5(14:29) fails even if the unfinished 14:31 row shoots upward.
    price_1425 = bars().filter((pl.col("symbol") == "a")
                              & (pl.col("datetime") == TARGET-timedelta(minutes=5)))["close"].item()
    flat = bars().with_columns(
        pl.when((pl.col("symbol") == "a") & (pl.col("datetime") == TARGET))
        .then(price_1425).otherwise(pl.col("close")).alias("close"))
    assert "a" not in evaluate(frame=flat, sig=delayed()).symbols
    # Only the earliest of seven 1m MA5 values breaks the trend.
    early = bars().with_columns(
        pl.when((pl.col("symbol") == "a") & (pl.col("datetime") == TARGET-timedelta(minutes=10)))
        .then(10.2).otherwise(pl.col("close")).alias("close"))
    assert "a" not in evaluate(frame=early, sig=delayed()).symbols
    assert "a" in evaluate(frame=early, sig=delayed(ma5_1m_count=6)).symbols


def test_engine_ignores_unfinished_row_and_preserves_data_and_available_times(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    custom_signals.save_one(tmp_path, delayed())
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    future = bars().filter(pl.col("datetime") == TARGET).with_columns(
        (pl.col("datetime")+pl.duration(minutes=1)).alias("datetime"), pl.lit(5.).alias("close"))
    frame = pl.concat([bars(), future])
    previous = pl.DataFrame({"symbol": POOL, "date": [DAY-timedelta(days=1)]*5, "close": [10.]*5})
    context = StrategyDataContext("stock", "1m", DAY, history=frame, pool_history=previous,
                                  observed_at=CHECK-timedelta(seconds=1))
    assert not engine._inject_intraday_signal_columns(
        frame, context=context, pool_symbols=POOL)["csgi_late_session_strength"].any()
    context.observed_at = CHECK
    hits = engine._inject_intraday_signal_columns(
        frame, context=context, pool_symbols=POOL).filter(pl.col("csgi_late_session_strength"))
    assert hits["datetime"].to_list() == [TARGET]*3
    assert hits["entry_evaluated_at"].to_list() == [CHECK]*3
    assert hits["close"].to_list() == pytest.approx([10.159]*3)


def test_next_open_fill_is_available_minute_not_an_extra_minute(tmp_path):
    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "sample", "asset_types": ["stock"], "timeframes": ["1m"],
        "execution_backend": "minute_filter", "minute_entry_fill": "next_minute_open"}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_late_session_strength")).rename({"datetime": "last_datetime"})
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, delayed())
    future = bars().filter(pl.col("datetime") == TARGET).with_columns(
        (pl.col("datetime")+pl.duration(minutes=1)).alias("datetime"),
        pl.lit(10.18).alias("open"), pl.lit(10.35).alias("close"))
    frame = pl.concat([bars(), future])
    panel = pl.DataFrame({"symbol": POOL*2, "date": [DAY-timedelta(days=1)]*5+[DAY]*5,
                          "close": [10.]*10, "raw_close": [10.]*10, "name": ["sample"]*10})
    repo = SimpleNamespace(list_minute_dates=lambda *_: [DAY], get_minute_by_dates=lambda *_: frame)
    result = MinuteSignalReplayer(SimpleNamespace(repo=repo), engine).replay(
        engine.get("sample"), panel=panel, start=DAY, end=DAY, params={},
        overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert len(result.hits) == 3
    assert all(hit.trigger_time == "14:31" and hit.signal_time == "14:31" for hit in result.hits)
    assert all(hit.raw_entry_price == 10.18 for hit in result.hits)


@pytest.mark.parametrize("start,end", [("14:32:00", datetime(2026, 9, 4, 14, 32)),
                                     ("14:59:00", datetime(2026, 9, 4, 14, 59))])
def test_custom_launch_checks_next_minute(start, end):
    assert evaluate(frame=bars(end), sig=delayed(time=start),
                    completed_through=end+timedelta(minutes=1)).symbols == POOL[:3]


@pytest.mark.parametrize("patch", [{"evaluation_delay_minutes": True}, {"evaluation_delay_minutes": 2},
                                   {"time": "15:00:00"}, {"time": "14:30:01"}])
def test_new_contract_rejects_invalid_launch_or_delay(patch):
    from app.strategy import custom_signals

    with pytest.raises(ValueError):
        custom_signals.validate(delayed(**patch))


def test_pool_observation_uses_check_time_but_respects_earlier_observation(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.late_session_strength import pool_cutoff

    custom_signals.save_one(tmp_path, delayed())
    engine = SimpleNamespace(_user_data_dir=lambda: tmp_path, get=lambda _: SimpleNamespace(
        entry_signals=["csgi_late_session_strength"], required_features=[]))
    assert pool_cutoff(engine, ["s"], {}, DAY, TARGET+timedelta(hours=1)) == CHECK
    assert pool_cutoff(engine, ["s"], {}, DAY, TARGET) == TARGET
    custom_signals.save_one(tmp_path, definition())
    assert pool_cutoff(engine, ["s"], {}, DAY, CHECK) == TARGET


def test_http_save_upgrades_new_definition_but_loading_does_not(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router
    from app.strategy import custom_signals

    custom_signals.save_one(tmp_path, definition())
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    client = TestClient(app)
    assert "evaluation_delay_minutes" not in client.get("/api/custom-signals").json()["signals"][0]["params"]
    response = client.post("/api/custom-signals", json=definition())
    assert response.status_code == 200, response.text
    assert response.json()["signal"]["params"]["evaluation_delay_minutes"] == 1.5
    assert client.get("/api/custom-signals").json()["signals"][0]["params"]["evaluation_delay_minutes"] == 1.5
    assert client.post("/api/custom-signals", json=definition(time="15:00:00")).status_code == 400
