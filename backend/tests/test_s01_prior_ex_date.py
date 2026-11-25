"""Restore the old ex-date exclusion through frozen and legacy S01 inputs."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
import test_s01_independent_entry as independent
from test_s01_independent_entry import DAY, FROZEN, OVERRIDES, SID, minutes

from app.data_providers.corporate_actions import CorporateAction
from app.services.corporate_actions import save_actions
from app.services.screener import ScreenerService
from app.strategy.signal_sections import select_rows

PRIOR = date(2026, 11, 20)
engine = independent.engine


def events(tmp_path):
    save_actions(tmp_path, [
        CorporateAction(id=symbol, symbol=symbol, source="fixture", ex_date=day,
                        cash_per_share=.1, dates_verified=False)
        for symbol, day in (("600001.SH", PRIOR), ("600002.SH", DAY),
                            ("600003.SH", date(2026, 11, 19)))
    ])
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))


def test_s01_declares_entry_exclusion_without_daily_dependency(engine):
    assert engine.get(SID).meta.get("entry_prior_ex_date_exclusion") is True
    assert engine.minute_entry_daily_history_bars([SID], overrides_map={SID: OVERRIDES}) == 0


@pytest.mark.parametrize("flag,expected", [(True, 0), (False, 1), (None, 1)])
def test_frozen_prior_ex_date_excludes_only_flagged_entries_and_keeps_score(engine, flag, expected):
    strategy = engine.get(SID)
    selection = deepcopy(FROZEN)
    selection["rows"][0]["reference_prior_ex_date"] = flag
    frame = minutes().filter(pl.col("datetime").dt.minute() != 46)
    result = strategy.filter_minute_history_fn(frame, {"_s01_skip_entry": True}, selection=selection)
    assert result.height == expected
    if expected:
        baseline = strategy.filter_minute_history_fn(frame, {"_s01_skip_entry": True}, selection=FROZEN)
        assert result["score"].to_list() == baseline["score"].to_list()


def test_selection_excludes_exact_source_ex_date_and_preserves_full_input(engine, tmp_path, monkeypatch):
    repo = events(tmp_path)
    repo.get_instruments_asset = lambda *_: pl.DataFrame()
    service = ScreenerService(repo)
    symbols = ["600001.SH", "600002.SH", "600003.SH"]
    prior = pl.DataFrame({"symbol": symbols, "date": [date(2026, 11, 19)] * 3,
                         "close": [10.] * 3})
    source = pl.DataFrame({
        "symbol": symbols, "date": [PRIOR] * 3, "close": [10.] * 3,
        "high": [10.2] * 3, "low": [10.] * 3, "amount": [1e8] * 3,
    })
    history = pl.concat([prior, source], how="diagonal_relaxed")
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: history)
    monkeypatch.setattr("app.services.daily_close_validation.validate_daily_close", lambda *_a, **_k: {})
    context = service.build_selection_context(engine, PRIOR, DAY, [SID], overrides_map={SID: OVERRIDES})
    rows = select_rows(engine, engine.get(SID), context, overrides={"basic_filter": {"enabled": False}})
    assert set(context.current["symbol"]) == set(symbols)
    assert {r["symbol"] for r in rows} == {"600002.SH", "600003.SH"}
    assert {r["symbol"]: r.get("reference_prior_ex_date") for r in rows} == {
        "600002.SH": False, "600003.SH": False,
    }
    assert {r["reference_date"] for r in rows} == {str(PRIOR)}
    context.current = context.current.filter(pl.col("symbol") == "600001.SH")
    assert select_rows(engine, engine.get(SID), context,
                       overrides={"basic_filter": {"enabled": False}}) == []


def test_legacy_selection_uses_bound_events_without_daily_or_pool_reads(engine, tmp_path, monkeypatch):
    repo = events(tmp_path)
    repo.get_minute_by_dates = lambda symbols, days: minutes()
    service = ScreenerService(repo)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: pytest.fail("daily loaded"))
    selection = deepcopy(FROZEN)
    selection["rows"] += [
        {"symbol": "600002.SH", "reference_close": 10., "reference_prior_ex_date": True},
        {"symbol": "600003.SH", "reference_close": 10., "reference_prior_ex_date": False},
    ]
    before = deepcopy(selection)
    context = service.build_selected_stock_context(
        engine, DAY, SID, selection, current=None, overrides=OVERRIDES, observed_at=None)
    assert selection == before
    frozen = context.selection_snapshots[SID]
    assert [row.get("reference_prior_ex_date") for row in frozen["rows"]] == [True, True, False]
    assert context.daily_history is None
    assert engine.get(SID).filter_minute_history_fn(
        context.history, {"_s01_skip_entry": True}, selection=frozen).is_empty()


def test_saved_flags_need_no_event_read_and_other_strategies_do_not_opt_in(engine, monkeypatch):
    monkeypatch.setattr("app.services.corporate_actions.load_actions",
                        lambda *_a, **_k: pytest.fail("events loaded"))
    selection = deepcopy(FROZEN)
    selection["rows"][0]["reference_prior_ex_date"] = False
    service = ScreenerService(SimpleNamespace(get_minute_by_dates=lambda *_: minutes()))
    context = service.build_selected_stock_context(
        engine, DAY, SID, selection, current=None, overrides=OVERRIDES, observed_at=None)
    assert context.selection_snapshots[SID] == selection
    engine.get(SID).meta.pop("entry_prior_ex_date_exclusion", None)
    context = service.build_selected_stock_context(
        engine, DAY, SID, FROZEN, current=None, overrides=OVERRIDES, observed_at=None)
    assert context.selection_snapshots[SID] == FROZEN


def test_event_annotation_is_member_and_date_bounded(tmp_path):
    from app.strategy.overnight_runtime import annotate_prior_ex_dates

    repo = events(tmp_path)
    frame = pl.DataFrame({"symbol": ["600001.SH", "600001.SH", "600002.SH"],
                          "date": [PRIOR, DAY, PRIOR]})
    annotated = annotate_prior_ex_dates(frame, repo)
    assert annotated["prior_ex_date"].to_list() == [True, False, False]
    assert annotate_prior_ex_dates(pl.DataFrame(), repo).is_empty()
    assert "prior_ex_date" not in frame.columns
