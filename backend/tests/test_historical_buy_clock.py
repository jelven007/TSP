"""Historical close availability must not expire the saved entry signal clock."""
from dataclasses import replace
from datetime import date, datetime, timedelta

import polars as pl
import pytest
from test_late_session_optional_params import signal

from app.market_time import CN_TZ
from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext, StrategyEngine

DAY = date(2026, 11, 12)


@pytest.fixture(params=[
    ("custom_mvxxcs01", "14:45:00", "14:55:00"),
    ("custom_mvxxcp88", "14:30:00", "14:55:00"),
    ("custom_mvxxcs03", "10:00:00", "11:30:00"),
])
def scenario(request, tmp_path):
    sid, start, end = request.param
    (tmp_path / "sample.py").write_text(f'''
import polars as pl
META={{"id":"{sid}","name":"{sid}","asset_types":["stock"],"timeframes":["1m"],
      "result_sections":"signals","scoring_mode":"strategy","scoring":{{}}}}
EXECUTION_BACKEND="minute_filter"
BASIC_FILTER={{"enabled":False,"target_mode":"all"}}
ENTRY_SIGNALS=["csgi_late"]
def filter_minute_history(df, params, *, daily=None, selection=None):
    return df.filter(pl.col("csgi_late")).group_by("symbol").agg(
        pl.col("datetime").last().alias("last_datetime"), pl.col("close").last()).with_columns(
        pl.lit(100.).alias("score"))
''')
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    sig = {**signal(time=start, end_time=end, evaluation_delay_minutes=.5,
                   change_min_pct=.1, change_max_pct=4.),
           "id": "late", "name": "入场", "kind": "entry", "timeframe": "intraday"}
    at = datetime.fromisoformat(f"{DAY}T{start}")
    history = pl.DataFrame({
        "symbol": ["600001.SH"] * 3,
        "datetime": [at - timedelta(minutes=1), at, at + timedelta(minutes=1)],
        "close": [10.1, 10.2, 90.0],
    })
    previous = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 11)],
                             "raw_close": [10.], "close": [10.]})
    frozen = {"as_of": str(DAY), "source_date": "2026-11-11",
              "rows": [{"symbol": "600001.SH", "reference_close": 10.}]}
    context = StrategyDataContext(
        "stock", "1m", DAY, current=previous, history=history, daily_history=previous,
        pool_history=previous, observed_at=datetime(2026, 11, 12, 15, tzinfo=CN_TZ),
        completed_through=datetime(2026, 11, 12, 15),
        selection_snapshots={sid: frozen},
    )
    with custom_signals.private_signal_registry([sig]):
        yield engine, sid, sig, context, at


def test_historical_close_replays_exact_saved_entry_and_ignores_later_price(scenario):
    engine, sid, _, context, at = scenario
    result = engine.run(sid, context)
    assert result.section_statuses["buy"] == "success", result.section_errors
    assert result.section_errors == {}
    assert len(result.sections["buy"]) == 1
    assert result.rows[0]["close"] == 10.2
    assert result.rows[0]["last_datetime"] == at
    assert result.rows[0]["entry_evaluated_at"] == at + timedelta(seconds=30)


def test_live_close_still_expires_without_changing_historical_context(scenario):
    engine, sid, _, context, _ = scenario
    result = engine.run(sid, replace(context, realtime=True))
    assert result.section_statuses["buy"] == "expired"
    assert result.sections["buy"] == []


def test_history_missing_target_minute_does_not_become_successful_zero(scenario):
    engine, sid, _, context, at = scenario
    result = engine.run(sid, replace(context, history=context.history.filter(pl.col("datetime") != at)))
    assert result.sections["buy"] == []
    assert result.section_errors["buy"]
    assert result.section_statuses["buy"] in {"waiting", "failed"}


def test_history_before_confirmation_stays_pending(scenario):
    engine, sid, _, context, at = scenario
    result = engine.run(sid, replace(context, observed_at=at + timedelta(seconds=15)))
    assert result.sections["buy"] == []
    assert result.section_statuses["buy"] == "waiting"
