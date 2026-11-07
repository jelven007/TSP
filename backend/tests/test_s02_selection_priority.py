"""Selection enrichment must run on the real source-owned eligibility path."""
# ruff: noqa: F811 -- fixture reuse.

import polars as pl
import pytest
from test_minute_signal_sections import scan  # noqa: F401

from app.strategy.signal_sections import select_rows


def test_source_selection_enrichment_preserves_score_and_all_members(scan):
    engine, context, overrides = scan
    calls = []

    def enrich(rows, params, *, daily, as_of):
        calls.append((daily, as_of))
        return rows.with_columns(
            pl.lit(.12).alias("atr14_previous_close"),
            pl.int_range(1, pl.len() + 1).alias("atr_rank"),
            pl.lit(50.).alias("atr_rank_score"),
        )

    strategy = engine.get("custom_test")
    strategy.enrich_selection_fn = enrich
    rows = select_rows(engine, strategy, context, overrides=overrides)
    assert len(rows) == 3
    assert all(row["atr14_previous_close"] == .12 for row in rows)
    assert calls[0][1] == context.as_of
    assert "score" in rows[0]


def test_source_enrichment_cannot_change_selection_members(scan):
    engine, context, overrides = scan
    strategy = engine.get("custom_test")
    strategy.enrich_selection_fn = lambda rows, params, **kw: rows.head(1)
    with pytest.raises(ValueError, match="预选"):
        select_rows(engine, strategy, context, overrides=overrides)


def test_parallel_strategy_runs_do_not_share_pending_diagnostics(scan, monkeypatch):
    engine, context, overrides = scan
    def inject(frame, *, context, **kwargs):
        context.intraday_errors.append("等待完整池")
        return frame
    monkeypatch.setattr(engine, "_inject_intraday_signal_columns", inject)
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.section_errors["buy"] == "等待完整池"
    assert context.intraday_errors == []


def test_s02_preselection_uses_exact_prior_atr_and_separate_rank_score(monkeypatch):
    from test_s02_atr14_strategy import DAY, SOURCE, STRATEGY_ID, frames, prior

    from app.strategy import market_data
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    monkeypatch.setattr(market_data, "previous_session", prior)
    engine = StrategyEngine([SOURCE.parent])
    strategy = engine.get(STRATEGY_ID)
    _, daily = frames()
    current = daily.filter(pl.col("date") == prior(DAY)).with_columns(
        pl.lit(DAY).alias("date"), pl.lit(.02).alias("change_pct"),
        pl.lit(1e9).alias("float_shares"),
    )
    context = StrategyDataContext("stock", "1m", DAY, current=current, daily_history=daily)
    rows = select_rows(engine, strategy, context, overrides={"basic_filter": {
        "change_pct_min": None, "change_pct_max": None,
        "float_cap_min": None, "float_cap_max": None,
    }})
    ordered = sorted(rows, key=lambda row: row["atr_rank"])
    assert [row["atr14_previous_close"] for row in ordered] == pytest.approx([.2, .1])
    assert [row["atr_rank_score"] for row in ordered] == [100., 0.]
    assert all(row["atr_reference_date"] == prior(DAY) for row in ordered)


def test_actual_s02_scheduled_selection_persists_atr_through_restart(tmp_path, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from test_s02_atr14_strategy import DAY, SOURCE, STRATEGY_ID, frames, prior

    from app.services.daily_selections import DailySelections, generate_scheduled_local
    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyEngine

    (tmp_path / "s02.py").write_bytes(SOURCE.read_bytes())
    engine = StrategyEngine([tmp_path])
    _, daily = frames()
    daily = daily.with_columns(pl.lit(.02).alias("change_pct"),
                              pl.lit(.02).alias("turnover_rate"),
                              pl.lit(1e9).alias("float_shares"))
    source = prior(DAY)
    raw = daily.filter(pl.col("date") == source).select(
        "symbol", "date", pl.col("raw_close").alias("close"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_instruments_asset=lambda *_: pl.DataFrame(),
        get_daily_close_inputs=lambda day: (raw, raw.select("symbol", "close")))
    service = ScreenerService(repo)
    service._load_enriched_history = lambda *_: daily
    (tmp_path / "settlement_ready.json").write_text('{"day":"2026-10-30"}')
    now = datetime(2026, 10, 30, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = generate_scheduled_local(engine, service, tmp_path, now=now)
    assert result == {"saved": [STRATEGY_ID], "errors": {}}
    rows = DailySelections(tmp_path).read(STRATEGY_ID, DAY)["rows"]
    assert len(rows) == 2
    assert sorted(row["atr14_previous_close"] for row in rows) == pytest.approx([.1, .2])
    assert sorted(row["atr_rank"] for row in rows) == [1, 2]
    assert sorted(row["atr_rank_score"] for row in rows) == [0., 100.]
    assert generate_scheduled_local(engine, service, tmp_path, now=now)["saved"] == []
