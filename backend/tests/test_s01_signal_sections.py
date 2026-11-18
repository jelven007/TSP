"""S01 reuses durable signal lists without reducing its market breadth."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from test_s01_optional_triggers import context, optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import BUILTINS, bars, daily

from app.services.daily_selections import signal_strategy
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine


@pytest.fixture
def scan():
    engine = StrategyEngine([BUILTINS])
    strategy = optional(engine)
    strategy.meta.update(result_sections="signals", entry_window_policy="available_minutes_v1")
    ctx = context()
    second = daily().with_columns(pl.lit("600002.SH").alias("symbol"))
    ctx.daily_history = pl.concat([daily(), second])
    ctx.current = ctx.daily_history.filter(pl.col("date") == ctx.daily_history["date"].max())
    ctx.history = pl.concat([bars(price=10.25), bars(symbol="600002.SH", price=10.25)])
    saved = {"as_of": str(ctx.as_of), "source_date": "2026-08-24",
             "rows": [{"symbol": "600001.SH", "score": None}]}
    engine._selection_loader = lambda *_: saved
    entry = {**ENTRY, "params": {**ENTRY["params"], "market_min_valid": 2}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        yield engine, strategy, ctx, saved


def test_native_entry_reads_complete_breadth_then_limits_buys_to_saved_members(scan):
    engine, strategy, ctx, saved = scan
    assert signal_strategy(strategy)
    result = engine.run("s01", ctx, overrides={"display_limit": 0})
    assert result.section_mode == "signals"
    assert result.sections["select"] == saved["rows"]
    assert [row["symbol"] for row in result.sections["buy"]] == ["600001.SH"]
    assert result.total == 1
    assert set(result.daily_stock_pool["symbols"]) == {"600001.SH", "600002.SH"}
    assert result.rows == result.sections["buy"]
    assert not result.price_suggestion_reference
    assert "suggested_lots_100k" not in result.rows[0]


@pytest.mark.parametrize("saved", [None, {"as_of": "2026-08-25", "source_date": "2026-08-24", "rows": []}])
def test_missing_or_empty_selection_keeps_holding_exit_independent(scan, saved):
    engine, _, ctx, _ = scan
    engine._selection_loader = lambda *_: saved
    ctx.positions = [{"symbol": "600002.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "sell_date": str(ctx.as_of)}]
    result = engine.run("s01", ctx)
    assert result.sections["buy"] == []
    assert result.exit_signal_hits[0]["symbol"] == "600002.SH"
    assert bool(result.section_errors.get("select")) is (saved is None)
    assert bool(result.section_errors.get("buy")) is (saved is None)


def test_missing_minutes_preserves_frozen_selection_with_waiting_buy(scan):
    engine, _, ctx, saved = scan
    result = engine.run("s01", replace(ctx, history=None))
    assert result.sections["select"] == saved["rows"]
    assert result.sections["buy"] == []
    assert result.section_statuses["buy"] == "waiting"
    assert "分钟" in result.section_errors["buy"]


def test_old_optional_source_retains_its_unsectioned_contract(scan):
    engine, strategy, ctx, _ = scan
    strategy.meta.pop("result_sections")
    assert not signal_strategy(strategy)
    result = engine.run("s01", ctx)
    assert result.sections is None
    assert result.total == 2


def test_partial_minute_prefix_is_not_a_confirmed_zero_before_native_entry(scan):
    engine, _, ctx, _ = scan
    ctx.completed_through = datetime(2026, 8, 25, 14, 44)
    ctx.history = ctx.history.filter(pl.col("datetime") <= ctx.completed_through)
    result = engine.run("s01", ctx)
    assert result.sections["buy"] == []
    assert result.section_statuses["buy"] == "waiting"
    assert "14:45" in result.section_errors["buy"]


def test_s01_sell_archive_reuses_previous_buy_without_s02_price_suggestions(scan, tmp_path):
    engine, _, ctx, _ = scan
    store = StrategyResults(tmp_path)
    key = ResultIdentity("s01", "s01", "v1", "{}", "{}", "null", "stock", "1m")
    prior = store.save(key, {"as_of": "2026-08-24", "rows": [{"symbol": "600001.SH"}],
                            "total": 1}, started_at=1)
    result = engine.run("s01", ctx)
    from dataclasses import asdict

    from app.api.screener import _safe

    saved = store.save(key, _safe(asdict(result)), started_at=2)
    assert saved["result"]["sections"]["sell"] == [{
        "symbol": "600001.SH", "entry_date": "2026-08-24", "entry_record_id": prior["record_id"],
    }]
    assert "suggested_sell_price" not in saved["result"]["sections"]["sell"][0]


@pytest.mark.parametrize("now,status,count", [
    (datetime(2026, 8, 25, 14, 45), "waiting", 0),
    (datetime(2026, 8, 25, 14, 46), "success", 1),
])
def test_saved_timed_entry_keeps_confirmation_and_complete_market(scan, now, status, count):
    engine, _, ctx, _ = scan
    entry = {**ENTRY, "params": {**ENTRY["params"], "market_min_valid": 2,
             "time": "14:45:00", "evaluation_delay_minutes": 1,
             "market_validity": "price_amount_prev_close_v2"}}
    ctx.observed_at = now
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = engine.run("s01", ctx)
    assert result.section_statuses["buy"] == status
    assert result.total == count
    if count:
        assert result.entry_data_quality["csgi_" + ENTRY["id"]]["market_valid_count"] == 2
        assert result.rows[0]["last_datetime"] == datetime(2026, 8, 25, 14, 45)
        assert result.rows[0]["entry_evaluated_at"] == now


@pytest.mark.parametrize("seconds,expired", [(0, False), (.001, True)])
def test_s01_live_deadline_preserves_sell_and_explicit_history(scan, seconds, expired):
    engine, _, ctx, saved = scan
    ctx.observed_at = datetime(2026, 8, 25, 14, 55) + timedelta(seconds=seconds)
    ctx.realtime = True
    ctx.positions = [{"symbol": "600002.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "sell_date": str(ctx.as_of)}]
    result = engine.run("s01", ctx)
    assert result.entry_deadline_at == "2026-08-25T14:55:00"
    assert (result.section_statuses["buy"] == "expired") is expired
    assert bool(result.entry_signal_hits) is not expired
    assert result.sections["select"] == saved["rows"]
    assert result.exit_signal_hits
    assert engine.run("s01", replace(ctx, realtime=False)).total == 1


def test_no_valid_timed_market_is_waiting_instead_of_successful_zero(scan):
    engine, _, ctx, _ = scan
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    ctx.daily_history = ctx.daily_history.with_columns(pl.lit(None).cast(pl.Float64).alias("raw_close"))
    entry = {**ENTRY, "params": {**ENTRY["params"], "market_min_valid": 2,
             "time": "14:45:00", "evaluation_delay_minutes": 1,
             "market_validity": "price_amount_prev_close_v2"}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = engine.run("s01", ctx)
    assert result.section_statuses["buy"] == "waiting"
    assert result.section_errors["buy"]
    assert result.total == 0


def test_independent_timed_native_or_keeps_earlier_confirmed_evidence(scan):
    engine, _, ctx, _ = scan
    ctx.observed_at = datetime(2026, 8, 25, 14, 44)
    early = {**ENTRY, "id": "early", "params": {**ENTRY["params"], "market_min_valid": 2,
             "time": "14:40:00", "evaluation_delay_minutes": .5,
             "market_validity": "price_amount_prev_close_v2"}}
    later = {**early, "id": ENTRY["id"], "params": {**early["params"], "time": "14:45:00"}}
    with custom_signals.private_signal_registry([early, later, EXIT]):
        result = engine.run("s01", ctx, overrides={"entry_signals": [
            "csgi_" + later["id"], "csgi_" + early["id"]]})
    assert result.section_statuses["buy"] == "success"
    assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": ["csgi_early"],
                                        "evaluated_at": datetime(2026, 8, 25, 14, 40, 30)}]
    assert result.rows[0]["last_datetime"] == datetime(2026, 8, 25, 14, 40)
    assert result.rows[0]["entry_evaluated_at"] == datetime(2026, 8, 25, 14, 40, 30)


def test_missing_minutes_service_recovers_s01_selection_and_cause(scan, monkeypatch):
    from app.services.screener import ScreenerService

    engine, _, ctx, saved = scan
    service = ScreenerService(SimpleNamespace(get_instruments_asset=lambda *_: pl.DataFrame()))
    service.repo.get_daily_close_inputs = lambda *_: (
        ctx.daily_history.tail(1), ctx.daily_history.tail(1).select("symbol", "close"))
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *_: ctx.current)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: ctx.daily_history)
    monkeypatch.setattr(service, "_load_minute_history",
                        lambda *_: (_ for _ in ()).throw(OSError("S01 minute partition unreadable")))
    built = service.build_strategy_context(engine, ctx.as_of, ["s01"], timeframe="1m")
    result = engine.run("s01", built)
    assert result.sections["select"] == saved["rows"]
    assert result.section_statuses["buy"] == "failed"
    assert "S01 minute partition unreadable" in result.section_errors["buy"]


def test_candidate_uses_saved_source_with_only_explicit_section_opt_in():
    import ast

    root = Path(__file__).resolve().parents[2]
    saved = ast.parse((Path(__file__).parent / "fixtures" / "s01_available_minutes.py").read_text())
    candidate = ast.parse((root / "examples/strategies/s01_signal_sections.py").read_text())
    assignment = candidate.body.pop()
    assert ast.unparse(assignment) == "META['result_sections'] = 'signals'"
    assert ast.dump(saved) == ast.dump(candidate)
    engine = StrategyEngine([root / "examples/strategies"])
    assert signal_strategy(engine.get("custom_mvxxcs01"))


def test_saved_candidate_freezes_previous_session_and_confirms_available_minutes(monkeypatch):
    from app.services.daily_selections import compute_selections
    from app.services.screener import ScreenerService

    root = Path(__file__).resolve().parents[2]
    engine = StrategyEngine([root / "examples/strategies"])
    sid = "custom_mvxxcs01"
    ctx = context()
    ctx.daily_history = daily().with_columns(
        pl.Series("change_pct", [.01, 20 / 19 - 1]), pl.Series("amplitude", [.02, .02]),
        pl.Series("close", [19., 20.]), pl.Series("raw_close", [19., 20.]),
        pl.Series("high", [19.1, 20.1]), pl.Series("raw_high", [19.1, 20.1]),
        pl.Series("low", [18.9, 19.9]), pl.Series("raw_low", [18.9, 19.9]))
    ctx.current = ctx.daily_history.tail(1)
    service = ScreenerService(SimpleNamespace(
        get_instruments_asset=lambda *_: pl.DataFrame({
            "symbol": ["600001.SH"], "name": ["正常股票"], "listing_date": ["2010-01-01"]}),
        get_daily_close_inputs=lambda *_: (ctx.current, ctx.current.select("symbol", "close"))))
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: ctx.daily_history)
    frozen = compute_selections(engine, service, ctx.current["date"][0], ctx.as_of, [sid], {},
                                now=datetime(2026, 8, 24, 16))
    assert frozen["errors"] == {}
    assert [row["symbol"] for row in frozen["selections"][sid]["rows"]] == ["600001.SH"]
    assert frozen["selections"][sid]["scheduled_for"].endswith("20:00:00+08:00")
    engine._selection_loader = lambda *_: frozen["selections"][sid]
    # A missing minute and a repeated timestamp cannot alter cumulative evidence.
    ctx.history = bars(price=20.5).filter(pl.col("datetime") != datetime(2026, 8, 25, 10))
    ctx.history = pl.concat([ctx.history, ctx.history.tail(1)])
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    entry = {**ENTRY, "params": {**ENTRY["params"], "time": "14:45:00",
             "evaluation_delay_minutes": 1, "market_validity": "price_amount_prev_close_v2"}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = engine.run(sid, ctx, overrides={"scoring_replace": True, "scoring": {}})
    assert result.total == 1
    row = result.rows[0]
    assert row["count"] == 224
    assert row["amount"] == pytest.approx(224 * 20.5 * 100000)
    assert row["score"] == row["intraday_range_amount_score"]
    assert result.entry_data_quality["csgi_" + ENTRY["id"]]["market_valid_count"] == 1
