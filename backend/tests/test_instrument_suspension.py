"""Dated official halts must work without a fabricated current price row."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.screener import ScreenerService
from app.strategy.daily_pool import build_pool
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 6, 29)
PRIOR = date(2026, 6, 26)
RESUME = date(2026, 7, 13)
HALTED = "601369.SH"
ACTIVE = "600000.SH"
BF = {"enabled": True, "target_mode": "selected", "boards": ["沪主板"],
      "data_reference": "previous_trade_day", "float_cap_max": 20e9,
      "market_cap_min": None, "exclude_new_days": 0, "exclude_suspended": True}


def events():
    return pl.DataFrame([
        {"symbol": HALTED, "effective_date": DAY, "announced_on": date(2026, 6, 27),
         "is_suspended": True, "source_url": "https://www.sse.com.cn/halt.pdf"},
        {"symbol": HALTED, "effective_date": RESUME, "announced_on": date(2026, 7, 11),
         "is_suspended": False, "source_url": "https://www.sse.com.cn/resume.pdf"},
    ])


def prices():
    return pl.DataFrame([
        {"symbol": symbol, "date": day, "close": 10.0, "raw_close": 10.0,
         "float_shares": 1e9, "volume": 100.0, "amount": 2e8}
        for symbol, day in [(ACTIVE, PRIOR), (HALTED, PRIOR), (ACTIVE, DAY)]
    ])


@pytest.fixture
def engine(tmp_path):
    custom = tmp_path / "strategies/custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"],
        "timeframes": ["1m"], "execution_backend": "minute_filter", "daily_history_bars": 2}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params, *, daily=None):
    return df.sort("datetime").group_by("symbol").last().rename({"datetime": "last_datetime"})
''')
    return StrategyEngine([custom])


@pytest.mark.parametrize("enabled", [True, False])
def test_known_halt_without_current_price_is_excluded_but_unknown_is_retained(engine, enabled):
    rows = prices()
    evidence = pl.DataFrame({
        "symbol": [HALTED], "_suspension_date": [DAY], "_suspension_status": [True],
    })
    context = StrategyDataContext("stock", "1d", DAY,
        current=rows.filter(pl.col("date") == DAY), pool_history=rows, pool_universe=evidence)
    config = {**BF, "enabled": enabled}
    pool = build_pool(engine, "sample", context, overrides={"basic_filter": config})
    assert pool["symbols"] == [ACTIVE]
    assert pool["suspended_count"] == 1
    assert pool["unknown_suspension_count"] == 0
    unchecked = build_pool(engine, "sample", context,
        overrides={"basic_filter": {**config, "exclude_suspended": False}})
    assert unchecked["symbols"] == [ACTIVE, HALTED]
    context.pool_universe = None
    unknown = build_pool(engine, "sample", context, overrides={"basic_filter": config})
    assert unknown["symbols"] == [ACTIVE, HALTED]
    assert unknown["unknown_suspension_count"] == 1


def test_status_only_evidence_does_not_invent_pool_members(engine):
    rows = prices().filter(pl.col("symbol") == ACTIVE)
    context = StrategyDataContext("stock", "1d", DAY,
        current=rows.filter(pl.col("date") == DAY), pool_history=rows,
        pool_universe=pl.DataFrame({
            "symbol": [HALTED], "_suspension_date": [DAY], "_suspension_status": [False],
        }))
    pool = build_pool(engine, "sample", context, overrides={
        "basic_filter": {"enabled": False, "target_mode": "all"},
    })
    assert pool["symbols"] == [ACTIVE]


def test_suspension_event_loader_dates_cache_and_atomic_replacement(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    try:
        assert repo.get_suspension_history().is_empty()
        path = tmp_path / "instrument_status/suspensions.parquet"
        path.parent.mkdir()
        events().write_parquet(path)
        first = repo.get_suspension_history()
        assert first.height == 2
        assert repo.get_suspension_history() is first
        temporary = path.with_suffix(".new")
        events().head(1).write_parquet(temporary)
        temporary.replace(path)
        assert repo.get_suspension_history().height == 1
    finally:
        repo.db.close()


@pytest.mark.parametrize("day,status", [
    (PRIOR, None), (DAY, True), (date(2026, 7, 10), True), (RESUME, False),
])
def test_event_effective_and_announcement_dates_prevent_future_resume(day, status):
    from app.instrument_suspension import dated_status

    result = dated_status(events(), day)
    assert result.is_empty() if status is None else result["_suspension_status"].to_list() == [status]
    if status is not None:
        assert result["_suspension_date"].to_list() == [day]


def test_same_day_announcement_is_not_assumed_available_before_signal():
    from app.instrument_suspension import dated_status

    event = events().head(1).with_columns(pl.lit(DAY).alias("announced_on"))
    assert dated_status(event, DAY).is_empty()
    assert dated_status(event, DAY + timedelta(days=1))["_suspension_status"].to_list() == [True]


@pytest.mark.parametrize("mutation", ["missing_source", "null_state", "duplicate_conflict"])
def test_malformed_evidence_fails_explicitly(tmp_path, mutation):
    from app.instrument_suspension import load_suspension_history

    frame = events()
    if mutation == "missing_source":
        frame = frame.drop("source_url")
    elif mutation == "null_state":
        frame = frame.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_suspended"))
    else:
        frame = pl.concat([frame, frame.head(1).with_columns(pl.lit(False).alias("is_suspended"))])
    path = tmp_path / "instrument_status/suspensions.parquet"
    path.parent.mkdir()
    frame.write_parquet(path)
    with pytest.raises(ValueError, match="停复牌"):
        load_suspension_history(tmp_path)


@pytest.mark.parametrize("premarket", [False, True])
def test_screener_attaches_dated_halt_without_replacing_listing_metadata(engine, monkeypatch, premarket):
    rows = prices()
    instruments = pl.DataFrame({"symbol": [ACTIVE, HALTED], "name": ["示例", "陕鼓动力"]})
    repo = SimpleNamespace(get_instruments_asset=lambda _: instruments,
                           get_suspension_history=events)
    service = ScreenerService(repo)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: rows)
    monkeypatch.setattr(service, "_load_enriched_for_date",
                        lambda *_: rows.filter(pl.col("date") == DAY))
    context = service.build_daily_pool_context(DAY, premarket=premarket)
    pool = build_pool(engine, "sample", context, premarket=premarket, overrides={"basic_filter": BF})
    assert pool["symbols"] == [ACTIVE]
    assert pool["suspended_count"] == 1
    if premarket:
        assert context.current.filter(pl.col("symbol") == HALTED)["name"].item() == "陕鼓动力"


def test_minute_replay_halt_exclusion_and_unknown_quote_diagnostic(engine, tmp_path):
    from test_late_session_strength import bars, definition

    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.strategy import custom_signals

    minute = bars().filter(pl.col("symbol") == "a").with_columns(
        pl.lit(ACTIVE).alias("symbol"),
        pl.col("datetime").dt.offset_by("-67d"),
    )
    custom_signals.save_one(tmp_path, definition())
    engine._user_data_dir = lambda: tmp_path
    repo = SimpleNamespace(
        get_suspension_history=events,
        list_minute_dates=lambda *_: [DAY],
        get_minute_by_dates=lambda *_: minute,
    )
    replayer = MinuteSignalReplayer(SimpleNamespace(repo=repo), engine)
    overrides = {"basic_filter": BF, "entry_signals": ["csgi_late_session_strength"]}
    panel = prices()
    fixed = replayer.replay(engine.get("sample"), panel=panel, start=DAY, end=DAY,
                            params={}, overrides=overrides)
    assert fixed.errors == []
    assert [hit.symbol for hit in fixed.hits] == [ACTIVE]
    unchecked = replayer.replay(engine.get("sample"), panel=panel, start=DAY, end=DAY,
        params={}, overrides={**overrides, "basic_filter": {**BF, "exclude_suspended": False}})
    assert any("1/2" in message for message in unchecked.errors)
    repo.get_suspension_history = lambda: events().clear()
    unknown = replayer.replay(engine.get("sample"), panel=panel, start=DAY, end=DAY,
                              params={}, overrides=overrides)
    assert any("1/2" in message for message in unknown.errors)


def test_research_fingerprint_includes_suspension_events(tmp_path):
    from app.services.data_release import research_input_fingerprints

    before = research_input_fingerprints(tmp_path)
    path = tmp_path / "instrument_status/suspensions.parquet"
    path.parent.mkdir()
    events().write_parquet(path)
    after = research_input_fingerprints(tmp_path)
    assert before != after
    assert len(after["files"]["instrument_status/suspensions.parquet"]["sha256"]) == 64


@pytest.mark.parametrize("clock,known", [("08:00", False), ("09:00", False),
                                       ("09:28", False), ("09:29", True), ("14:30", True)])
def test_exact_publication_time_is_consumed_only_after_observation(clock, known):
    from app.instrument_suspension import dated_status

    event = events().head(1).with_columns(
        pl.lit(DAY).alias("announced_on"),
        pl.lit(datetime(2026, 6, 29, 9, 29)).alias("announced_at"))
    observed = datetime.fromisoformat(f"{DAY} {clock}")
    result = dated_status(event, DAY, observed_at=observed)
    assert result.height == int(known)
    assert dated_status(event, DAY).is_empty()
    assert dated_status(event, DAY, observed_at=observed.replace(
        tzinfo=timezone(timedelta(hours=8)))).equals(result)


def test_optional_timestamp_loader_preserves_old_schema_and_rejects_wrong_date(tmp_path):
    from app.instrument_suspension import load_suspension_history

    path = tmp_path / "instrument_status/suspensions.parquet"
    path.parent.mkdir()
    events().write_parquet(path)
    assert load_suspension_history(tmp_path)["announced_at"].null_count() == 2
    events().with_columns(pl.lit(datetime(2026, 6, 28, 9)).alias("announced_at")).write_parquet(path)
    with pytest.raises(ValueError, match="停复牌"):
        load_suspension_history(tmp_path)


@pytest.mark.parametrize("published,error,delay", [
    ("09:00:00", False, 0), ("15:00:00", True, 0),
    ("14:30:30", True, 0), ("14:30:30", False, 1), ("14:31:01", True, 1),
    ("14:31:01", False, 1.5), ("14:31:30", False, 1.5), ("14:31:31", True, 1.5),
])
def test_replay_uses_signal_time_not_end_of_day_for_suspension(engine, tmp_path, published, error, delay):
    from test_late_session_strength import TARGET, bars, definition

    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.strategy import custom_signals

    minute = bars(TARGET).filter(pl.col("symbol") == "a").with_columns(
        pl.lit(ACTIVE).alias("symbol"), pl.col("datetime").dt.offset_by("-67d"))
    custom_signals.save_one(tmp_path, definition(**({"evaluation_delay_minutes": delay} if delay else {})))
    engine._user_data_dir = lambda: tmp_path
    event = events().head(1).with_columns(
        pl.lit(DAY).alias("announced_on"),
        pl.lit(datetime.fromisoformat(f"{DAY} {published}")).alias("announced_at"))
    repo = SimpleNamespace(get_suspension_history=lambda: event,
                           list_minute_dates=lambda *_: [DAY],
                           get_minute_by_dates=lambda *_: minute)
    result = MinuteSignalReplayer(SimpleNamespace(repo=repo), engine).replay(
        engine.get("sample"), panel=prices(), start=DAY, end=DAY, params={},
        overrides={"basic_filter": BF, "entry_signals": ["csgi_late_session_strength"]})
    assert bool(result.errors) is error


@pytest.mark.parametrize("clock,excluded", [("08:00", False), ("09:00", True)])
def test_premarket_pool_honors_each_batch_clock(engine, monkeypatch, clock, excluded):
    event = events().head(1).with_columns(
        pl.lit(DAY).alias("announced_on"),
        pl.lit(datetime(2026, 6, 29, 8, 23)).alias("announced_at"))
    repo = SimpleNamespace(get_instruments_asset=lambda _: pl.DataFrame({
        "symbol": [ACTIVE, HALTED]}), get_suspension_history=lambda: event)
    service = ScreenerService(repo)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: prices())
    context = service.build_daily_pool_context(
        DAY, premarket=True, observed_at=datetime.fromisoformat(f"{DAY} {clock}"))
    pool = build_pool(engine, "sample", context, premarket=True, overrides={"basic_filter": BF})
    assert (HALTED not in pool["symbols"]) is excluded
