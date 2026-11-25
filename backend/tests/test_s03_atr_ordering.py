"""历史冻结ATR排序声明的兼容回归; 当前安装源沿A/B资金排序。"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from test_entry_groups import AT, DAY, bars, signal, snapshot

from app.services.s03_selection import enrich_candidates, ranked_result
from app.strategy import custom_signals
from app.strategy.candidate_order import candidate_pool_order
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.late_session_strength import evaluate

POLICY = "s03_selection_atr_desc_v1"
SID = "custom_mvxxcs03"
SOURCE = Path(__file__).resolve().parents[2] / "examples/strategies/s03.py"


def saved_selection():
    saved = snapshot()
    saved["selection_ordering"] = POLICY
    for row in saved["rows"]:
        row["atr14_previous_close"] = {"A": .02, "AB": .04, "B": .08, "NO": .09}[row["symbol"]]
    return saved


def options(saved, state=None, phase=None):
    return {"day": DAY, "pool_symbols": ["A", "AB", "B", "NO"],
            "prev_close": dict.fromkeys(["A", "AB", "B", "NO"], 10.),
            "completed_through": AT + timedelta(seconds=30), "selection": saved,
            "task_state": state, "independent_phase": phase}


def test_enrichment_orders_only_top30_and_preserves_five_factor_ranks():
    ranked = ranked_result([
        {"symbol": f"{i:06}.SH", "score": 100. - i} for i in range(1, 32)], {}, 31)
    original = deepcopy(ranked)

    def enrich(rows, params, *, daily, as_of):
        return rows.reverse().with_columns(
            pl.lit(999.).alias("score"),
            pl.Series("atr14_previous_close", [.9 if i == 30 else i / 100
                                              for i in range(30, 0, -1)]))

    strategy = SimpleNamespace(enrich_selection_fn=enrich, meta={"selection_ordering": POLICY})
    engine = SimpleNamespace(get=lambda sid: strategy, resolve_params=lambda *args: {})
    actual = enrich_candidates(engine, SimpleNamespace(daily_history=None, as_of=DAY), ranked, {})
    assert actual["selection_ordering"] == POLICY
    assert [r["symbol"] for r in actual["rows"]] == [f"{i:06}.SH" for i in range(30, 0, -1)]
    assert [r["selection_rank"] for r in actual["rows"]] == list(range(30, 0, -1))
    assert [r["score"] for r in actual["rows"]] == [100. - i for i in range(30, 0, -1)]
    assert ranked == original
    empty = enrich_candidates(engine, SimpleNamespace(), ranked_result([], {}, 0), {})
    assert empty["selection_ordering"] == POLICY


def test_matches_follow_atr_subset_order_and_dual_match_stays_a():
    saved = saved_selection()
    original = deepcopy(saved)
    result = evaluate(bars(), signal(), **options(saved))
    assert result.symbols == ["B", "AB", "A"]
    assert [result.entry_matches[s]["entry_group"] for s in result.symbols] == ["B", "A", "A"]
    assert [result.entry_matches[s]["selection_rank"] for s in result.symbols] == [8, 6, 3]
    assert saved == original
    del saved["selection_ordering"]
    assert evaluate(bars(), signal(), **options(saved)).symbols == ["A", "AB", "B"]


def test_invalid_atr_is_not_a_screening_condition_and_ties_use_code():
    from app.strategy.entry_groups import match

    saved = saved_selection()
    values = [.05, .05, 0, None, float("nan"), float("inf"), -.1, True]
    saved["rows"] = [{**saved["rows"][0], "symbol": f"{i:06}.SH",
                      "selection_rank": i, "atr14_previous_close": value,
                      "emotion_score": None} for i, value in enumerate(values, 1)]
    saved["rows"].reverse()
    sig_groups = {"a": {"enabled": False}, "b": {"enabled": True, "score_min": 55}}
    matches, errors, coverage = match(
        sig_groups, saved, pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime}),
        day=DAY, at=AT, candidates={r["symbol"] for r in saved["rows"]}, previous={})
    assert list(matches) == [f"{i:06}.SH" for i in range(1, 9)]
    assert coverage["passed"] == 8 and not errors
    # Missing enabled screening inputs are still isolated.
    saved["rows"][0]["score"] = None
    matches, errors, _ = match(
        sig_groups, saved, pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime}),
        day=DAY, at=AT, candidates={r["symbol"] for r in saved["rows"]}, previous={})
    assert "000008.SH" not in matches and "selection_score" in errors["000008.SH"]


@pytest.mark.parametrize("phase", [None, "stocks"])
def test_new_marker_invalidates_completed_identity_without_mutating_archive(phase):
    saved = saved_selection()
    del saved["selection_ordering"]
    state = {}
    assert evaluate(bars(), signal(), **options(saved, state, phase)).symbols == ["A", "AB", "B"]
    stage = state if phase is None else state["independent_v1"]["stocks"]
    old = stage["identity"]
    saved["selection_ordering"] = POLICY
    assert evaluate(bars(), signal(), **options(saved, state, phase)).symbols == ["B", "AB", "A"]
    stage = state if phase is None else state["independent_v1"]["stocks"]
    assert stage["identity"] != old


@pytest.mark.parametrize("current", [True, False])
def test_installed_source_applies_current_policy_to_private_snapshot(tmp_path, current):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    # 只在本测试的私有副本声明历史口径, 不改变当前安装源或输入归档。
    source = SOURCE.read_text().replace('"s03_ab_fund_v1"', f'"{POLICY}"')
    if not current:
        source = source.replace('"selection_ordering": "s03_selection_atr_desc_v1",', "")
    (custom / "s03.py").write_text(source)
    custom_signals.save_one(tmp_path, signal())
    engine = StrategyEngine([custom])
    previous = pl.DataFrame({"symbol": ["A", "AB", "B", "NO"],
                             "date": [DAY - timedelta(days=1)] * 4, "close": [10.] * 4})
    saved = saved_selection()
    del saved["selection_ordering"]
    original = deepcopy(saved)
    context = StrategyDataContext(
        "stock", "1m", DAY, history=bars(), daily_history=previous, pool_history=previous,
        current=previous.with_columns(pl.lit(DAY).alias("date")),
        observed_at=AT + timedelta(seconds=30), selection_snapshots={SID: saved})
    result = engine.run(SID, context, overrides={
        "basic_filter": {"enabled": False, "target_mode": "all"},
        "entry_signals": ["csgi_late_session_strength_s03"], "exit_signals": []})
    expected = ["B", "AB", "A"] if current else ["A", "AB", "B"]
    assert [r["symbol"] for r in result.rows] == expected
    assert [r["entry_priority"] for r in result.rows] == [1, 2, 3]
    assert all(r["strategy_score"] == 80. for r in result.rows)
    assert all(str(r["last_datetime"]).replace(" ", "T") == AT.isoformat() for r in result.rows)
    assert result.rows[0]["atr14_previous_close"] == (.08 if current else .02)
    assert saved == original


def test_candidate_projection_uses_atr_order_without_replacing_selection_rank():
    saved = saved_selection()
    result = SimpleNamespace(
        as_of=DAY, section_mode="signals", sections={"select": saved["rows"]},
        selection_snapshot={**saved, "total": 4})
    ordered = candidate_pool_order(result, SID, {})
    assert list(ordered) == ["NO", "B", "AB", "A"]
    assert ordered["B"]["candidate_pool_rule"] == POLICY
    assert saved["rows"][1]["selection_rank"] == 8
