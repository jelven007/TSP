"""Historical identity, exclusive entry routing and civil-clock order windows."""

import json
from datetime import date, datetime, time, timedelta
from importlib import import_module
from pathlib import Path

import polars as pl
import pytest

from app.strategy.basic_filter import previous_session

DAY = date(2026, 11, 24)
STRATEGIES = Path(__file__).resolve().parents[2] / "examples" / "strategies" / "s02_173004552"


def policy():
    source = STRATEGIES / "strategy.py"
    assert source.is_file(), "缺少 173004552 可保存的策略定义"
    return import_module("app.strategy.s02_173004552")


def breadth(values):
    rows, day = [], DAY
    for value in reversed(values):
        day = previous_session(day)
        rows.append({"date": str(day), "up": value, "above_ma20": .48})
    return list(reversed(rows))


@pytest.mark.parametrize(("values", "expected"), [
    ([.51] * 16 + [.55] * 5, "tail"),
    ([.48] * 15 + [.60] + [.47] * 4 + [.25], "tail"),
    ([.60] + [.50] * 19 + [.52], "low_gap"),
    ([.50] * 19 + [.46, .52], "board"),
])
def test_route_only_uses_twenty_one_exact_completed_trading_days(values, expected):
    domain = policy()
    evidence = breadth(values)
    assert domain.route(evidence, DAY) == expected
    with pytest.raises(ValueError, match="广度"):
        domain.route(evidence[:-1], DAY)
    with pytest.raises(ValueError, match="广度"):
        domain.route([*evidence[:-1], {**evidence[-1], "date": str(DAY)}], DAY)


def test_low_gap_top_rank_is_vetoed_without_replacing_it():
    domain = policy()
    at = datetime.combine(DAY, time(14, 14))
    rows = [
        {"symbol": "600001.SH", "reference_close": 10., "s02_rank": 1, "s02_score": 90.,
         "s02_fund_score": 90., "s02_tech_score": 90.},
        {"symbol": "600002.SH", "reference_close": 10., "s02_rank": 2, "s02_score": 85.,
         "s02_fund_score": 85., "s02_tech_score": 85.},
    ]
    frame = pl.DataFrame([
        {"symbol": s, "datetime": datetime.combine(DAY, t), "open": 10., "high": 10.28,
         "low": 9.9, "close": x}
        for s, values in (("600001.SH", [10., 10.15, 10.25]), ("600002.SH", [10., 10.19, 10.23]))
        for t, x in zip((time(9, 31), time(13, 59), time(14, 14)), values, strict=True)
    ])
    selected, errors, detail = domain.select_entry(rows, frame, "low_gap", at, breadth([.51] * 21)[-1])
    assert not selected and not errors
    assert detail["skipped_symbol"] == "600001.SH"
    old = pl.col("symbol").eq("600001.SH") & pl.col("datetime").eq(at)
    updated = frame.with_columns(pl.when(old).then(10.24).otherwise(pl.col("close")).alias("close"))
    selected, errors, detail = domain.select_entry(rows, updated, "low_gap", at, breadth([.51] * 21)[-1])
    assert selected[0]["symbol"] == "600001.SH" and not errors
    assert selected[0]["last_datetime"] == at


def test_configuration_has_three_authorized_entry_ids_and_holds_exit_parameters():
    from app.strategy.engine import StrategyEngine

    domain = policy()
    engine = StrategyEngine([STRATEGIES])
    strategy = engine.get(domain.SID)
    assert not engine.has("custom_mvxxcp88")
    definitions = json.loads((STRATEGIES / "signals.json").read_text())
    for kind, names in (("entry", strategy.entry_signals), ("exit", strategy.exit_signals)):
        assert len(names) == 3 and len(set(names)) == 3
        assert set(names) == {"csgi_" + sig["id"] for sig in definitions if sig["kind"] == kind}
    assert strategy.meta["entry_signal_match"] == "any"
    assert domain.route(breadth([.55] * 21), DAY) == "tail"


def test_order_windows_keep_the_real_minute_and_do_not_extend_lunch():
    domain = policy()
    def at(hour, minute):
        return datetime.combine(DAY, time(hour, minute))

    sig = next(s for s in json.loads((STRATEGIES / "signals.json").read_text())
               if s["id"] == domain.ENTRY_IDS["low_gap"])
    assert domain.entry_window(at(14, 14), signal=sig) == (at(14, 16), at(14, 25))
    assert domain.exit_window(at(9, 59)) == (at(10, 0), at(10, 29))
    assert domain.exit_window(at(11, 22)) == (at(11, 23), at(11, 30))
    assert domain.exit_window(at(11, 30)) == (at(13, 1), at(13, 30))
    assert domain.exit_window(at(13, 45)) == (at(13, 46), at(14, 0))


def test_freeze_four_dimensions_keeps_s03_source_members_and_excludes_future_prices():
    from app.services.s02_173004552_selection import freeze_selection

    source = previous_session(DAY)
    sessions = [source]
    for _ in range(64):
        sessions.insert(0, previous_session(sessions[0]))
    symbols = [f"600{i:03d}.SH" for i in range(1, 53)]
    daily = pl.DataFrame([
        {"symbol": symbol, "date": day, "close": 10 + i / 100, "raw_close": 10 + i / 100,
         "high": 11., "low": 9., "amount": 1e8}
        for i, day in enumerate([*sessions, DAY]) for symbol in symbols[:-1]
    ])
    snapshot = {"as_of": str(DAY), "source_date": str(source),
                "rows": [{"symbol": symbol, "reference_close": 10.64} for symbol in symbols]}
    out = freeze_selection(snapshot, daily, pl.DataFrame(), {}, DAY)
    assert out["entry_branch"] == "tail"
    assert len(out["rows"]) == 52
    assert [row.get("s02_rank") for row in out["rows"]] == [*range(1, 51), None, None]
    assert out["rows"][0]["s02_fund_score"] == 50
    assert out["rows"][0]["s02_fundamental_score"] == 50
    row = out["rows"][0]
    assert row["s02_score"] == pytest.approx(
        .40 * row["s02_tech_score"] + .30 * row["s02_fund_score"]
        + .25 * row["s02_emotion_score"] + .05 * row["s02_fundamental_score"]
    )
    assert str(DAY) not in {row["date"] for row in out["market_breadth"]}
    assert out["score_coverage"]["invalid_symbols"].keys() == {symbols[-1]}
    assert out["rows"] == freeze_selection(snapshot, daily.filter(pl.col("date") < DAY),
                                            pl.DataFrame(), {}, DAY)["rows"]


def test_signal_projection_waits_for_confirmation_and_ignores_other_routes():
    domain = policy()
    at = datetime.combine(DAY, time(14, 5))
    snapshot = {"as_of": str(DAY), "source_date": str(previous_session(DAY)),
                "entry_routing": domain.POLICY, "entry_branch": "tail",
                "market_breadth": breadth([.55] * 21),
                "rows": [{"symbol": "600001.SH", "reference_close": 10.,
                          "s02_rank": 1, "s02_score": 82.}]}
    frame = pl.DataFrame([
        {"symbol": "600001.SH", "datetime": at + timedelta(minutes=n), "open": 10.,
         "high": 10.2, "low": 9.9, "close": close}
        for n, close in ((-15, 10.), (-5, 10.05), (0, 10.1), (1, 10.2))
    ])
    definitions = {s["id"]: s for s in json.loads((STRATEGIES / "signals.json").read_text())}
    for branch, observed, expected in [
        ("tail", at + timedelta(seconds=29), "waiting"),
        ("low_gap", at + timedelta(minutes=20), "inactive"),
        ("tail", at + timedelta(seconds=30), "confirmed"),
    ]:
        hits, detail = domain.evaluate_signal(frame, snapshot, DAY, definitions[domain.ENTRY_IDS[branch]], observed)
        assert detail["stage"] == expected
        assert [row["last_datetime"] for row in hits] == ([at] if expected == "confirmed" else [])


def test_run_uses_only_frozen_trigger_and_preserves_branch_for_next_day():
    from app.services.routed_strategy_monitor import task_windows
    from app.strategy.custom_signals import private_signal_registry
    from app.strategy.engine import StrategyDataContext
    from app.strategy.routed_engine import RoutedStrategyEngine

    domain = policy()
    engine = RoutedStrategyEngine([STRATEGIES])
    book = json.loads((STRATEGIES / "signals.json").read_text())
    at = datetime.combine(DAY, time(14, 5))
    symbol = "600001.SH"
    frame = pl.DataFrame([
        {"symbol": symbol, "datetime": at + timedelta(minutes=n), "open": 10.,
         "high": 10.2, "low": 9.9, "close": close}
        for n, close in ((-15, 10.), (-5, 10.05), (0, 10.1), (0, 10.12))
    ])
    snapshot = {"as_of": str(DAY), "source_date": str(previous_session(DAY)),
                "entry_routing": domain.POLICY, "entry_branch": "tail",
                "market_breadth": breadth([.55] * 21),
                "rows": [{"symbol": symbol, "reference_close": 10., "s02_rank": 1, "s02_score": 82.}]}
    context = StrategyDataContext("stock", "1m", DAY, history=frame, realtime=True, positions=[],
        observed_at=at + timedelta(seconds=30), selection_snapshots={domain.SID: snapshot},
        independent_entry_phase="stocks")
    with private_signal_registry(book):
        windows = task_windows(engine, engine.get(domain.SID), {}, day=DAY, selection=snapshot)
        assert [w["signal"]["id"] for w in windows["buy"]] == [domain.ENTRY_IDS["tail"]]
        assert not windows["sell"] and windows["sell_source_error"]
        carried = task_windows(engine, engine.get(domain.SID), {}, day=DAY, selection=snapshot,
                               roster={"rows": [{"symbol": symbol, "entry_branch": "board"}]})
        assert [w["signal"]["id"] for w in carried["sell"]] == [domain.EXIT_IDS["board"]]
        assert task_windows(engine, engine.get(domain.SID), {}, day=DAY)["selection_error"]
        result = engine.run(domain.SID, context)
    assert result.section_statuses["buy"] == "success", result.section_errors
    assert result.entry_signal_hits == [{"symbol": symbol, "signals": ["csgi_" + domain.ENTRY_IDS["tail"]],
                                         "evaluated_at": at + timedelta(seconds=30)}]
    assert result.rows[0]["entry_branch"] == "tail"
    assert result.rows[0]["close"] == 10.12
    assert result.rows[0]["score"] == 82.


def test_new_strategy_is_outside_mature_authority_scope():
    from app.services.strategy_input_binding import requires_authority

    payload = {"s02_inputs_required": True}
    assert requires_authority(payload, "custom_mvxxcp88")
    assert requires_authority(payload, "custom_mvxxcs01")
    assert not requires_authority(payload, policy().SID)


def test_saved_clock_moves_bar_and_preserves_the_configured_end():
    from app.services.routed_strategy_monitor import task_windows
    from app.services.strategy_monitor_state import plan
    from app.strategy.custom_signals import private_signal_registry
    from app.strategy.routed_engine import RoutedStrategyEngine

    domain = policy()
    definition = json.loads((STRATEGIES / "signals.json").read_text())[0]
    definition["params"].update(time="14:08:00", evaluation_delay_minutes=2.5, end_time="14:21:00")
    bar = datetime.combine(DAY, time(14, 8))
    confirmation = datetime.combine(DAY, time(14, 10, 30))
    assert domain.branch_clock(definition, DAY) == ("tail", bar, confirmation)
    assert domain.entry_window(bar, signal=definition) == (
        datetime.combine(DAY, time(14, 11)), datetime.combine(DAY, time(14, 21)))
    engine = RoutedStrategyEngine([STRATEGIES])
    snapshot = {"as_of": str(DAY), "source_date": str(previous_session(DAY)),
                "entry_routing": domain.POLICY, "entry_branch": "tail",
                "market_breadth": breadth([.55] * 21), "rows": []}
    with private_signal_registry([definition]):
        windows = task_windows(engine, engine.get(domain.SID), {}, day=DAY, selection=snapshot)
    assert not plan(windows, {}, confirmation - timedelta(seconds=1), trading=True)["actions"]
    action, = plan(windows, {}, confirmation, trading=True)["actions"]
    assert action["scheduled_at"] == "2026-11-24T14:10:30+08:00"
    assert action["retry_until"] == "2026-11-24T14:21:00+08:00"


@pytest.mark.parametrize(("branch", "values", "params", "sort_by", "expected_value"), [
    ("tail", [.55] * 21, {"return_5m_min_pct": 1}, "return_5m", 2.),
    ("low_gap", [.51] * 21, {"fund_weight_pct": 20}, "fund_tech", 74.),
    ("board", [.30] * 21, {"prior_up_min_pct": 30}, "four_dimension", 82.),
])
def test_configured_branch_changes_eligibility_or_ranking(branch, values, params, sort_by, expected_value):
    from app.strategy.custom_signals import validate

    domain = policy()
    definition = next(s for s in json.loads((STRATEGIES / "signals.json").read_text())
                      if s["id"] == domain.ENTRY_IDS[branch])
    definition["params"].update(time="14:20:00", end_time="14:31:00", evaluation_delay_minutes=1,
                                s05_entry={**params, "sort_by": sort_by})
    validate(definition)
    bar = datetime.combine(DAY, time(14, 20))
    rows = [
        {"symbol": "600001.SH", "reference_close": 10., "s02_rank": 1, "s02_score": 90.,
         "s02_fund_score": 90., "s02_tech_score": 50.},
        {"symbol": "600002.SH", "reference_close": 10., "s02_rank": 2, "s02_score": 82.,
         "s02_fund_score": 50., "s02_tech_score": 80.},
    ]
    snapshot = {"as_of": str(DAY), "source_date": str(previous_session(DAY)),
                "entry_routing": domain.POLICY, "entry_branch": branch,
                "market_breadth": breadth(values), "rows": rows if branch != "board" else rows[1:]}
    frame = pl.DataFrame([
        {"symbol": row["symbol"], "datetime": datetime.combine(DAY, clock), "open": 10.,
         "low": 9.9, "high": 10.24, "close": close}
        for row, closes in zip(rows, ((10., 10., 10.16, 10.2, 10.24), (10., 10., 10., 10.2, 10.24)), strict=True)
        for clock, close in zip((time(9, 31), time(14, 5), time(14, 15), time(14, 20), time(14, 21)),
                                closes, strict=True)
    ])
    hits, waiting = domain.evaluate_signal(frame, snapshot, DAY, definition, bar + timedelta(seconds=59))
    assert not hits and waiting["stage"] == "waiting"
    hits, detail = domain.evaluate_signal(frame, snapshot, DAY, definition, bar + timedelta(minutes=1))
    assert detail["sort_by"] == sort_by
    assert [hit["symbol"] for hit in hits] == ["600002.SH"]
    assert hits[0]["entry_sort_value"] == pytest.approx(expected_value)
    assert hits[0]["last_datetime"] == bar and hits[0]["close"] == 10.2


def test_routed_signal_rejects_bad_sort_and_inverted_bounds():
    from app.strategy.custom_signals import validate

    definition = json.loads((STRATEGIES / "signals.json").read_text())[0]
    definition["params"]["s05_entry"] = {"sort_by": "unknown"}
    with pytest.raises(ValueError, match="排序"):
        validate(definition)
    definition["params"]["s05_entry"] = {"change_min_pct": 8, "change_max_pct": 7}
    with pytest.raises(ValueError, match="下限"):
        validate(definition)


@pytest.mark.parametrize(("values", "above", "expected", "matched"), [
    ([.55] * 21, .48, "tail", ["tail", "low_gap", "board"]),
    ([.50] * 21, .48, "low_gap", ["low_gap", "board"]),
    ([.48] * 21, .45, "board", ["board"]),
    ([.48] * 21, .4499, None, []),
    ([.4799] * 21, .45, None, []),
    ([.138915] * 21, .083438, None, []),
])
def test_explicit_market_routes_preserve_priority_and_have_no_fallback(values, above, expected, matched):
    domain = policy()
    market = breadth(values)
    market[-1]["above_ma20"] = above
    prior = {"as_of": str(DAY), "source_date": str(previous_session(DAY)), "rows": [],
             "entry_routing": domain.POLICY, "entry_branch": domain.route(market, DAY),
             "market_breadth": market}
    decision = domain.freeze_market_route(prior, DAY, domain.MARKET_DEFAULTS,
                                          observed=datetime.combine(previous_session(DAY), time(21, 10)))
    assert decision["entry_branch"] == domain.frozen_route(decision, DAY) == expected
    assert decision["entry_route"]["matched"] == matched
    assert prior["entry_branch"] == domain.frozen_route(prior, DAY)
    with pytest.raises(ValueError, match="不一致"):
        domain.frozen_route({**decision, "entry_branch": "low_gap" if expected != "low_gap" else "board"}, DAY)


def test_no_market_route_skips_minutes_and_keeps_the_previous_buy_sell_branch():
    from app.services.routed_strategy_monitor import task_windows
    from app.strategy.custom_signals import private_signal_registry
    from app.strategy.engine import StrategyDataContext
    from app.strategy.routed_engine import RoutedStrategyEngine

    domain = policy()
    engine = RoutedStrategyEngine([STRATEGIES])
    prior = {"as_of": str(DAY), "source_date": str(previous_session(DAY)), "rows": [{"symbol": "600001.SH"}],
             "entry_routing": domain.POLICY, "entry_branch": "board", "market_breadth": breadth([.3] * 21)}
    selected = domain.freeze_market_route(prior, DAY, domain.MARKET_DEFAULTS,
                                          observed=datetime.combine(previous_session(DAY), time(21, 10)))
    selected["entry_route"]["schema"] = domain.LEGACY_MARKET_ROUTE
    selected.pop("branch_code")
    book = json.loads((STRATEGIES / "signals.json").read_text())
    with private_signal_registry(book):
        legacy = task_windows(engine, engine.get(domain.SID), {}, day=DAY, selection=prior,
                              require_market_route=True)
        assert not legacy["buy"] and "等待" in legacy["selection_error"]
        windows = task_windows(engine, engine.get(domain.SID), {}, day=DAY, selection=selected,
                               roster={"rows": [{"symbol": "600002.SH", "entry_branch": "low_gap"}]},
                               require_market_route=False)
        assert windows["buy"] == [] and windows["buy_no_market_route"]
        assert [w["signal"]["id"] for w in windows["sell"]] == [domain.EXIT_IDS["low_gap"]]
        result = engine.run(domain.SID, StrategyDataContext(
            "stock", "1m", DAY, realtime=True, input_errors=["没有分钟行情"],
            selection_snapshots={domain.SID: selected}))
        assert result.section_statuses["buy"] == "rejected"
        assert result.sections["buy"] == [] and not result.entry_filter_error
