"""Current S05 common entries are independent of archived market routing."""
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.market_time import CN_TZ
from app.strategy.late_session_strength import DEFAULT_PARAMS

SID = "custom_mvxxcs05"
DAY = date(2026, 11, 26)
SOURCE = date(2026, 11, 25)


def current_strategy():
    return SimpleNamespace(meta={"id": SID, "entry_signal_match": "any"},
                           execution_backend="minute_filter")


def test_current_selection_keeps_common_execution_with_daily_schedule_parameter(monkeypatch):
    from test_s05_common_daily_branch import common_selection

    from app.services import s05_scoring
    from app.services.daily_selections import compute_selections, selection_policy
    from app.services.s05_selection import COMMON_BRANCH_POLICY, POLICY, WEIGHTS, freeze_source
    from app.strategy import s02_173004552 as routed

    def forbidden(*args, **kwargs):
        raise AssertionError("Current S05 evaluated removed market rules")

    monkeypatch.setattr(s05_scoring, "route", forbidden)
    source = {
        "as_of": str(DAY), "source_date": str(SOURCE), "identity": "source",
        "scheduled_for": f"{SOURCE}T21:00:00+08:00",
        "generated_at": f"{SOURCE}T21:10:00+08:00", "rows": [{"symbol": "a"}]}
    output = {**common_selection(4), **source, "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
              "market_breadth": [{"date": str(d), "up": .13, "above_ma20": .08}
                                 for d in routed.sessions_until(DAY)], "score_coverage": {}}
    original = deepcopy(output)
    monkeypatch.setattr(s05_scoring, "compute_selection", lambda *args, **kwargs: deepcopy(output))
    engine = SimpleNamespace(get=lambda sid: current_strategy())
    actual = compute_selections(engine, SimpleNamespace(repo=None), SOURCE, DAY, [SID], {},
        source_selection=freeze_source(source, SOURCE, DAY),
        now=datetime(2026, 11, 25, 22, tzinfo=CN_TZ))
    assert actual["errors"] == {}
    assert actual["selections"][SID]["rows"] == source["rows"]
    assert "entry_routing" not in actual["selections"][SID]
    assert actual["selections"][SID]["branch_code"] == 4
    assert actual["selections"][SID]["entry_execution"] == "common_template_v1"
    assert output == original
    assert selection_policy(current_strategy()) == COMMON_BRANCH_POLICY


def test_shared_scorer_can_skip_route_without_changing_scores(monkeypatch):
    from test_s05_independent_scoring import inputs

    from app.services import s05_scoring

    snapshot, daily, flows, financial = inputs()
    original = s05_scoring.freeze_selection(snapshot, daily, flows, financial, DAY)
    monkeypatch.setattr(s05_scoring, "route", lambda *a: (_ for _ in ()).throw(
        AssertionError("Removed market rule executed")))
    common = s05_scoring.freeze_selection(snapshot, daily, flows, financial, DAY, include_entry_route=False)
    assert common["rows"] == original["rows"]
    assert not set(common) & {"entry_routing", "entry_branch", "entry_route", "branch_code"}


def test_s05_windows_wait_for_daily_parameter_without_route_or_roster():
    from app.services.routed_strategy_monitor import task_windows
    from app.strategy.custom_signals import private_signal_registry

    pairs = [("tail", "14:05:00", "14:16:00"), ("low_gap", "14:14:00", "14:25:00"),
             ("board", "14:40:00", "14:51:00")]
    book = [{"id": branch + "_entry_s05", "name": branch, "enabled": True,
             "kind": "entry", "conditions": [], "template": "late_session_strength",
             "timeframe": "intraday", "params": {**dict.fromkeys(DEFAULT_PARAMS),
                 "time": start, "end_time": end, "evaluation_delay_minutes": 1}}
            for branch, start, end in pairs]
    strategy = current_strategy()
    strategy.entry_signals = ["csgi_" + s["id"] for s in book]
    strategy.exit_signals = []
    engine = SimpleNamespace(_effective_signals=lambda override, key, default: override.get(key, default))
    with private_signal_registry(book):
        result = task_windows(engine, strategy, {}, day=DAY, selection=None, roster=None,
                              require_market_route=True)
    assert result["buy"] == result["sell"] == []
    assert result["selection_error"] and result["sell_source_error"]


def test_any_mask_and_hits_keep_each_signal_independent():
    from app.strategy.engine import StrategyEngine

    frame = pl.DataFrame({"symbol": ["a", "b", "c"],
                          "csgi_tail_entry_s05": [True, False, False],
                          "csgi_low_gap_entry_s05": [False, True, False]})
    names = ["csgi_tail_entry_s05", "csgi_low_gap_entry_s05"]
    assert frame.filter(StrategyEngine._entry_signal_mask(frame, names, require_all=False))["symbol"].to_list() == ["a", "b"]
    assert StrategyEngine._collect_signal_hits(frame, names) == [
        {"symbol": "a", "signals": [names[0]]}, {"symbol": "b", "signals": [names[1]]}]


def test_current_common_archive_takes_priority_without_rewriting_routed_history(tmp_path):
    from test_s05_daily_branch import selection

    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import COMMON_POLICY, POLICY, WEIGHTS
    from app.services.strategy_results import ResultIdentity

    legacy = selection(4)
    target = date.fromisoformat(legacy["as_of"])
    legacy.update(scoring_policy=POLICY, scoring_weights=WEIGHTS)
    identity = ResultIdentity(SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    store = DailySelections(tmp_path)
    old = store.save(identity, legacy, started_at=1)
    common = {key: value for key, value in legacy.items()
              if key not in ("entry_routing", "entry_branch", "entry_route", "branch_code")}
    common["entry_execution"] = "common_template_v1"
    new = store.save(identity, common, started_at=2)
    assert old["record_id"] != new["record_id"]
    assert store.results.detail(SID, old["record_id"]) == old
    assert store.read(SID, target, policy=COMMON_POLICY)["entry_execution"] == "common_template_v1"
    assert store.read(SID, target)["entry_execution"] == "common_template_v1"
