"""Current S05 signal references and branch scheduling."""

import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.services.routed_strategy_monitor import task_windows
from app.strategy import s02_173004552 as routed
from app.strategy.custom_signals import private_signal_registry, validate
from app.strategy.routed_engine import RoutedStrategyEngine

DEFINITIONS = Path(__file__).resolve().parents[2] / "examples/strategies/s02_173004552"
ENTRIES = {"tail": "tail_entry_s05", "low_gap": "low_gap_entry_s05", "board": "board_entry_s05"}
EXITS = {"tail": "tail_exit_s05", "low_gap": "low_gap_exit_s05", "board": "board_exit_s05"}
DAY = date(2026, 11, 27)


def test_current_signal_references_are_the_six_saved_identifiers():
    assert routed.ENTRY_IDS == ENTRIES
    assert routed.EXIT_IDS == EXITS
    book = json.loads((DEFINITIONS / "signals.json").read_text())
    assert {s["id"] for s in book} == set(ENTRIES.values()) | set(EXITS.values())
    engine = RoutedStrategyEngine([DEFINITIONS])
    strategy = engine.get(routed.SID)
    overrides = json.loads((DEFINITIONS / "trigger-overrides.json").read_text())
    for kind, expected in (("entry", ENTRIES), ("exit", EXITS)):
        names = {"csgi_" + value for value in expected.values()}
        assert set(getattr(strategy, kind + "_signals")) == names
        assert set(overrides[kind + "_signals"]) == names
    for signal in book:
        validate(signal)


@pytest.mark.parametrize(("branch", "up", "ma"), [
    ("tail", .55, .48), ("low_gap", .50, .48), ("board", .48, .45),
])
def test_current_identifiers_read_both_schedules_from_selected_signals(branch, up, ma):
    book = json.loads((DEFINITIONS / "signals.json").read_text())
    # Use the current identifiers independently of the sample to expose stale routing constants.
    for signal in book:
        signal["id"] = (ENTRIES if signal["kind"] == "entry" else EXITS)[
            next(name for name in routed.BRANCHES if name in signal["id"])]
    entry = next(s for s in book if s["id"] == ENTRIES[branch])
    exit_signal = next(s for s in book if s["id"] == EXITS[branch])
    entry["params"].update(time="14:17:00", end_time="14:28:00", evaluation_delay_minutes=1)
    exit_signal["params"].update(start_time="10:11:00", time="10:50:00", fallback_time="11:40:00")
    dates = routed.sessions_until(DAY)
    saved = routed.freeze_market_route({
        "as_of": str(DAY), "source_date": str(dates[-1]), "rows": [],
        "entry_routing": routed.POLICY,
        "market_breadth": [{"date": str(day), "up": up, "above_ma20": ma} for day in dates],
    }, DAY, routed.MARKET_DEFAULTS, observed=datetime.combine(dates[-1], time(22, 15), CN_TZ))
    engine = RoutedStrategyEngine([DEFINITIONS])
    overrides = {"entry_signals": ["csgi_" + v for v in ENTRIES.values()],
                 "exit_signals": ["csgi_" + v for v in EXITS.values()]}
    with private_signal_registry(book):
        windows = task_windows(engine, engine.get(routed.SID), overrides, day=DAY,
                               selection=saved, roster={"rows": []}, require_market_route=True)
    assert [(w["signal"]["id"], w["start"], w["end"]) for w in windows["buy"]] == [
        (ENTRIES[branch], "14:17:00", "14:28:00")]
    assert [(w["signal"]["id"], w["start"], w["end"]) for w in windows["sell"]] == [
        (EXITS[branch], "10:11:00", "11:40:00")]
    assert routed.branch_clock(entry, DAY)[0] == branch
