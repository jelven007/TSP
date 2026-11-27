"""S05 post-close integer decision and same-day signal scheduling."""
import copy
import gzip
import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.strategy import s02_173004552 as routed
from app.strategy.custom_signals import private_signal_registry
from app.strategy.routed_engine import RoutedStrategyEngine

DAY = date(2026, 11, 27)
DEFINITIONS = Path(__file__).resolve().parents[2] / "examples/strategies/s02_173004552"


def selection(code, day=DAY):
    up, ma = {1: (.55, .48), 2: (.50, .48), 3: (.48, .45), 4: (.13, .08)}[code]
    dates = routed.sessions_until(day)
    saved = {"as_of": str(day), "source_date": str(dates[-1]), "rows": [],
             "entry_routing": routed.POLICY,
             "market_breadth": [{"date": str(d), "up": up, "above_ma20": ma} for d in dates]}
    return routed.freeze_market_route(saved, day, routed.MARKET_DEFAULTS,
        observed=datetime.combine(dates[-1], time(22, 15), CN_TZ))


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_frozen_branch_code_is_a_positive_integer(code):
    saved = selection(code)
    assert type(saved.get("branch_code")) is int and saved["branch_code"] == code
    assert routed.frozen_route(saved, DAY) == {1: "tail", 2: "low_gap", 3: "board", 4: None}[code]


@pytest.mark.parametrize("bad", [None, True, 0, -1, 5, 1.0, "1", 2])
def test_numeric_archive_rejects_missing_wrong_type_range_or_mismatched_code(bad):
    with pytest.raises(ValueError, match="分支参数"):
        routed.frozen_route({**selection(1), "branch_code": bad}, DAY)


def test_branch_parameter_cannot_cross_trading_dates():
    with pytest.raises(ValueError, match="归档"):
        routed.frozen_route(selection(2), date(2026, 11, 26))


def test_code_four_does_not_execute_either_side_but_preserves_roster():
    from app.services.routed_strategy_monitor import task_windows
    from app.services.strategy_monitor_tasks import run_tasks
    from app.strategy.engine import StrategyDataContext

    engine = RoutedStrategyEngine([DEFINITIONS])
    saved = selection(4)
    engine._selection_loader = lambda *_: saved
    roster = {"rows": [{"symbol": "600001.SH", "entry_branch": "board"}]}
    with private_signal_registry(json.loads((DEFINITIONS / "signals.json").read_text())):
        windows = task_windows(engine, engine.get(routed.SID), {}, day=DAY, selection=saved,
                               roster=roster, require_market_route=True)
        context = StrategyDataContext("stock", "1m", DAY, observed_at=datetime(2026, 11, 27, 10, 0, tzinfo=CN_TZ))
        result = run_tasks(engine, engine.get(routed.SID), context, {}, windows, roster)
    assert result.sections["sell"] == roster["rows"]
    assert result.rows == result.exit_signal_hits == []
    assert all(result.monitor_tasks[k]["executed"] is False for k in ("buy", "sell"))
    assert result.section_statuses["buy"] == result.section_statuses["sell"] == "rejected"


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_both_schedules_read_the_same_day_code_and_saved_signal_times(code):
    from app.services.routed_strategy_monitor import task_windows
    from app.services.strategy_monitor_state import plan

    book = json.loads((DEFINITIONS / "signals.json").read_text())
    engine = RoutedStrategyEngine([DEFINITIONS])
    branch = {1: "tail", 2: "low_gap", 3: "board", 4: None}[code]
    if branch:
        entry = next(s for s in book if s["id"] == routed.ENTRY_IDS[branch])
        entry["params"].update(time="14:17:00", end_time="14:28:00", evaluation_delay_minutes=1)
        exit_signal = next(s for s in book if s["id"] == routed.EXIT_IDS[branch])
        exit_signal["params"].update(start_time="10:11:00", time="10:50:00", fallback_time="11:40:00")
    # Inactive branch definitions are not required to plan today's chosen branch.
    other = next(s for s in book if s["id"] == routed.ENTRY_IDS["tail"] and branch != "tail") if branch != "tail" else None
    if other:
        other["params"]["time"] = "not-a-clock"
    saved = selection(code)
    with private_signal_registry(book):
        windows = task_windows(engine, engine.get(routed.SID), {}, day=DAY, selection=saved,
            roster={"rows": [{"symbol": "600001.SH", "entry_branch": "board"}]}, require_market_route=True)
        if code == 4:
            assert windows["buy"] == windows["sell"] == []
            assert plan(windows, {}, datetime(2026, 11, 27, 10, 11, tzinfo=CN_TZ), trading=True)["actions"] == []
        else:
            assert [(w["signal"]["id"], w["start"], w["end"]) for w in windows["buy"]] == [
                (routed.ENTRY_IDS[branch], "14:17:00", "14:28:00")]
            assert [(w["signal"]["id"], w["start"], w["end"]) for w in windows["sell"]] == [
                (routed.EXIT_IDS[branch], "10:11:00", "11:40:00")]
            due = plan(windows, {}, datetime(2026, 11, 27, 10, 11, tzinfo=CN_TZ), trading=True)
            assert due["actions"][0]["signal_id"] == routed.EXIT_IDS[branch]
            # A missing roster prevents selling; it does not hide the saved signal's schedule.
            pending = task_windows(engine, engine.get(routed.SID), {}, day=DAY, selection=saved,
                                   roster=None, require_market_route=True)
            assert pending["sell"] == windows["sell"] and pending["sell_source_error"]
            off = task_windows(engine, engine.get(routed.SID), {"exit_signals": []}, day=DAY,
                               selection=saved, roster={"rows": []}, require_market_route=True)
            assert off["sell"] == []


def test_save_integer_separately_from_legacy_and_read_day_view(tmp_path):
    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import POLICY, ROUTED_POLICY, WEIGHTS
    from app.services.strategy_results import ResultIdentity, StrategyResults

    new = selection(4)
    new.update(scoring_policy=POLICY, scoring_weights=WEIGHTS)
    legacy = copy.deepcopy(new)
    legacy.pop("branch_code", None)
    legacy["entry_route"]["schema"] = "s05_market_route_v2"
    identity = ResultIdentity(routed.SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    store = DailySelections(tmp_path)
    old = store.save(identity, legacy, started_at=1)
    saved = store.save(identity, new, started_at=2)
    assert saved["record_id"] != old["record_id"]
    assert store.read(routed.SID, DAY)["branch_code"] == 4
    assert "branch_code" not in store.read(routed.SID, DAY, policy=ROUTED_POLICY)
    assert store.results.detail(routed.SID, old["record_id"]) == old
    view = json.loads(gzip.decompress(StrategyResults(tmp_path).day_view(routed.SID, str(DAY))[1]))["result"]
    assert view["selection_snapshot"]["branch_code"] == 4
    assert view["monitor_tasks"]["buy"]["executed"] is False
    assert "sell" not in view["monitor_tasks"]
