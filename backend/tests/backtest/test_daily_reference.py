"""Replay uses completed T-1 daily prices without requiring T-1 minute bars."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.minute_replay import MinuteSignalReplayer
from app.market_time import CN_TZ
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import KlineRepository

PREVIOUS = date(2026, 6, 12)
START = date(2026, 6, 15)
SYMBOL = "300001.SZ"


def _daily():
    return pl.DataFrame({"symbol": [SYMBOL], "date": [PREVIOUS], "close": [35.75]})


@pytest.fixture
def scenario(tmp_path):
    daily_dir = tmp_path / "kline_daily" / f"date={PREVIOUS}"
    daily_dir.mkdir(parents=True)
    _daily().write_parquet(daily_dir / "part.parquet")
    repo = object.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    repo.get_suspension_history = lambda: pl.DataFrame()
    repo.list_minute_dates = lambda *_: [START]

    def current_minutes(symbols, dates, asset_type):
        assert dates == [START] and asset_type == "stock"
        return pl.DataFrame({
            "symbol": [SYMBOL], "datetime": [datetime(2026, 6, 15, 14, 30)],
            "close": [36.20],
        })

    repo.get_minute_by_dates = current_minutes
    panel = pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL], "date": [PREVIOUS, START],
        "close": [17.875, 18.50], "raw_close": [35.75, 37.00],
        "volume": [100., 100.], "amount": [1000., 1000.],
    })
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "daily_reference.py").write_text("""
import polars as pl
META = {"id": "daily_reference", "asset_types": ["stock"], "timeframes": ["1m"],
        "daily_history_bars": 1}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"enabled": False}
def filter_minute_history(df, params, *, daily=None):
    assert daily["date"].max() < df["datetime"].min().date()
    assert daily["raw_close"][-1] == 35.75
    return df.rename({"datetime": "last_datetime"})
""")
    engine = StrategyEngine([strategy_dir])
    return repo, panel, engine


def _replay(scenario, *, panel=None):
    repo, original, engine = scenario
    return MinuteSignalReplayer(SimpleNamespace(repo=repo), engine).replay(
        engine.get("daily_reference"), panel=original if panel is None else panel,
        start=START, end=START, params={}, overrides={},
    )


@pytest.mark.parametrize("minute_state", [
    "absent", "empty", "mismatch", "duplicate", "invalid_schema", "corrupt",
])
def test_replay_uses_daily_close_independent_of_prior_minutes(scenario, tmp_path, minute_state):
    if minute_state != "absent":
        minute_dir = tmp_path / "kline_minute" / f"date={PREVIOUS}"
        minute_dir.mkdir(parents=True)
        path = minute_dir / "part.parquet"
        closing = pl.DataFrame({
            "symbol": [SYMBOL], "datetime": [datetime(2026, 6, 12, 15)],
            "close": [99.00 if minute_state == "mismatch" else 35.75],
        })
        if minute_state == "corrupt":
            path.write_bytes(b"invalid parquet")
        else:
            if minute_state == "empty":
                closing = closing.head(0)
            elif minute_state == "duplicate":
                closing = pl.concat([closing, closing])
            elif minute_state == "invalid_schema":
                closing = closing.drop("close")
            closing.write_parquet(path)

    result = _replay(scenario)
    assert not result.errors, result.errors
    assert result.replayed_days == 1 and not result.skipped_days
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.prior_close == 35.75
    assert hit.raw_entry_price == 36.20
    assert hit.entry_price == pytest.approx(18.10)
    assert hit.entry_window_start == datetime(2026, 6, 15, 14, 31)


def test_replay_never_requests_combined_daily_and_minute_evidence(scenario):
    repo, _, _ = scenario
    original = repo.get_daily_close_inputs
    calls = []

    def daily_only(day, *, include_closing_minute=True):
        assert not include_closing_minute
        calls.append(day)
        return original(day, include_closing_minute=include_closing_minute)

    repo.get_daily_close_inputs = daily_only
    result = _replay(scenario)
    assert not result.errors and len(result.hits) == 1
    assert calls == [PREVIOUS]


@pytest.mark.parametrize("daily_state", [
    "absent", "empty", "wrong_date", "received", "intraday_snapshot",
    "invalid_price", "zero_price", "duplicate", "missing_symbol",
])
def test_replay_still_rejects_invalid_original_daily_reference(scenario, tmp_path, daily_state):
    path = tmp_path / "kline_daily" / f"date={PREVIOUS}" / "part.parquet"
    raw = _daily()
    if daily_state == "absent":
        path.unlink()
    else:
        if daily_state == "empty":
            raw = raw.head(0)
        elif daily_state == "wrong_date":
            raw = raw.with_columns(pl.lit(date(2026, 6, 11)).alias("date"))
        elif daily_state in {"received", "intraday_snapshot"}:
            stamp = datetime(2026, 6, 12, 14, 59, tzinfo=CN_TZ)
            raw = raw.with_columns(
                pl.lit("received" if daily_state == "received" else "source_snapshot")
                .alias("quote_time_basis"),
                pl.lit(int(stamp.timestamp() * 1000)).alias("quote_ts"),
            )
        elif daily_state in {"invalid_price", "zero_price"}:
            raw = raw.with_columns(
                pl.lit(float("nan") if daily_state == "invalid_price" else 0.).alias("close"),
            )
        elif daily_state == "duplicate":
            raw = pl.concat([raw, raw])
        elif daily_state == "missing_symbol":
            raw = raw.with_columns(pl.lit("600000.SH").alias("symbol"))
        raw.write_parquet(path)

    result = _replay(scenario)
    assert result.errors and str(PREVIOUS) in result.errors[0]
    assert result.replayed_days == 0 and not result.hits


@pytest.mark.parametrize("panel_state", ["wrong_date", "received", "price_conflict"])
def test_replay_still_rejects_invalid_processed_daily_reference(scenario, panel_state):
    _, panel, _ = scenario
    prior = panel.filter(pl.col("date") == PREVIOUS)
    current = panel.filter(pl.col("date") == START)
    if panel_state == "wrong_date":
        prior = prior.with_columns(pl.lit(date(2026, 6, 11)).alias("date"))
    elif panel_state == "received":
        prior = prior.with_columns(
            pl.lit("received").alias("quote_time_basis"),
            pl.lit(int(datetime(2026, 6, 12, 16, tzinfo=CN_TZ).timestamp() * 1000))
            .alias("quote_ts"),
        )
    else:
        prior = prior.with_columns(pl.lit(35.80).alias("raw_close"))
    result = _replay(scenario, panel=pl.concat([prior, current], how="diagonal_relaxed"))
    assert result.errors and str(PREVIOUS) in result.errors[0]
    assert result.replayed_days == 0 and not result.hits
