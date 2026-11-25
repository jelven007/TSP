"""A/B entry gates use frozen five-factor inputs and completed morning bars."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.strategy.late_session_strength import DEFAULT_PARAMS, evaluate, parameters

DAY = date(2026, 11, 20)
AT = datetime(2026, 11, 20, 10)


def groups():
    return {
        "a": {"enabled": True, "score_min": 55, "tech_min": 75, "fund_min": 70,
              "momentum_min_pct": 18, "momentum_max_pct": 40, "bias_max_pct": 100,
              "slope_min_pct": 1, "rank_min": 2, "rank_max": 20,
              "gap_min_pct": -1, "gap_max_pct": 10, "intraday_min_pct": .8,
              "position_min_pct": 90},
        "b": {"enabled": True, "score_min": 70, "tech_min": 65, "fund_min": 40,
              "momentum_min_pct": 5, "momentum_max_pct": 50, "bias_max_pct": 25,
              "slope_min_pct": 0, "rank_min": 6, "rank_max": 25,
              "breadth_min_pct": 25, "gap_min_pct": -5, "gap_max_pct": 1},
    }


def signal():
    return {"id": "late_session_strength_s03", "name": "s03_早盘建仓",
            "kind": "entry", "timeframe": "intraday", "template": "late_session_strength",
            "enabled": True, "conditions": [], "params": dict.fromkeys(DEFAULT_PARAMS) | {
                "time": "10:00:00", "evaluation_delay_minutes": .5,
                "end_time": "10:30:00", "entry_groups": groups()}}


def snapshot():
    def row(symbol, rank, tech, funds, emotion):
        return {"symbol": symbol, "selection_rank": rank, "score": 80.,
                "tech_score": tech, "fund_score": funds, "emotion_score": emotion,
                "reference_close": 10.,
                "scoring_inputs": {"momentum20": .2, "bias20": .1,
                                   "ma20_change5": .02, "breadth": .5}}
    return {"as_of": str(DAY), "source_date": "2026-11-19",
            "scoring_policy": "s03_five_dimensions_v1", "rows": [
                row("A", 3, 80, 75, 75), row("B", 8, 65, 90, 65),
                row("AB", 6, 80, 80, 80), row("NO", 7, 40, 20, 95)]}


def bars():
    return pl.DataFrame([{
        "symbol": symbol, "datetime": moment, "open": 10., "close": close,
        "high": 10.1, "low": 9.9, "volume": 100., "amount": 1000.,
    } for symbol in ("A", "B", "AB", "NO") for moment, close in [
        (AT.replace(hour=9, minute=31), 10.), (AT, 10.1),
        (AT + timedelta(minutes=1), 1.)]])


def run(**patch):
    args = dict(day=DAY, pool_symbols=["A", "AB", "B", "NO"],
                prev_close=dict.fromkeys(["A", "AB", "B", "NO"], 10.),
                completed_through=AT + timedelta(seconds=30), selection=snapshot())
    args.update(patch)
    return evaluate(bars(), signal(), **args)


def test_and_or_and_a_priority_do_not_consume_future_minute():
    result = run()
    assert not result.diagnostics
    assert result.symbols == ["A", "AB", "B"]
    assert [result.entry_matches[s]["entry_group"] for s in result.symbols] == ["A", "A", "B"]
    assert result.entry_matches["AB"]["selection_score"] == 80
    assert result.pool_total == 4


def test_group_missing_data_isolated_and_all_uncomputable_fails():
    saved = snapshot()
    saved["rows"][0]["scoring_inputs"]["momentum20"] = None
    result = run(selection=saved)
    assert result.symbols == ["AB", "B"]
    assert "A" in result.entry_group_errors
    assert not result.diagnostics
    for row in saved["rows"]:
        row["score"] = None
    assert run(selection=saved).diagnostics


def test_archive_validation_empty_selection_and_terminal_invalidation():
    saved = snapshot()
    assert run(selection={**saved, "rows": []}).stage == "conditions_rejected"
    assert run(selection={**saved, "source_date": "2026-11-18"}).diagnostics
    assert run(selection=None).diagnostics
    state = {}
    assert run(task_state=state).symbols == ["A", "AB", "B"]
    for row in saved["rows"]:
        row["score"] = 0
    result = run(task_state=state, selection=saved)
    assert result.symbols == [] and not result.diagnostics


@pytest.mark.parametrize("patch", [
    {"rank_min": 0}, {"rank_min": 2.5}, {"score_min": True},
    {"tech_min": float("nan")}, {"gap_min_pct": 11}, {"unknown": 1},
])
def test_invalid_parameters_rejected(patch):
    sig = signal()
    sig["params"]["entry_groups"]["a"].update(patch)
    with pytest.raises(ValueError):
        parameters(sig)


def test_disabled_and_empty_groups_and_legacy_compatibility():
    sig = signal()
    sig["params"]["entry_groups"] = {"a": {"enabled": True}, "b": {"enabled": False}}
    with pytest.raises(ValueError):
        parameters(sig)
    del sig["params"]["entry_groups"]
    assert "entry_groups" not in parameters(sig)


def test_api_preserves_groups_and_invalid_save_preserves_original(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), clear_cache=lambda: None)
    app.include_router(router)
    client = TestClient(app)
    original = signal()
    saved = client.post("/api/custom-signals", json=original)
    assert saved.status_code == 200, saved.text
    invalid = deepcopy(original)
    invalid["params"]["entry_groups"]["a"]["momentum_min_pct"] = 41
    assert client.post("/api/custom-signals", json=invalid).status_code == 400
    assert client.get("/api/custom-signals").json()["signals"][0]["params"] == original["params"]


def test_engine_restores_projected_group_order_before_limit(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "sample", "asset_types": ["stock"], "timeframes": ["1m"],
        "execution_backend": "minute_filter", "scoring_mode": "strategy", "limit": 2}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_late_session_strength_s03")).select(
        "symbol", pl.col("datetime").alias("last_datetime"), "close",
        pl.when(pl.col("symbol") == "B").then(100.).otherwise(1.).alias("score"))
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, signal())
    previous = pl.DataFrame({"symbol": ["A", "AB", "B", "NO"],
                             "date": [DAY - timedelta(days=1)] * 4, "close": [10.] * 4})
    context = StrategyDataContext(
        "stock", "1m", DAY, history=bars(), current=previous.with_columns(pl.lit(DAY).alias("date")),
        pool_history=previous, observed_at=AT + timedelta(seconds=30),
        selection_snapshots={"sample": snapshot()})
    result = engine.run("sample", context, overrides={"entry_signals": ["csgi_late_session_strength_s03"]})
    assert [row["symbol"] for row in result.rows] == ["A", "AB"]
    assert all(row["entry_group"] == "A" for row in result.rows)
    assert result.rows[0]["score"] > result.rows[1]["score"]
    assert result.rows[0]["strategy_score"] == 1.


def test_b_orders_by_emotion_and_null_conditions_do_not_load_missing_metrics():
    sig, saved = signal(), snapshot()
    sig["params"]["entry_groups"] = {
        "a": {"enabled": False}, "b": {"enabled": True, "score_min": 55}}
    for row in saved["rows"]:
        row["scoring_inputs"] = {}
    result = evaluate(bars(), sig, day=DAY, pool_symbols=["A", "AB", "B", "NO"],
                      prev_close={}, completed_through=AT + timedelta(seconds=30), selection=saved)
    assert result.symbols == ["NO", "AB", "A", "B"]
    assert [result.entry_matches[s]["entry_emotion_score"] for s in result.symbols] == [95, 80, 75, 65]
    assert not result.entry_group_errors
    for row in saved["rows"]:
        row["emotion_score"] = None
    missing = evaluate(bars(), sig, day=DAY, pool_symbols=["A", "AB", "B", "NO"],
                       prev_close={}, completed_through=AT + timedelta(seconds=30), selection=saved)
    assert not missing.symbols
    assert missing.diagnostics == ["A/B入场筛选: 全部候选缺少可计算的分组数据"]


def test_emotion_ties_use_selection_rank_then_symbol_and_a_stays_first():
    from app.strategy.entry_groups import match

    saved = snapshot()
    saved["rows"][0].update(score=100., emotion_score=0.)
    for row in saved["rows"][1:3]:
        row["emotion_score"] = 90.
    saved["rows"][3]["emotion_score"] = 50.
    saved["rows"].append({**saved["rows"][1], "symbol": "C_B", "fund_score": 1.})
    groups_only = {"a": {"enabled": True, "score_min": 99},
                   "b": {"enabled": True, "score_min": 55}}
    matches, errors, coverage = match(
        groups_only, saved, pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime}),
        day=DAY, at=AT, candidates={row["symbol"] for row in saved["rows"]}, previous={})
    assert list(matches) == ["A", "AB", "B", "C_B", "NO"]
    assert [matches[s]["entry_priority"] for s in matches] == [1, 2, 3, 4, 5]
    assert matches["A"]["entry_group"] == "A"
    assert not errors and coverage["passed"] == 5


def test_missing_emotion_isolated_but_unselected_funds_do_not_block_b():
    from app.strategy.entry_groups import match

    saved = snapshot()
    saved["rows"][0].update(score=100., emotion_score=None)
    saved["rows"][1]["fund_score"] = None
    saved["rows"][2]["emotion_score"] = None
    groups_only = {"a": {"enabled": True, "score_min": 99},
                   "b": {"enabled": True, "score_min": 55}}
    matches, errors, coverage = match(
        groups_only, saved, pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime}),
        day=DAY, at=AT, candidates={row["symbol"] for row in saved["rows"]}, previous={})
    assert list(matches) == ["A", "NO", "B"]
    assert set(errors) == {"AB"} and "emotion_score" in errors["AB"]
    # AB的A组输入完整、正常未命中, 因此仍计入可计算覆盖。
    assert coverage["usable"] == 4 and coverage["missing"] == 1


@pytest.mark.parametrize("phase", [None, "stocks"])
def test_ordering_policy_change_invalidates_group_terminal(monkeypatch, phase):
    from app.strategy import entry_groups

    monkeypatch.setattr(entry_groups, "ORDERING_POLICY", "old_order", raising=False)
    state = {}
    options = {"day": DAY, "pool_symbols": ["A", "AB", "B", "NO"],
               "prev_close": dict.fromkeys(["A", "AB", "B", "NO"], 10.),
               "completed_through": AT + timedelta(seconds=30),
               "selection": snapshot(), "task_state": state, "independent_phase": phase}
    assert evaluate(bars(), signal(), **options).symbols == ["A", "AB", "B"]
    stage = state if phase is None else state["independent_v1"]["stocks"]
    before_identity = stage["identity"]
    assert stage.get("terminal")
    monkeypatch.setattr(entry_groups, "ORDERING_POLICY", "new_emotion_order", raising=False)
    assert evaluate(bars(), signal(), **options).symbols == ["A", "AB", "B"]
    stage = state if phase is None else state["independent_v1"]["stocks"]
    assert stage["identity"] != before_identity


def test_engine_restores_emotion_priority_and_columns_before_limit(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "sample", "asset_types": ["stock"], "timeframes": ["1m"],
        "execution_backend": "minute_filter", "scoring_mode": "strategy", "limit": 3}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_late_session_strength_s03")).select(
        "symbol", pl.col("datetime").alias("last_datetime"), "close",
        pl.when(pl.col("symbol") == "B").then(100.).otherwise(1.).alias("score"))
''')
    sig = signal()
    sig["params"]["entry_groups"] = {
        "a": {"enabled": False}, "b": {"enabled": True, "score_min": 55}}
    custom_signals.save_one(tmp_path, sig)
    engine = StrategyEngine([custom])
    previous = pl.DataFrame({"symbol": ["A", "AB", "B", "NO"],
                             "date": [DAY - timedelta(days=1)] * 4, "close": [10.] * 4})
    context = StrategyDataContext(
        "stock", "1m", DAY, history=bars(), current=previous.with_columns(pl.lit(DAY).alias("date")),
        pool_history=previous, observed_at=AT + timedelta(seconds=30),
        selection_snapshots={"sample": snapshot()})
    result = engine.run("sample", context, overrides={"entry_signals": ["csgi_late_session_strength_s03"]})
    assert [row["symbol"] for row in result.rows] == ["NO", "AB", "A"]
    assert [row["entry_emotion_score"] for row in result.rows] == [95, 80, 75]
    assert [row["entry_priority"] for row in result.rows] == [1, 2, 3]
    assert result.rows[0]["score"] > result.rows[1]["score"] > result.rows[2]["score"]


def test_duplicate_member_and_invalid_open_are_isolated():
    saved = snapshot()
    saved["rows"].append(deepcopy(saved["rows"][0]))
    result = run(selection=saved)
    assert result.symbols == ["AB", "B"]
    assert "重复" in result.entry_group_errors["A"]
    frame = bars().filter(~((pl.col("symbol") == "A") & (pl.col("datetime").dt.hour() == 9)))
    result = evaluate(frame, signal(), day=DAY, pool_symbols=["A", "AB", "B", "NO"],
                      prev_close=dict.fromkeys(["A", "AB", "B", "NO"], 10.),
                      completed_through=AT + timedelta(seconds=30), selection=snapshot())
    assert result.symbols == ["AB", "B"]
    assert "gap_pct" in result.entry_group_errors["A"]
