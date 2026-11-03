"""90秒确认完成的启动分钟; 秒级确认不得变成提前一分钟成交。"""
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl
import pytest
from test_late_session_strength import DAY, POOL, TARGET, bars, definition, evaluate

CHECK = TARGET + timedelta(seconds=90)


def delayed(**params):
    return definition(**{"evaluation_delay_minutes": 1.5, **params})


def test_seconds_gate_and_cutoff_ignore_later_prices():
    from app.strategy.late_session_strength import DEFAULT_PARAMS, bar_time, evaluation_time

    sig = delayed()
    assert DEFAULT_PARAMS["evaluation_delay_minutes"] == 1.5
    assert evaluation_time(sig, DAY) == CHECK
    assert bar_time(sig, DAY) == TARGET
    for stamp in [TARGET, TARGET+timedelta(seconds=65), CHECK-timedelta(seconds=1)]:
        assert evaluate(sig=sig, completed_through=stamp).symbols == []
    future = bars().filter(pl.col("datetime") == TARGET).with_columns(
        (pl.col("datetime")+pl.duration(minutes=1)).alias("datetime"),
        pl.lit(5.).alias("close"))
    frame = pl.concat([bars(), future])
    result = evaluate(frame=frame, sig=sig, completed_through=CHECK)
    assert result.symbols == POOL[:3] and result.pool_up_pct == 60
    utc = CHECK.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    assert evaluate(frame=frame, sig=sig, completed_through=utc).symbols == POOL[:3]
    assert evaluate(frame=frame.filter(pl.col("datetime") != TARGET),
                    sig=sig, completed_through=CHECK).diagnostics
    assert evaluate(frame=frame, sig=delayed(pool_up_min_pct=60.01)).symbols == []


def test_both_ma_windows_still_end_at_launch():
    early = bars().with_columns(
        pl.when((pl.col("symbol") == "a") & (pl.col("datetime") == TARGET-timedelta(minutes=10)))
        .then(10.2).otherwise(pl.col("close")).alias("close"))
    assert "a" not in evaluate(frame=early, sig=delayed()).symbols
    assert "a" in evaluate(frame=early, sig=delayed(ma5_1m_count=6)).symbols
    five = bars().with_columns(
        pl.when((pl.col("symbol") == "a") & (pl.col("datetime") == TARGET-timedelta(minutes=40)))
        .then(10.3).otherwise(pl.col("close")).alias("close"))
    assert "a" in evaluate(frame=five, sig=delayed()).symbols
    assert "a" not in evaluate(frame=five, sig=delayed(ma5_5m_count=5)).symbols


def test_engine_records_seconds_on_launch_data_and_pool_observation(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine
    from app.strategy.late_session_strength import pool_cutoff

    custom_signals.save_one(tmp_path, delayed())
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    previous = pl.DataFrame({"symbol": POOL, "date": [DAY-timedelta(days=1)]*5, "close": [10.]*5})
    context = StrategyDataContext("stock", "1m", DAY, history=bars(), pool_history=previous,
                                  observed_at=CHECK-timedelta(seconds=1))
    column = "csgi_late_session_strength"
    assert not engine._inject_intraday_signal_columns(
        bars(), context=context, pool_symbols=POOL)[column].any()
    context.observed_at = CHECK
    hits = engine._inject_intraday_signal_columns(
        bars(), context=context, pool_symbols=POOL).filter(pl.col(column))
    assert hits["datetime"].to_list() == [TARGET]*3
    assert hits["entry_evaluated_at"].to_list() == [CHECK]*3
    assert all(hit["evaluated_at"] == CHECK
               for hit in engine._collect_signal_hits(hits, [column]))
    engine.get = lambda _: SimpleNamespace(entry_signals=[column], required_features=[])
    assert pool_cutoff(engine, ["s"], {}, DAY, CHECK+timedelta(hours=1)) == CHECK
    assert pool_cutoff(engine, ["s"], {}, DAY, TARGET) == TARGET


@pytest.mark.parametrize("mode,missing", [
    ("next_minute_open", False), ("next_minute_open", True), ("signal_close", False),
])
def test_replay_seconds_are_preserved_and_execution_ceil_is_causal(tmp_path, mode, missing):
    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text(f'''
import polars as pl
META = {{"id": "sample", "name": "sample", "asset_types": ["stock"], "timeframes": ["1m"],
        "execution_backend": "minute_filter", "minute_entry_fill": "{mode}"}}
BASIC_FILTER = {{"enabled": False, "target_mode": "all"}}
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_late_session_strength")).rename({{"datetime": "last_datetime"}})
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, delayed())
    future = [bars()]
    for offset, price in [(1, 10.18), (2, 10.22)]:
        if missing and offset == 2:
            continue
        future.append(bars().filter(pl.col("datetime") == TARGET).with_columns(
            (pl.col("datetime")+pl.duration(minutes=offset)).alias("datetime"),
            pl.lit(price).alias("open"), pl.lit(5.).alias("close")))
    frame = pl.concat(future)
    calls = []

    def minute(symbols, *_):
        calls.append(set(symbols))
        return frame.filter(pl.col("symbol").is_in(symbols))

    repo = SimpleNamespace(list_minute_dates=lambda *_: [DAY], get_minute_by_dates=minute)
    panel = pl.DataFrame({"symbol": POOL*2, "date": [DAY-timedelta(days=1)]*5+[DAY]*5,
                          "close": [10.]*10, "raw_close": [10.]*10, "name": ["sample"]*10})
    result = MinuteSignalReplayer(SimpleNamespace(repo=repo), engine).replay(
        engine.get("sample"), panel=panel, start=DAY, end=DAY, params={}, symbols=["a"],
        overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert calls == [set(POOL)]
    assert not result.errors
    if missing:
        assert not result.hits and result.buy_no_next_minute == 1
    else:
        assert len(result.hits) == 1
        hit = result.hits[0]
        assert hit.trigger_time == "14:32" and hit.signal_time == "14:31:30"
        assert hit.raw_entry_price == pytest.approx(10.22 if mode == "next_minute_open" else 10.159)


@pytest.mark.parametrize("start,valid", [("14:58:00", True), ("14:59:00", False), ("15:00:00", False)])
def test_launch_must_leave_ninety_seconds_before_close(start, valid):
    from app.strategy import custom_signals
    from app.strategy.late_session_strength import evaluation_time

    if valid:
        custom_signals.validate(delayed(time=start))
        assert evaluation_time(delayed(time=start), DAY) == datetime(2026, 9, 4, 14, 59, 30)
    else:
        with pytest.raises(ValueError):
            custom_signals.validate(delayed(time=start))
    custom_signals.validate(definition(time="14:59:00", evaluation_delay_minutes=1))


@pytest.mark.parametrize("delay", [True, "1.5", 1.25, float("nan"), float("inf")])
def test_delay_accepts_only_frozen_contracts_and_ninety_seconds(delay):
    from app.strategy import custom_signals

    with pytest.raises(ValueError):
        custom_signals.validate(delayed(evaluation_delay_minutes=delay))
