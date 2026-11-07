"""Suggested prices are frozen display data, independent of live quotes or fills."""
# ruff: noqa: F401, F811 -- reuse the existing pytest signal fixture.
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from test_minute_signal_sections import scan, signal_result

from app.services.daily_selections import DailySelections, compute_selections
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.monitor import MonitorRuleEngine


def identity():
    return ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")


def reference(closes):
    return {"reference_date": "2026-10-30", "closes": closes}


def test_engine_and_monitor_generate_frozen_lots_and_exact_raw_reference(scan):
    engine, context, overrides = scan
    context = replace(context, daily_history=context.daily_history.with_columns(
        pl.lit(20.).alias("raw_close")))
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.rows[0]["close"] == 10.3
    assert result.rows[0].get("suggested_lots_100k") == 97
    assert result.rows == result.sections["buy"]
    refs = asdict(result).get("price_suggestion_reference")
    assert refs == reference({symbol: 20. for symbol in context.daily_history["symbol"]})
    assert MonitorRuleEngine._strategy_result_payload(result).get("price_suggestion_reference") == refs


@pytest.mark.parametrize("price,lots", [(10., 100), (10.01, 99), (1000., 1),
                                       (1000.01, 0), (None, None), (0, None), (-1, None)])
def test_archive_saves_lots_in_both_buy_views_without_changing_source(tmp_path, price, lots):
    source = {**signal_result("2026-11-02", [{"symbol": "A", "close": price}]),
              "price_suggestion_reference": reference({})}
    original = deepcopy(source)
    saved = StrategyResults(tmp_path).save(identity(), source, started_at=1)
    assert saved["result"]["rows"][0].get("suggested_lots_100k", "absent") == lots
    assert saved["result"]["sections"]["buy"][0]["suggested_lots_100k"] == lots
    assert saved["ever_rows"]["A"]["suggested_lots_100k"] == lots
    assert "price_suggestion_reference" not in saved["result"]
    assert source == original


def test_archive_uses_prior_daily_close_not_prior_trigger_and_survives_restart(tmp_path):
    store = StrategyResults(tmp_path)
    prior = store.save(identity(), signal_result("2026-10-30", [
        {"symbol": "A", "close": 9., "last_datetime": "2026-10-30T14:30:00"},
        {"symbol": "MISSING", "close": 15.},
    ]), started_at=1)
    payload = {**signal_result("2026-11-02"),
               "price_suggestion_reference": reference({"A": 10.25})}
    saved = store.save(identity(), payload, started_at=2)
    sell = saved["result"]["sections"]["sell"]
    assert sell[0].get("suggested_sell_price") == {
        "lower": 9.64, "upper": 10.87, "reference_date": "2026-10-30", "reference_close": 10.25,
    }
    assert sell[0]["close"] == 9.
    assert sell[0]["entry_record_id"] == prior["record_id"]
    assert sell[1]["suggested_sell_price"] is None
    assert StrategyResults(tmp_path).read(identity(), "2026-11-02") == saved
    assert store.save(identity(), payload, started_at=2) == saved
    assert store.detail("custom_test", prior["record_id"]) == prior


@pytest.mark.parametrize("reference_day", [date(2026, 10, 29), date(2026, 11, 2)])
def test_engine_never_borrows_older_or_current_daily_close(scan, reference_day):
    engine, context, overrides = scan
    context = replace(context, daily_history=context.daily_history.with_columns(
        pl.lit(reference_day).alias("date")))
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.rows == []
    assert result.entry_signal_hits == []
    assert result.price_suggestion_reference["closes"] == {}


def test_unknown_display_reference_does_not_invalidate_entries_without_price_scoring(scan):
    engine, context, overrides = scan
    overrides = {**overrides, "scoring": {}, "scoring_replace": True}
    original = engine.run("custom_test", context, overrides=overrides)
    context = replace(context, daily_history=context.daily_history.with_columns(
        pl.lit(date(2026, 10, 29)).alias("date")))
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.rows == original.rows
    assert len(result.entry_signal_hits) == 2
    assert result.price_suggestion_reference["closes"] == {}
    assert result.section_statuses["buy"] == "success"


def test_preselection_generates_sell_interval_and_does_not_archive_market_map(tmp_path):
    store = StrategyResults(tmp_path)
    store.save(identity(), signal_result("2026-10-30", [{"symbol": "A", "close": 9.}]), started_at=1)
    snapshot = {"as_of": "2026-11-02", "source_date": "2026-10-30", "rows": [],
                "generated_at": "2026-10-30T16:00:00+08:00",
                "price_suggestion_reference": reference({"A": 10.})}
    original = deepcopy(snapshot)
    saved = DailySelections(tmp_path).save(identity(), snapshot, started_at=2)
    assert saved["result"]["sections"]["sell"][0].get("suggested_sell_price") == {
        "lower": 9.4, "upper": 10.6, "reference_date": "2026-10-30", "reference_close": 10.,
    }
    assert "price_suggestion_reference" not in saved["result"]["selection_snapshot"]
    assert "price_suggestion_reference" not in saved["result"]
    assert snapshot == original


def test_compute_preselection_passes_exact_reference_for_next_session(scan):
    engine, context, overrides = scan
    service = SimpleNamespace(build_selection_context=lambda *_args, **_kwargs: context)
    generated = compute_selections(engine, service, date(2026, 10, 30), context.as_of,
                                   ["custom_test"], {"custom_test": overrides})
    assert generated["errors"] == {}
    assert generated["selections"]["custom_test"].get("price_suggestion_reference") == reference({
        symbol: close for symbol, close in
        context.daily_history.select("symbol", "close").iter_rows()
    })
