"""S05 scores the complete, frozen S02 source without changing its archive."""
import gzip
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.daily_selections import due_window, selection_record, valid_selection
from app.strategy.basic_filter import previous_session

DAY = date(2026, 11, 26)
SOURCE = previous_session(DAY)
SID = "custom_mvxxcs05"


def inputs(count=52):
    sessions = [SOURCE]
    for _ in range(64):
        sessions.insert(0, previous_session(sessions[0]))
    symbols = [f"600{i:03d}.SH" for i in reversed(range(1, count + 1))]
    snapshot = {"as_of": str(DAY), "source_date": str(SOURCE),
                "identity": "real-s02-identity",
                "scheduled_for": f"{SOURCE}T21:00:00+08:00",
                "generated_at": f"{SOURCE}T21:12:00+08:00",
                "rows": [{"symbol": s, "reference_close": 10.64} for s in symbols]}
    daily = pl.DataFrame([
        {"symbol": s, "date": day, "close": 10 + i / 100, "raw_close": 10 + i / 100,
         "high": 11., "low": 9., "amount": 1e8}
        for i, day in enumerate([*sessions, DAY]) for s in symbols])
    return snapshot, daily


def test_top50_sorted_and_source_untouched():
    from app.services.s05_selection import WEIGHTS, freeze_selection

    snapshot, daily = inputs()
    out = freeze_selection(snapshot, daily, pl.DataFrame(), {}, DAY)
    assert len(snapshot["rows"]) == 52
    assert "scoring_policy" not in snapshot
    assert [r["symbol"] for r in out["rows"]] == [f"600{i:03d}.SH" for i in range(1, 51)]
    assert [r["s02_rank"] for r in out["rows"]] == list(range(1, 51))
    assert WEIGHTS == {"tech": .4, "fund": .3, "emotion": .25, "fundamental": .05}
    row = out["rows"][0]
    assert row["s02_score"] == pytest.approx(sum(row[f"s02_{k}_score"] * w for k, w in WEIGHTS.items()))
    assert row["s02_fund_score"] == row["s02_fundamental_score"] == 50
    assert out["score_coverage"]["scorable_total"] == 52
    assert out["score_coverage"]["missing_components"]
    assert out["entry_branch"] in ("tail", "low_gap", "board")
    assert str(DAY) not in {r["date"] for r in out["market_breadth"]}


def test_partial_bad_reference_is_isolated_all_bad_and_duplicates_fail():
    from app.services.s05_selection import freeze_selection

    snapshot, daily = inputs(2)
    snapshot["rows"][0]["reference_close"] = 9.
    out = freeze_selection(snapshot, daily, pl.DataFrame(), {}, DAY)
    assert len(out["rows"]) == 1
    assert snapshot["rows"][0]["symbol"] in out["score_coverage"]["invalid_symbols"]
    snapshot["rows"][1]["reference_close"] = 9.
    with pytest.raises(ValueError, match="全部"):
        freeze_selection(snapshot, daily, pl.DataFrame(), {}, DAY)
    snapshot["rows"][1] = snapshot["rows"][0].copy()
    with pytest.raises(ValueError, match="重复"):
        freeze_selection(snapshot, daily, pl.DataFrame(), {}, DAY)


def test_daily_selection_and_replay_rank_technical_and_fund_candidates_identically():
    from app.services.s05_scoring import freeze_selection as replay_selection
    from app.services.s05_selection import freeze_selection

    snapshot, daily = inputs(2)
    funds_symbol = "600002.SH"
    daily = daily.with_columns(*(
        pl.when(pl.col("symbol") == funds_symbol).then(10.64).otherwise(pl.col(col)).alias(col)
        for col in ("close", "raw_close")))
    flows = daily.select("symbol", "date").with_columns(
        pl.when(pl.col("symbol") == funds_symbol).then(500000.).otherwise(-2000000.)
        .alias("super_large_net_inflow"), pl.lit(0.).alias("large_net_inflow"))
    replayed = replay_selection(snapshot, daily, flows, {}, DAY)
    archived = freeze_selection(snapshot, daily, flows, {}, DAY)
    assert archived["rows"][0]["symbol"] == "600001.SH"
    assert archived["rows"] == sorted(replayed["rows"], key=lambda row: row["s02_rank"])


def test_empty_source_no_io_and_dependency_digest_is_checked():
    from app.services.s05_selection import POLICY, compute_selection, freeze_source

    snapshot, _ = inputs(0)
    dependency = freeze_source(snapshot, SOURCE, DAY)
    now = datetime(2026, 11, 26, 16, tzinfo=CN_TZ)
    out = compute_selection(SimpleNamespace(repo=None), SOURCE, DAY, dependency, now)
    assert out["rows"] == []
    assert out["scoring_policy"] == POLICY
    assert out["source_selection"] == dependency
    assert out["generated_at"] == now.isoformat()
    assert out["scheduled_for"] == f"{SOURCE}T22:00:00+08:00"
    assert dependency["snapshot"]["scheduled_for"] == f"{SOURCE}T21:00:00+08:00"
    assert valid_selection(out, DAY, strategy_id=SID)
    with pytest.raises(ValueError, match="摘要"):
        compute_selection(SimpleNamespace(repo=None), SOURCE, DAY, {**dependency, "sha256": "wrong"}, now)
    with pytest.raises(ValueError, match="等待S02"):
        compute_selection(SimpleNamespace(repo=None), SOURCE, DAY, None, now)
    with pytest.raises(ValueError):
        freeze_source({**snapshot, "generated_at": f"{SOURCE}T22:00:01+08:00"}, SOURCE, DAY)


def test_s05_policy_slot_rejects_legacy_and_wrong_weights():
    from app.services.daily_selections import selection_identity
    from app.services.s05_selection import POLICY, WEIGHTS

    snapshot, _ = inputs(0)
    current = {**snapshot, "scoring_policy": POLICY, "scoring_weights": WEIGHTS}
    old = {**snapshot, "entry_routing": "s02_173004552_v1"}
    records = {selection_identity(SID, policy="s02_173004552_v1").record_id(str(DAY)):
               {"result": {"selection_snapshot": old}}}
    assert selection_record(SID, DAY, records.get, policy=POLICY) is None
    records[selection_identity(SID, policy=POLICY).record_id(str(DAY))] = {
        "result": {"selection_snapshot": current}}
    assert selection_record(SID, DAY, records.get, policy=POLICY)["result"]["selection_snapshot"] == current
    assert not valid_selection({**current, "scoring_weights": {"tech": WEIGHTS["tech"]}}, DAY, strategy_id=SID)
    assert not valid_selection({**current, "rows": [{}] * 51}, DAY, strategy_id=SID)
    assert due_window(datetime(2026, 11, 26, 23, 0, 1, tzinfo=CN_TZ),
                      monitored_only=True, strategy_id=SID) is None


@pytest.mark.parametrize("clock,eligible", [
    ((21, 0, 0), False), ((21, 59, 59), False),
    ((22, 0, 0), True), ((22, 0, 1), True),
    ((22, 59, 59), True), ((23, 0, 0), False), ((23, 0, 1), False),
])
def test_s05_natural_window_boundaries(clock, eligible):
    now = datetime(2026, 11, 26, *clock, tzinfo=CN_TZ)
    actual = due_window(now, monitored_only=True, strategy_id=SID)
    assert actual == ((DAY, date(2026, 11, 27)) if eligible else None)


def test_new_selection_replaces_stale_select_in_day_view_preserving_buy_sell(tmp_path):
    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import POLICY, WEIGHTS
    from app.services.strategy_results import ResultIdentity, StrategyResults

    identity = ResultIdentity(SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    snapshot, _ = inputs(0)
    store = StrategyResults(tmp_path)
    old = store.save(identity, {
        "as_of": str(DAY), "section_mode": "signals", "rows": [], "total": 0,
        "selection_snapshot": {**snapshot, "rows": [{"symbol": "OLD"}]},
        "sections": {"sell": [{"symbol": "SELL"}], "select": [{"symbol": "OLD"}],
                     "buy": [{"symbol": "BUY"}]},
    }, started_at=1)
    DailySelections(tmp_path).save(identity, {
        **snapshot, "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
    }, started_at=2)
    view = json.loads(gzip.decompress(store.day_view(SID, str(DAY))[1]))["result"]
    assert view["sections"]["select"] == []
    assert view["sections"]["sell"] == old["result"]["sections"]["sell"]
    assert view["sections"]["buy"] == [
        {**row, "signal_score": None, "signal_score_rule": None}
        for row in old["result"]["sections"]["buy"]]
    assert store.detail(SID, old["record_id"])["result"]["sections"]["buy"] == old["result"]["sections"]["buy"]


def test_no_route_archive_is_separate_and_confirms_empty_next_sell(tmp_path):
    from app.services.daily_selections import DailySelections
    from app.services.s05_selection import NUMERIC_POLICY, POLICY, WEIGHTS
    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import ResultIdentity, StrategyResults
    from app.strategy.overnight_runtime import next_market_day
    from app.strategy.s02_173004552 import MARKET_DEFAULTS, freeze_market_route, sessions_until

    identity = ResultIdentity(SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    snapshot, _ = inputs(1)
    snapshot.update(scoring_policy=POLICY, scoring_weights=WEIGHTS, entry_routing="s02_173004552_v1",
                    entry_branch="board", market_breadth=[
                        {"date": str(d), "up": .13, "above_ma20": .08} for d in sessions_until(DAY)])
    selections = DailySelections(tmp_path)
    store = StrategyResults(tmp_path)
    old = selections.save(identity, snapshot, started_at=1)
    store.save(identity, {
        "as_of": str(DAY), "section_mode": "signals", "rows": [], "total": 0,
        "sections": {"sell": [], "select": snapshot["rows"], "buy": []},
        "section_errors": {"buy": "等待入场", "sell": "缺档"},
    }, started_at=2)
    newer = selections.save(identity, freeze_market_route(
        snapshot, DAY, MARKET_DEFAULTS, observed=datetime(2026, 11, 26, 12, tzinfo=CN_TZ)), started_at=3)
    assert old["record_id"] != newer["record_id"]
    archived = selections.read(SID, DAY, policy=POLICY)
    assert archived["entry_branch"] == "board" and "entry_route" not in archived
    assert selections.read(SID, DAY, policy=NUMERIC_POLICY)["entry_branch"] is None
    view = json.loads(gzip.decompress(store.day_view(SID, str(DAY))[1]))["result"]
    assert view["sections"]["select"] == snapshot["rows"]
    assert view["sections"]["buy"] == []
    assert view["section_statuses"]["buy"] == "rejected"
    assert "buy" not in view["section_errors"]
    assert view["monitor_tasks"]["buy"]["executed"] is False
    assert sell_roster(tmp_path, SID, next_market_day(DAY))["rows"] == []


def test_scheduled_s05_freezes_market_thresholds_from_stored_signals(monkeypatch):
    from app.services import s05_scoring as reference
    from app.services.daily_selections import compute_selections
    from app.services.s05_selection import POLICY as SCORING_POLICY
    from app.services.s05_selection import WEIGHTS, freeze_source
    from app.strategy.custom_signals import private_signal_registry
    from app.strategy.s02_173004552 import ENTRY_IDS, POLICY, sessions_until

    snapshot, _ = inputs(0)
    dependency = freeze_source(snapshot, SOURCE, DAY)
    monkeypatch.setattr(reference, "compute_selection", lambda *args, **kwargs: {
        **snapshot, "market_breadth": [
            {"date": str(d), "up": .45, "above_ma20": .46} for d in sessions_until(DAY)],
        "entry_routing": POLICY, "entry_branch": "board",
        "scoring_policy": SCORING_POLICY, "scoring_weights": WEIGHTS,
        "score_coverage": {"candidate_total": 0, "scorable_total": 0, "ranked_total": 0},
    })
    with private_signal_registry([{"id": ENTRY_IDS["board"], "params": {"s05_entry": {"prior_up_min_pct": 40}}}]):
        selected = compute_selections(None, SimpleNamespace(repo=None), SOURCE, DAY, [SID], {},
            source_selection=dependency, now=datetime(2026, 11, 25, 21, 30, tzinfo=CN_TZ))
    assert selected["errors"] == {}
    frozen = selected["selections"][SID]
    assert frozen["entry_branch"] == "board"
    assert frozen["entry_route"]["thresholds"]["board"]["prior_up_min_pct"] == 40
