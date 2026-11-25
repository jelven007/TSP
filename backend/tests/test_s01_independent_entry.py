"""Real S01 consumes frozen stocks independently of complete-pool breadth."""
import math
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.screener import ScreenerService
from app.services.strategy_monitor_tasks import run_tasks, task_windows
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine

SID = "custom_mvxxcs01"
DAY = date(2026, 11, 23)
SOURCE = Path(__file__).resolve().parents[2] / "examples/strategies/s01_signal_sections.py"
SIGNAL = {
    "id": "late_session_strength_s01", "name": "s01_尾盘建仓", "enabled": True,
    "kind": "entry", "timeframe": "intraday", "template": "late_session_strength",
    "conditions": [], "params": {
        "time": "14:45:00", "evaluation_delay_minutes": 1, "end_time": "15:00:00",
        "pool_up_min_pct": 65, "change_min_pct": 2, "change_max_pct": 3,
        "amount_min_100m": 1, "amplitude_max_pct": 5.5,
        "ma5_1m_count": None, "ma5_5m_count": None,
        "turnover_min_pct": None, "volume_ratio_min": None,
    },
}
OVERRIDES = {"entry_signals": ["csgi_late_session_strength_s01"], "exit_signals": []}
FROZEN = {"as_of": str(DAY), "source_date": "2026-11-20",
          "rows": [{"symbol": "600001.SH", "reference_close": 10.}]}


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "s01.py").write_bytes(SOURCE.read_bytes())
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, SIGNAL)
    return engine


def minutes():
    # Sparse input, duplicate startup label, extreme later minute: none may
    # double amount or alter the confirmed startup snapshot.
    return pl.DataFrame({
        "symbol": ["600001.SH"] * 4,
        "datetime": [datetime(2026, 11, 23, 9, 31),
                     datetime(2026, 11, 23, 14, 45),
                     datetime(2026, 11, 23, 14, 45),
                     datetime(2026, 11, 23, 14, 46)],
        "open": [10., 10., 10., 100.], "high": [10.1, 10.2, 10.3, 999.],
        "low": [10., 10., 10., 1.], "close": [10., 10.2, 10.25, 100.],
        "amount": [40e6, 999e6, 60e6, 999e9], "volume": [1000.] * 4,
    })


def test_real_s01_declares_independent_clocks_and_no_repeated_daily_dependency(engine):
    strategy = engine.get(SID)
    assert strategy.meta.get("entry_phase_policy") == "independent_v1"
    assert strategy.meta.get("entry_breadth_observation") is True
    assert strategy.minute_selection_snapshot
    assert engine.minute_entry_daily_history_bars([SID], overrides_map={SID: OVERRIDES}) == 0
    assert task_windows(engine, strategy, OVERRIDES)["buy"][0]["phase_policy"] == "independent_v1"


def test_stock_phase_succeeds_without_full_pool_or_daily_and_cannot_publish_alone(engine, monkeypatch):
    from app.strategy import late_session_strength as late
    from app.strategy import signal_price_suggestions  # noqa: F401

    monkeypatch.setattr(engine, "_daily_pool_loader", lambda *_: pytest.fail("loaded full pool"), raising=False)
    monkeypatch.setattr(late, "previous_closes", lambda *_a, **_k: pytest.fail("loaded full references"))
    reads = []

    def load(symbols, dates):
        assert symbols == ["600001.SH"] and dates == [DAY]
        reads.append(symbols)
        return minutes()

    context = ScreenerService(SimpleNamespace(get_minute_by_dates=load)).build_selected_stock_context(
        engine, DAY, SID, FROZEN, current=None, overrides=OVERRIDES,
        observed_at=datetime(2026, 11, 23, 14, 46, tzinfo=CN_TZ))
    context.signal_task_states = {}
    result = run_tasks(engine, engine.get(SID), context, OVERRIDES,
                       task_windows(engine, engine.get(SID), OVERRIDES), None)
    step = result.monitor_tasks["buy"]["signals"][SIGNAL["id"]]["stocks"]
    assert step["state"] == "success", step
    assert reads == [["600001.SH"]]
    row = step["rows"][0]
    assert row["close"] == 10.25
    assert row["amount"] == 100e6
    assert row["score"] == pytest.approx(-.03 + .001 * math.log(100e6))
    assert str(row["last_datetime"]) == "2026-11-23 14:45:00"
    assert result.rows == result.entry_signal_hits == []
    assert not result.monitor_tasks["buy"]["published"]


def test_entry_keeps_actual_daily_dependencies(engine):
    sig = {**SIGNAL, "params": {**SIGNAL["params"], "volume_ratio_min": 1.5}}
    custom_signals.save_one(engine._user_data_dir(), sig)
    assert engine.minute_entry_daily_history_bars([SID], overrides_map={SID: OVERRIDES}) >= 6
    custom_signals.save_one(engine._user_data_dir(), SIGNAL)
    daily = {**OVERRIDES, "entry_signals": ["signal_limit_up"]}
    assert engine.minute_entry_daily_history_bars([SID], overrides_map={SID: daily}) >= 60
