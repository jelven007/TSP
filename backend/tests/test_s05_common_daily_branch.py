"""S05 buy schedule follows the target day; sell follows the prior archived buy."""
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.market_time import CN_TZ
from app.services.routed_strategy_monitor import task_windows
from app.strategy import s02_173004552 as routed
from app.strategy.custom_signals import private_signal_registry
from app.strategy.late_session_strength import DEFAULT_PARAMS

DAY = date(2026, 11, 27)
SOURCE = date(2026, 11, 26)


def common_selection(code, day=DAY):
    from test_s05_daily_branch import selection
    saved = selection(code, day)
    saved.pop("entry_routing")
    saved["entry_execution"] = "common_template_v1"
    return saved


def common_strategy():
    return SimpleNamespace(
        meta={"id": routed.SID, "entry_signal_match": "any"}, source="custom",
        execution_backend="minute_filter",
        entry_signals=["csgi_" + value for value in routed.ENTRY_IDS.values()],
        exit_signals=["csgi_" + value for value in routed.EXIT_IDS.values()])


def signals():
    book = []
    for branch, start, end, sell_start, sell_stop, sell_end in (
        ("tail", "14:05:00", "14:16:00", "09:49:00", "10:00:00", "10:30:00"),
        ("low_gap", "14:14:00", "14:25:00", "09:43:00", "13:25:00", "13:55:00"),
        ("board", "14:40:00", "14:51:00", "10:13:00", "10:55:00", "11:25:00"),
    ):
        book.extend([
            {"id": routed.ENTRY_IDS[branch], "name": branch, "enabled": True, "kind": "entry",
             "template": "late_session_strength", "timeframe": "intraday", "conditions": [],
             "params": {**dict.fromkeys(DEFAULT_PARAMS), "time": start, "end_time": end,
                        "evaluation_delay_minutes": 1, "change_max_pct": 6}},
            {"id": routed.EXIT_IDS[branch], "name": branch, "enabled": True, "kind": "exit",
             "template": "morning_liquidation", "timeframe": "intraday", "conditions": [],
             "params": {"start_time": sell_start, "time": sell_stop, "fallback_time": sell_end,
                        "take_profit_pct": 1, "stop_loss_pct": 1.5}},
        ])
    return book


def engine():
    return SimpleNamespace(_effective_signals=lambda override, key, default: override.get(key, default))


@pytest.mark.parametrize("code", [2, 4, None])
def test_sell_tasks_follow_confirmed_prior_buy_groups_even_without_today_route(code):
    roster = {"source_date": str(SOURCE), "rows": [
        {"symbol": "a", "entry_branch": "tail"}, {"symbol": "b", "entry_branch": "board"}]}
    with private_signal_registry(signals()):
        windows = task_windows(engine(), common_strategy(), {}, day=DAY,
            selection=common_selection(code) if code else None,
            roster=roster, require_market_route=True)
    actual = {window["signal"]["id"]: window.get("symbols") for window in windows["sell"]}
    assert actual == {"tail_exit_s05": ["a"], "board_exit_s05": ["b"]}
    if code == 2:
        assert [w["signal"]["id"] for w in windows["buy"]] == ["low_gap_entry_s05"]
    else:
        assert windows["buy"] == []


def test_branch_four_projection_keeps_pending_sell_instead_of_inventing_rejection():
    result = {"sections": {"buy": [], "select": [], "sell": [{"symbol": "a"}]},
              "section_statuses": {"sell": "waiting"}, "section_errors": {"sell": "等待卖出"},
              "monitor_tasks": {"sell": {"state": "waiting"}}}
    routed.project_no_market_route(result, common_selection(4))
    assert result["monitor_tasks"]["sell"] == {"state": "waiting"}
    assert result["section_statuses"]["sell"] == "waiting"
    assert result["section_errors"]["sell"] == "等待卖出"


@pytest.mark.parametrize("code, columns", [(2, ["csgi_low_gap_entry_s05"]), (4, [])])
def test_ordinary_scan_evaluates_only_the_confirmed_daily_entry(monkeypatch, code, columns):
    from app.strategy.engine import StrategyDataContext, StrategyEngine, StrategyResult
    from app.strategy.routed_engine import RoutedStrategyEngine

    strategy = common_strategy()
    engine = RoutedStrategyEngine()
    engine._strategies[routed.SID] = strategy
    calls = []

    def base_run(_engine, sid, context, pool=None, params=None, overrides=None):
        calls.append(overrides.get("entry_signals", strategy.entry_signals))
        rows = [{"symbol": "a"}]
        return StrategyResult(DAY, sid, rows, 1, section_mode="signals",
                              sections={"buy": rows, "sell": [{"symbol": "holding"}], "select": []})

    monkeypatch.setattr(StrategyEngine, "run", base_run)
    day = StrategyDataContext("stock", "1m", DAY,
                              selection_snapshots={routed.SID: common_selection(code)})
    saved = {"exit_signals": ["csgi_tail_exit_s05"]}
    result = engine.run(routed.SID, day, overrides=saved)
    assert calls[-1] == columns
    assert "entry_signals" not in saved
    assert result.sections["sell"] == [{"symbol": "holding"}]
    if code == 4:
        assert result.total == 0 and result.sections["buy"] == []
        assert result.section_statuses["buy"] == "rejected"


def test_mixed_ordinary_scan_archive_keeps_other_branches_out_of_buy_and_next_sell(tmp_path):
    import gzip
    import json

    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import POLICY, WEIGHTS
    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import ResultIdentity, StrategyResults
    from app.strategy.overnight_runtime import next_market_day

    store = StrategyResults(tmp_path)
    identity = ResultIdentity(routed.SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    rows = [{"symbol": "a", "score": 82, "csgi_low_gap_entry_s05": True},
            {"symbol": "b", "score": 76, "csgi_board_entry_s05": True}]
    snapshot = {**common_selection(2), "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
                "rows": rows}
    DailySelections(tmp_path).save(identity, snapshot, started_at=1)
    frozen = {"as_of": str(DAY), "section_mode": "signals", "rows": rows, "total": 2,
              "scores": {"a": 82, "b": 76}, "section_statuses": {"buy": "success"},
              "sections": {"buy": rows, "sell": [], "select": rows},
              "entry_signal_hits": [{"symbol": "a", "signals": ["csgi_low_gap_entry_s05"]},
                                    {"symbol": "b", "signals": ["csgi_board_entry_s05"]}]}
    saved = store.save(identity, frozen, started_at=2)
    day = json.loads(gzip.decompress(store.day_view(routed.SID, DAY)[1]))["result"]
    assert [row["symbol"] for row in day["sections"]["buy"]] == ["a"]
    assert [row["symbol"] for row in day["sections"]["select"]] == ["a", "b"]
    assert day["total"] == 1 and day["scores"] == {"a": 82}
    assert [hit["symbol"] for hit in day["entry_signal_hits"]] == ["a"]
    assert store.detail(routed.SID, saved["record_id"])["result"]["sections"]["buy"] == rows
    next_day = next_market_day(DAY)
    record = json.loads(gzip.decompress(store.day_view(routed.SID, next_day)[1]))["result"]
    assert [row["symbol"] for row in record["sections"]["sell"]] == ["a"]
    assert [(row["symbol"], row["entry_branch"]) for row in sell_roster(tmp_path, routed.SID, next_day)["rows"]] == [
        ("a", "low_gap")]
    from app.services.strategy_day_views import _get, _key, _put, publish

    # A pre-fix roster may already exist. Read and republish without rewriting its source.
    with store._local(write=True) as conn:
        key = _key(routed.SID, next_day, "stock", "1m")
        stale = _get(store, conn, "strategy_sell_rosters", key)
        _put(store, conn, "strategy_sell_rosters", key, {
            **stale, "rows": [*stale["rows"], {**rows[1], "entry_branch": "board",
                                            "entry_date": str(DAY), "entry_record_id": saved["record_id"]}],
        })
        publish(store, conn, routed.SID, next_day, cascade=False)
    assert [r["symbol"] for r in sell_roster(tmp_path, routed.SID, next_day)["rows"]] == ["a"]
    record = json.loads(gzip.decompress(store.day_view(routed.SID, next_day)[1]))["result"]
    assert [row["symbol"] for row in record["sections"]["sell"]] == ["a"]


def test_old_buy_archive_is_attributed_without_rewriting_it(tmp_path):
    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import ResultIdentity, StrategyResults

    store = StrategyResults(tmp_path)
    identity = ResultIdentity(routed.SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    buys = [{"symbol": "600001.SH"}, {"symbol": "600002.SH"}]
    result = {
        "strategy_id": routed.SID, "as_of": str(SOURCE), "rows": buys, "total": len(buys),
        "section_mode": "signals", "sections": {"buy": buys, "sell": [], "select": []},
        "section_statuses": {"buy": "success"},
        "entry_signal_hits": [{"symbol": "600001.SH", "signals": ["csgi_tail_entry_s05"]}],
    }
    saved = store.save(identity, result, started_at=1)
    roster = sell_roster(tmp_path, routed.SID, DAY)
    assert [(row["symbol"], row["entry_branch"]) for row in roster["rows"]] == [
        ("600001.SH", "tail"), ("600002.SH", None)]
    assert store.detail(routed.SID, saved["record_id"])["result"]["sections"]["buy"] == buys
    with private_signal_registry(signals()):
        windows = task_windows(engine(), common_strategy(), {}, day=DAY,
                               roster=roster, selection=common_selection(4))
    assert [window["symbols"] for window in windows["sell"]] == [["600001.SH"]]
    assert "600002.SH" in windows["sell_source_error"]


def test_branch_four_prior_rejection_can_schedule_correct_sell_groups(monkeypatch):
    from app.services import strategy_monitor_tasks as tasks
    from app.services.strategy_monitor_state import plan
    from app.strategy.engine import StrategyDataContext

    selected, strategy, runtime = common_selection(4), common_strategy(), engine()
    runtime._selection_loader = lambda *args: selected
    roster = {"source_date": str(SOURCE), "rows": [
        {"symbol": "a", "entry_branch": "tail"}, {"symbol": "b", "entry_branch": "board"}]}
    at = datetime(2026, 11, 27, 10, 15, tzinfo=CN_TZ)
    old = {"sell": {"state": "rejected", "terminal": True, "completion_reason": "s05_no_daily_branch"}}
    runtime._monitor_receipt_loader = lambda *_: {"tasks": old}
    observed = {}

    def evaluate(*args):
        signal = args[4]["sell"][0]["signal"]["id"]
        observed[signal] = [r["symbol"] for r in args[5]["rows"]]
        return {"state": "success", "errors": [], "hits": [
            {"symbol": r["symbol"], "trigger_time": at.isoformat()} for r in args[5]["rows"]]}

    monkeypatch.setattr(tasks, "evaluate_sell_task", evaluate)
    with private_signal_registry(signals()):
        windows = task_windows(runtime, strategy, {}, day=DAY, selection=selected, roster=roster)
        assert len(plan(windows, old, at, trading=True)["actions"]) == 2
        result = tasks.run_tasks(runtime, strategy, StrategyDataContext(
            "stock", "1m", DAY, observed_at=at), {}, windows, roster)
    assert observed == {"tail_exit_s05": ["a"], "board_exit_s05": ["b"]}
    assert result.monitor_tasks["sell"]["terminal"]
    assert result.monitor_tasks["buy"]["executed"] is False
    assert {hit["symbol"] for hit in result.exit_signal_hits} == {"a", "b"}


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_current_meta_uses_daily_code_for_buy_and_prior_branch_for_sell(code):
    from app.services.strategy_monitor_state import plan
    book = signals()
    before = deepcopy(book)
    saved = common_selection(code)
    with private_signal_registry(book):
        windows = task_windows(engine(), common_strategy(), {}, day=DAY, selection=saved,
            roster={"source_date": str(SOURCE), "rows": [{"symbol": "a", "entry_branch": "board"}]},
            require_market_route=True)
    assert windows["branch_code"] == code
    assert windows["branch_source_date"] == str(SOURCE)
    branch = routed.BRANCH_CODES[code]
    if branch is None:
        assert windows["buy"] == []
        assert plan(windows, {}, datetime(2026, 11, 27, 10, tzinfo=CN_TZ), trading=True)["actions"] == []
    else:
        for kind, ids, start_key, end_key in (
            ("buy", routed.ENTRY_IDS, "time", "end_time"),
            ("sell", routed.EXIT_IDS, "start_time", "fallback_time"),
        ):
            sig = next(s for s in book if s["id"] == ids[branch if kind == "buy" else "board"])
            assert [(w["signal"]["id"], w["start"], w["end"]) for w in windows[kind]] == [
                (sig["id"], sig["params"][start_key], sig["params"][end_key])]
        at = datetime.combine(DAY, datetime.fromisoformat("2000-01-01T" + windows["sell"][0]["start"]).time(), CN_TZ)
        due = plan(windows, {}, at, trading=True)["actions"]
        assert [a["signal_id"] for a in due if a["kind"] == "sell"] == [routed.EXIT_IDS["board"]]
        assert plan(windows, {"sell": {"state": "success", "terminal": True}}, at, trading=True)["actions"] == []
    assert [w["signal"]["id"] for w in windows["sell"]] == [routed.EXIT_IDS["board"]]
    assert book == before


@pytest.mark.parametrize("bad", [None, "wrong_day", "wrong_source", "missing_code", "bool", "legacy_schema"])
def test_no_daily_parameter_cannot_fall_back_to_three_sell_tasks(bad):
    saved = common_selection(2)
    if bad is None:
        saved = None
    elif bad == "wrong_day":
        saved["as_of"] = "2026-11-26"
    elif bad == "wrong_source":
        saved["source_date"] = "2026-11-25"
    elif bad == "missing_code":
        saved.pop("branch_code")
    elif bad == "bool":
        saved["branch_code"] = True
    else:
        saved["entry_route"]["schema"] = routed.LEGACY_MARKET_ROUTE
    with private_signal_registry(signals()):
        windows = task_windows(engine(), common_strategy(), {}, day=DAY, selection=saved,
                               roster={"rows": [{"symbol": "a"}]}, require_market_route=True)
    assert windows["buy"] == windows["sell"] == []
    assert windows["selection_error"] and windows["sell_source_error"]


def test_no_exit_window_when_roster_missing_or_exit_disabled():
    with private_signal_registry(signals()):
        pending = task_windows(engine(), common_strategy(), {}, day=DAY,
            selection=common_selection(1), roster=None, require_market_route=True)
        off = task_windows(engine(), common_strategy(), {"exit_signals": []}, day=DAY,
            selection=common_selection(1), roster={
                "source_date": str(SOURCE), "rows": [{"symbol": "a", "entry_branch": "tail"}]},
            require_market_route=True)
    assert pending["sell"] == []
    assert pending["sell_source_error"]
    assert off["sell"] == []


def test_nightly_common_scoring_freezes_integer_without_using_legacy_entry_rules(monkeypatch):
    from app.services import s05_scoring
    from app.services.daily_selections import compute_selections, selection_policy
    from app.services.s05_selection import COMMON_BRANCH_POLICY, POLICY, WEIGHTS, freeze_source

    source = {"as_of": str(DAY), "source_date": str(SOURCE), "identity": "source",
              "scheduled_for": f"{SOURCE}T21:00:00+08:00", "generated_at": f"{SOURCE}T21:10:00+08:00",
              "rows": [{"symbol": "a"}]}
    ranked = {**common_selection(3), "rows": source["rows"], "scoring_policy": POLICY,
              "scoring_weights": WEIGHTS}

    def score(*args, **kwargs):
        assert kwargs["include_entry_route"] is False
        return deepcopy(ranked)

    monkeypatch.setattr(s05_scoring, "compute_selection", score)
    with private_signal_registry(signals()):
        result = compute_selections(SimpleNamespace(get=lambda sid: common_strategy()),
            SimpleNamespace(repo=None), SOURCE, DAY, [routed.SID], {},
            source_selection=freeze_source(source, SOURCE, DAY),
            now=datetime(2026, 11, 26, 22, 15, tzinfo=CN_TZ))
    assert result["errors"] == {}
    actual = result["selections"][routed.SID]
    assert actual["branch_code"] == 3 and type(actual["branch_code"]) is int
    assert actual["entry_execution"] == "common_template_v1"
    assert "entry_routing" not in actual
    assert actual["rows"] == source["rows"]
    assert selection_policy(common_strategy()) == COMMON_BRANCH_POLICY


def test_branch_common_archive_separate_from_old_common_and_numeric_history(tmp_path):
    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import COMMON_BRANCH_POLICY, COMMON_POLICY, POLICY, WEIGHTS
    from app.services.strategy_results import ResultIdentity

    store = DailySelections(tmp_path)
    new = {**common_selection(2), "scoring_policy": POLICY, "scoring_weights": WEIGHTS}
    old = {k: v for k, v in new.items() if k not in ("entry_branch", "entry_route", "branch_code")}
    identity = ResultIdentity(routed.SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    prior = store.save(identity, old, started_at=1)
    current = store.save(identity, new, started_at=2)
    assert prior["record_id"] != current["record_id"]
    assert store.results.detail(routed.SID, prior["record_id"]) == prior
    assert store.read(routed.SID, DAY, policy=COMMON_POLICY)["entry_execution"] == "common_template_v1"
    assert store.read(routed.SID, DAY, policy=COMMON_BRANCH_POLICY)["branch_code"] == 2
    assert store.read(routed.SID, DAY)["branch_code"] == 2


def test_completed_sell_is_not_started_again_by_later_quotes(monkeypatch):
    from app.services import strategy_monitor_tasks as tasks
    from app.strategy.engine import StrategyDataContext

    selected, strategy, runtime = common_selection(2), common_strategy(), engine()
    runtime._selection_loader = lambda *args: selected
    observed = []

    def sell(*args):
        observed.append(args[4]["sell"][0]["signal"]["id"])
        return {"state": "success", "errors": [], "hits": [{
            "symbol": "a", "trigger_time": "2026-11-27T09:49:00",
            "signals": ["csgi_tail_exit_s05"]}]}

    monkeypatch.setattr(tasks, "evaluate_sell_task", sell)
    roster = {"source_date": str(SOURCE), "rows": [{"symbol": "a", "entry_branch": "tail"}]}
    with private_signal_registry(signals()):
        windows = task_windows(runtime, strategy, {}, day=DAY, selection=selected,
                               roster=roster, require_market_route=True)
        first = tasks.run_tasks(runtime, strategy, StrategyDataContext(
            "stock", "1m", DAY, observed_at=datetime(2026, 11, 27, 9, 49, tzinfo=CN_TZ)),
            {}, windows, roster)
        runtime._monitor_receipt_loader = lambda *args: {"tasks": first.monitor_tasks}
        second = tasks.run_tasks(runtime, strategy, StrategyDataContext(
            "stock", "1m", DAY, observed_at=datetime(2026, 11, 27, 10, 13, tzinfo=CN_TZ)),
            {}, windows, roster)
    assert observed == [routed.EXIT_IDS["tail"]]
    assert first.monitor_executed_signals["sell"] == [routed.EXIT_IDS["tail"]]
    assert second.monitor_executed_signals["sell"] == []
    assert second.monitor_tasks["sell"]["terminal"]
    assert second.exit_signal_hits == first.exit_signal_hits


def test_monitor_summary_uses_code_four_without_overwriting_prior_task_evidence(tmp_path, monkeypatch):
    from app.services.daily_selections import DailySelections
    from app.services.routed_strategy_monitor import monitoring_summary
    from app.services.s05_selection import POLICY, WEIGHTS
    from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
    from app.services.strategy_results import ResultIdentity

    set_monitoring(tmp_path, routed.SID, "s05", True)
    archived = datetime(2026, 11, 26, 22, 16, tzinfo=CN_TZ)
    monkeypatch.setattr("app.services.strategy_results.time.time", archived.timestamp)
    saved = {**common_selection(4), "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
             "generated_at": archived.isoformat()}
    DailySelections(tmp_path).save(ResultIdentity(
        routed.SID, "s05", "saved", "{}", "{}", "null", "stock", "1m"), saved, started_at=1)
    prior = {"tasks": {kind: {"state": "expired", "count": 0, "terminal": True,
                              "errors": ["旧时段回执"]} for kind in ("buy", "sell")}}
    task_receipt(tmp_path, routed.SID, DAY, prior)
    runtime, strategy = engine(), common_strategy()
    runtime._strategies = {routed.SID: strategy}
    runtime.get = lambda *args: strategy
    runtime.has = lambda sid: sid == routed.SID
    with private_signal_registry(signals()):
        summary = monitoring_summary(runtime, tmp_path, now=datetime(2026, 11, 27, 10, tzinfo=CN_TZ))
    row = summary["strategies"][0]
    assert row["enabled"] and row["branch_code"] == 4
    assert row["tasks"]["buy"]["windows"] == [] and row["tasks"]["buy"]["executed"] is False
    assert row["tasks"]["sell"]["windows"] == [] and row["tasks"]["sell"]["errors"] == ["旧时段回执"]
    assert task_receipt(tmp_path, routed.SID, DAY) == prior


@pytest.mark.parametrize("code, prior_code", [(4, 2), (3, 4), (1, None), (None, 2)])
def test_monitoring_api_reads_selected_day_and_exact_prior_session(tmp_path, monkeypatch, code, prior_code):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import monitor_rules
    from app.api import strategy as strategy_api
    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import POLICY, WEIGHTS
    from app.services.strategy_monitor_tasks import task_receipt
    from app.services.strategy_results import ResultIdentity

    day, previous = date(2026, 11, 23), date(2026, 11, 20)
    identity = ResultIdentity(routed.SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    saved = DailySelections(tmp_path)
    for target, branch in ((date(2026, 11, 19), 1), (previous, prior_code), (day, code)):
        if branch:
            saved.save(identity, {**common_selection(branch, target),
                                  "scoring_policy": POLICY, "scoring_weights": WEIGHTS}, started_at=1)
    actual = {"tasks": {"sell": {"state": "expired", "errors": ["当日未完成"], "count": 0,
                                 "windows": [{"start": "09:00:00", "end": "11:00:00"}]}}}
    task_receipt(tmp_path, routed.SID, day, actual)
    task_receipt(tmp_path, routed.SID, DAY, {"tasks": {"sell": {"state": "success", "count": 99}}})
    runtime = engine()
    runtime.has = lambda sid: sid == routed.SID
    runtime.get = lambda sid: common_strategy()
    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: runtime)
    monkeypatch.setattr(monitor_rules, "_data_dir", lambda _: tmp_path)
    api = FastAPI()
    api.include_router(monitor_rules.router)
    with private_signal_registry(signals()):
        # ContextVar registry is scoped to the request's worker context.
        response = TestClient(api).get(f"/api/monitor-rules/strategies/{routed.SID}/monitoring",
                                       params={"as_of": str(day)})
    assert response.status_code == 200
    row = response.json()
    assert (row["as_of"], row["historical"]) == (str(day), True)
    buy, sell = [row["tasks"][side] for side in ("buy", "sell")]
    assert buy.get("branch_code") == code
    assert (sell["branch_date"], sell.get("branch_code")) == (str(previous), prior_code)
    assert [(w["start"], w["end"]) for w in buy["windows"]] == {
        1: [("14:05:00", "14:16:00")], 3: [("14:40:00", "14:51:00")],
    }.get(code, [])
    assert [(w["signal_id"], w["start"], w["end"]) for w in sell["windows"]] == (
        [("low_gap_exit_s05", "09:43:00", "13:55:00")] if prior_code == 2 else [])
    assert bool(sell.get("plan_issue")) == (prior_code is None)
    assert (sell["state"], sell["receipt_source"], sell["errors"]) == (
        "expired", "scheduler", ["当日未完成"])
    assert sell["receipt_windows"] == actual["tasks"]["sell"]["windows"]
    assert task_receipt(tmp_path, routed.SID, day) == actual
