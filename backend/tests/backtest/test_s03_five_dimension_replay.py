"""S03 research routes S02 candidates through the same five-dimensional kernel."""
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_s03_daily_selection import SOURCE, TARGET, scoring_inputs

from app.market_time import CN_TZ
from app.strategy.engine import StrategyDataContext


def test_replay_preserves_scores_dates_and_research_dependency(tmp_path, monkeypatch):
    from app.services import s03_selection

    snapshot, daily, financial, flows, _market, notices = scoring_inputs()
    (tmp_path / ".public_release.json").write_text(json.dumps({
        "id": "pinned", "published_at": datetime(2026, 11, 22, tzinfo=CN_TZ).timestamp()}))
    strategy = SimpleNamespace(meta={}, enrich_selection_fn=None)
    engine = SimpleNamespace(get=lambda sid: strategy, _override_loader=lambda sid: {})
    context = StrategyDataContext("stock", "1m", TARGET, current=daily.tail(1),
                                  daily_history=daily)
    def select(engine, strategy, ctx, **kwargs):
        assert ctx.as_of == TARGET
        assert ctx.daily_history["date"].max() == SOURCE
        assert kwargs["scope_only"] is True
        return snapshot["rows"]
    monkeypatch.setattr("app.strategy.signal_sections.select_rows", select)
    monkeypatch.setattr(s03_selection, "_extension", lambda root, fields, *args, **kwargs:
                        (flows if "large_net_inflow" in fields else notices,
                         {"config_id": "frozen", "partitions": []}))
    monkeypatch.setattr("app.services.financial_sync.read_financials_as_of",
                        lambda root, key, cutoff: financial[key])
    monkeypatch.setattr("app.services.financial_sync.latest_financial_revision_meta",
                        lambda *args: {"observed_at": "2026-11-01T12:00:00+08:00"})
    result = s03_selection.replay_selection(engine, context, tmp_path, SOURCE, {}, strict=False)
    assert result["scoring_policy"] == s03_selection.POLICY
    assert result["as_of"] == str(TARGET) and result["source_date"] == str(SOURCE)
    assert result["rows"][0]["selection_rank"] == 1
    assert result["rows"][0]["fund_score"] == pytest.approx(73.7777777778)
    assert result["source_selection"]["research_only"] is True
    assert "scheduled_for" not in result and "generated_at" not in result
    assert result["scoring_provenance"]["public_release_id"] == "pinned"
    assert result["scoring_provenance"]["observation_policy"] == "retained_research_snapshot"
    strict = s03_selection.replay_selection(engine, context, tmp_path, SOURCE, {}, strict=True)
    assert strict["scoring_provenance"]["observed_cutoff"] == "2026-11-20T21:00:00+08:00"
    assert strict["scoring_provenance"]["observation_policy"] == "source_day_observed"
    # T-day/future rows never reach the common scoring kernel.
    contaminated = replace(context, daily_history=daily.vstack(
        daily.tail(1).with_columns(pl.lit(TARGET).alias("date"))))
    assert s03_selection.replay_selection(
        engine, contaminated, tmp_path, SOURCE, {}, strict=False)["rows"] == result["rows"]


def test_replay_requires_frozen_inputs_and_exact_previous_session(tmp_path):
    from app.services.s03_selection import replay_selection

    context = StrategyDataContext("stock", "1m", TARGET)
    with pytest.raises(ValueError, match="前交易日"):
        replay_selection(None, context, tmp_path, TARGET, {})
    with pytest.raises(ValueError, match="冻结"):
        replay_selection(None, context, tmp_path, SOURCE, {})


def test_minute_replay_routes_five_dimensions_into_actual_ab_entry(tmp_path, monkeypatch):
    from test_entry_groups import signal
    from test_minute_backtest import _make_service, _minute_frame

    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.services import s03_selection
    from app.strategy import custom_signals

    _snapshot, daily, financial, flows, _market, notices = scoring_inputs()
    day = SOURCE
    daily = daily.with_columns((pl.col("date") - timedelta(days=1)).alias("date"))
    flows = flows.with_columns((pl.col("date") - timedelta(days=1)).alias("date"))
    panel = daily.with_columns(
        pl.lit(1e7).alias("volume"), pl.lit(4e8).alias("float_shares"),
        pl.lit(365).alias("listing_age_days"))
    panel = panel.vstack(panel.tail(1).with_columns(pl.lit(day).alias("date")))
    frames = {day: _minute_frame(day, [
        ("600001.SH", "09:31", 10.64), ("600001.SH", "10:00", 10.7),
        ("600001.SH", "10:01", 10.71)])}
    service = _make_service(tmp_path, panel, frames)
    service.engine.repo.store = SimpleNamespace(data_dir=tmp_path)
    for sid in ("custom_mvxxcs03", "custom_mvxxcp88"):
        (tmp_path / "strategies" / f"{sid}.py").write_text(f'''
import polars as pl
META = {{"id": "{sid}", "name": "{sid}", "asset_types": ["stock"],
        "timeframes": ["1m"], "result_sections": "signals", "daily_history_bars": 65}}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {{"enabled": False, "target_mode": "all"}}
ENTRY_SIGNALS = ["csgi_late_session_strength_s03"]
def filter_minute_history(df, params, *, daily=None):
    return df.filter(pl.col("csgi_late_session_strength_s03")).rename({{"datetime": "last_datetime"}})
''')
    service.strategy_engine.reload()
    service.strategy_engine._user_data_dir = lambda: tmp_path
    sig = signal()
    sig["params"]["entry_groups"] = {
        "a": {"enabled": False}, "b": {"enabled": True, "score_min": 1}}
    custom_signals.save_one(tmp_path, sig)
    (tmp_path / ".public_release.json").write_text(json.dumps({
        "id": "pinned", "published_at": datetime(2026, 11, 22, tzinfo=CN_TZ).timestamp()}))
    monkeypatch.setattr(s03_selection, "_extension", lambda root, fields, *args, **kwargs:
                        (flows if "large_net_inflow" in fields else notices,
                         {"config_id": "frozen", "partitions": []}))
    monkeypatch.setattr("app.services.financial_sync.read_financials_as_of",
                        lambda root, key, cutoff: financial[key])
    monkeypatch.setattr("app.services.financial_sync.latest_financial_revision_meta",
                        lambda *args: {"observed_at": "2026-11-01T12:00:00+08:00"})
    result = MinuteSignalReplayer(service.engine, service.strategy_engine).replay(
        service.strategy_engine.get("custom_mvxxcs03"), panel=panel, start=day, end=day,
        params={}, overrides={})
    assert not result.errors
    assert [hit.symbol for hit in result.hits] == ["600001.SH"]
    assert result.hits[0].signal_time == "10:00:30"
    assert result.hits[0].trigger_time == "10:01"
    assert result.selection_provenance[0]["coverage"]["candidate_total"] == 1


def test_cash_ledger_plan_includes_s02_derived_return_even_when_s03_has_no_numeric_filter(
    tmp_path, monkeypatch,
):
    from test_entry_groups import signal
    from test_morning_backtest import EXIT, configuration, service_fixture

    from app.strategy import custom_signals

    service, _, _ = service_fixture(tmp_path)
    engine = service.strategy_engine
    original = engine.get("test_minute_ping")
    engine._strategies["custom_mvxxcs03"] = replace(
        original, meta={**original.meta, "id": "custom_mvxxcs03"},
        basic_filter={"enabled": False})
    engine._strategies["custom_mvxxcp88"] = replace(
        original, meta={**original.meta, "id": "custom_mvxxcp88"},
        basic_filter={"enabled": True, "change_pct_min": .5, "float_cap_min": 1})
    custom_signals.save_one(tmp_path, signal())
    plans = []
    def load(symbols, start, end, plan, **kwargs):
        plans.append(plan)
        return pl.DataFrame()
    monkeypatch.setattr(service.engine, "load_panel_for_backtest", load)
    result = service.run(configuration(
        strategy_id="custom_mvxxcs03", initial_capital=1_000_000, overrides={
            "entry_signals": ["csgi_late_session_strength_s03"], "exit_signals": [EXIT],
            "max_hold_days": None, "stop_loss": None, "basic_filter": {"enabled": False}}))
    assert result.error and len(plans) == 1
    assert "change_pct" in plans[0].indicator_columns
    assert "change_pct" not in plans[0].base_columns
    assert {"raw_close", "float_shares"} <= plans[0].base_columns
    assert plans[0].warmup_bars >= 65
