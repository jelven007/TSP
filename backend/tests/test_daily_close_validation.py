"""Auction finality regressions using the November 4 price incident."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy.engine import StrategyDataContext
from app.strategy.late_session_strength import previous_closes

DAY = date(2026, 11, 4)
NEXT = date(2026, 11, 5)
SYMBOL = "601799.SH"


def daily(price=68.30, *, basis=None, stamp=None, day=DAY):
    return pl.DataFrame({
        "symbol": [SYMBOL], "date": [day], "close": [price],
        "raw_close": [price], "volume": [100.], "amount": [1000.],
        "quote_time_basis": pl.Series([basis], dtype=pl.String),
        "quote_ts": pl.Series([stamp], dtype=pl.Int64),
    })


def ms(day=DAY, hour=15, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ).timestamp() * 1000)


def test_received_clock_cannot_supply_next_session_reference():
    frame = daily(68.39, basis="received", stamp=ms(minute=6))
    context = StrategyDataContext("stock", "1m", NEXT, current=frame, daily_history=frame)
    with pytest.raises(ValueError, match=r"601799\.SH"):
        previous_closes(context)


def test_previous_reference_never_falls_back_to_an_older_session():
    frame = daily(day=date(2026, 11, 3))
    context = StrategyDataContext("stock", "1m", NEXT, current=frame, daily_history=frame)
    with pytest.raises(ValueError, match="2026-11-04"):
        previous_closes(context)


def test_preselection_rejects_unfinalized_reference_before_filtering():
    from app.strategy.signal_sections import select_rows

    frame = daily(68.39, basis="received", stamp=ms(minute=6))
    engine = SimpleNamespace(
        _resolve_filter_scope=lambda config: config,
        _apply_basic_filter=lambda rows, *a, **kw: rows,
        _apply_scoring=lambda rows, *a: rows,
    )
    strategy = SimpleNamespace(basic_filter={"data_reference": "previous_trade_day"}, meta={})
    context = StrategyDataContext("stock", "1m", NEXT, current=frame, daily_history=frame)
    with pytest.raises(ValueError, match=r"601799\.SH"):
        select_rows(engine, strategy, context, overrides={})


def repository(tmp_path, *, raw=None, closing=None):
    from app.tickflow.repository import KlineRepository

    for table, frame in (
        ("kline_daily", raw if raw is not None else daily()),
        ("kline_minute", closing if closing is not None else pl.DataFrame({
            "symbol": [SYMBOL], "datetime": [datetime(2026, 11, 4, 15)], "close": [68.30],
        })),
    ):
        folder = tmp_path / table / f"date={DAY}"
        folder.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(folder / "part.parquet")
    repo = object.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    repo.get_suspension_history = lambda: pl.DataFrame()
    return repo


def test_valid_raw_and_reference_match_unique_auction_close(tmp_path):
    from app.services.daily_close_validation import validate_daily_close

    validate_daily_close(repository(tmp_path), DAY, rows=daily())
    context = StrategyDataContext("stock", "1m", NEXT, current=daily(), daily_history=daily())
    assert previous_closes(context)[SYMBOL] == 68.30


@pytest.mark.parametrize("raw,reference", [
    (daily(68.39), None),
    (daily(), daily(68.39)),
    (daily(68.39, basis="received", stamp=ms(minute=6)), daily(68.39)),
])
def test_auction_or_stale_enriched_mismatch_is_explicit(tmp_path, raw, reference):
    from app.services.daily_close_validation import validate_daily_close

    with pytest.raises(ValueError, match=r"601799\.SH"):
        validate_daily_close(repository(tmp_path, raw=raw), DAY, rows=reference)


@pytest.mark.parametrize("closing", [
    pl.DataFrame({"symbol": [SYMBOL], "datetime": [datetime(2026, 11, 4, 14, 59)], "close": [68.39]}),
    pl.DataFrame({"symbol": [SYMBOL] * 2, "datetime": [datetime(2026, 11, 4, 15)] * 2,
                  "close": [68.30] * 2}),
    pl.DataFrame({"symbol": [SYMBOL], "datetime": [datetime(2026, 11, 4, 15)], "close": [None]}),
])
def test_missing_duplicate_or_invalid_closing_minute_fails(tmp_path, closing):
    from app.services.daily_close_validation import validate_daily_close

    with pytest.raises(ValueError, match=r"601799\.SH"):
        validate_daily_close(repository(tmp_path, closing=closing), DAY)


@pytest.mark.parametrize("frame", [
    daily(basis="received", stamp=ms(minute=6)),
    daily(basis="source_event", stamp=ms()),
    daily(basis="unknown", stamp=ms()),
    daily(basis="source_snapshot", stamp=ms(hour=14, minute=59)),
    daily(basis="source_snapshot", stamp=ms(day=NEXT)),
    daily(basis="source_snapshot"),
    daily(stamp=ms(minute=6)),
    daily(day=date(2026, 11, 3)),
])
def test_invalid_clock_or_wrong_date_never_certifies(frame):
    from app.services.daily_close_validation import require_final_daily_rows

    with pytest.raises(ValueError, match="2026-11-04"):
        require_final_daily_rows(frame, DAY)


def test_batch_and_same_day_source_snapshot_contracts():
    from app.services.daily_close_validation import require_final_daily_rows

    require_final_daily_rows(daily().drop("quote_ts", "quote_time_basis"), DAY)
    require_final_daily_rows(daily(), DAY)
    require_final_daily_rows(daily(basis="source_snapshot", stamp=ms()), DAY)


def test_replaced_partition_is_not_hidden_by_cached_validation(tmp_path):
    from app.services.daily_close_validation import validate_daily_close

    repo = repository(tmp_path)
    validate_daily_close(repo, DAY)
    daily(68.39).write_parquet(tmp_path / "kline_daily" / f"date={DAY}" / "part.parquet")
    with pytest.raises(ValueError, match=r"601799\.SH"):
        validate_daily_close(repo, DAY)


def test_zero_volume_is_not_suspension_evidence(tmp_path):
    from app.services.daily_close_validation import validate_daily_close

    empty = pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime, "close": pl.Float64})
    repo = repository(tmp_path, raw=daily().with_columns(pl.lit(0.).alias("volume")), closing=empty)
    with pytest.raises(ValueError, match=r"601799\.SH"):
        validate_daily_close(repo, DAY)


def test_confirmed_suspension_allows_absent_auction_without_inventing_price(tmp_path):
    from app.services.daily_close_validation import validate_daily_close

    empty = pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime, "close": pl.Float64})
    repo = repository(tmp_path, closing=empty)
    repo.get_suspension_history = lambda: pl.DataFrame({
        "symbol": [SYMBOL], "effective_date": [DAY], "announced_on": [date(2026, 11, 3)],
        "is_suspended": [True], "source_url": ["https://example.org/official"],
    })
    validate_daily_close(repo, DAY)


def test_pipeline_rejects_completed_received_clock_and_leaves_intraday_provisional(tmp_path):
    from app.services.daily_close_validation import validate_completed_pipeline

    repo = repository(tmp_path, raw=daily(68.39, basis="received", stamp=ms(minute=6)))
    validate_completed_pipeline(repo, DAY, DAY, now=datetime(2026, 11, 4, 14, 59))
    with pytest.raises(ValueError, match=r"601799\.SH"):
        validate_completed_pipeline(repo, DAY, DAY, now=datetime(2026, 11, 4, 15, 35))


@pytest.mark.parametrize("basis,valid", [(None, True), ("received", False)])
def test_initial_history_outside_minute_window_keeps_official_batch_contract(tmp_path, basis, valid):
    from app.services.daily_close_validation import validate_completed_pipeline

    older = date(2026, 10, 20)
    part = tmp_path / "kline_daily" / f"date={older}"
    part.mkdir(parents=True)
    daily(day=older, basis=basis, stamp=None if valid else ms(older)).write_parquet(part / "part.parquet")
    repo = repository(tmp_path)
    if valid:
        validate_completed_pipeline(repo, older, DAY, now=datetime(2026, 11, 4, 16), minute_start=DAY)
    else:
        with pytest.raises(ValueError, match="2026-10-20"):
            validate_completed_pipeline(repo, older, DAY, now=datetime(2026, 11, 4, 16), minute_start=DAY)


def test_screener_rejects_stale_enriched_before_minute_loading(tmp_path, monkeypatch):
    from app.services.screener import ScreenerService

    repo = repository(tmp_path)
    repo.get_instruments_asset = lambda *_: pl.DataFrame()
    service = ScreenerService(repo)
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *_: daily().with_columns(pl.lit(NEXT).alias("date")))
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: daily(68.39))
    with pytest.raises(ValueError, match="加工/策略昨收"):
        service.build_strategy_context(None, NEXT, [], timeframe="1m")


@pytest.mark.parametrize("frame", [
    daily(68.39, basis="received", stamp=ms(minute=6)),
    daily(day=date(2026, 11, 3)),
    daily(68.39),
])
def test_replay_records_reference_error_and_generates_no_entry(tmp_path, frame):
    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.strategy.engine import StrategyEngine

    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "test.py").write_text("""
import polars as pl
META = {"id": "close_test", "asset_types": ["stock"], "timeframes": ["1m"]}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"enabled": False}
def filter_minute_history(df, params):
    return df.rename({"datetime": "last_datetime"})
""")
    engine = StrategyEngine([strategy_dir])
    repo = repository(tmp_path)
    repo.list_minute_dates = lambda *_: [NEXT]
    repo.get_minute_by_dates = lambda *_: pl.DataFrame({
        "symbol": [SYMBOL], "datetime": [datetime(2026, 11, 5, 14, 30)], "close": [71.11],
    })
    panel = pl.concat([frame, daily(day=NEXT)], how="vertical_relaxed")
    result = MinuteSignalReplayer(SimpleNamespace(repo=repo), engine).replay(
        engine.get("close_test"), panel=panel, start=NEXT, end=NEXT, params={}, overrides={},
    )
    assert result.errors and "2026-11-04" in result.errors[0]
    assert not result.hits and result.replayed_days == 0


def test_minute_panel_keeps_finality_provenance():
    from app.backtest.minute_replay import minute_replay_feature_plan

    assert {"quote_ts", "quote_time_basis"} <= minute_replay_feature_plan(1).base_columns
