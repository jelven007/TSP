"""S03 scores completed symbol records across gaps, independent of global windows."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.s03_selection import compute_selection, freeze_source
from app.strategy.engine import StrategyDataContext

SOURCE = date(2026, 4, 16)
TARGET = date(2026, 4, 17)
SYMBOL = "600499.SH"


@pytest.mark.parametrize("loaded,available", [(56, 65), (61, 65), (60, 60), (65, 65), (55, 55)])
def test_live_selection_recovers_actual65_before_scoring(loaded, available, monkeypatch, tmp_path):
    from app.services import financial_sync, s03_selection

    days = [SOURCE - timedelta(days=i) for i in reversed(range(available))]
    full = pl.DataFrame([{
        "symbol": SYMBOL, "date": day, "close": 10. + i / 100,
        "raw_close": 10. + i / 100, "high": 11., "low": 9.,
        "amount": 1000., "name": "测试公司",
    } for i, day in enumerate(days)])
    future = full.tail(1).with_columns(
        pl.lit(TARGET).alias("date"), pl.lit(999.).alias("close"))
    context = StrategyDataContext("stock", "1m", TARGET,
        current=full.tail(1), daily_history=full.tail(loaded))
    calls = []

    def daily_batch(symbols, end, bars, columns=None):
        calls.append((symbols, end, bars))
        return pl.concat([full, future])  # Boundary must exclude target-day data.

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_completed_daily_batch=daily_batch)
    service = SimpleNamespace(repo=repo, build_selection_context=lambda *a, **k: context)
    engine = SimpleNamespace(get=lambda sid: SimpleNamespace(
        meta={}, enrich_selection_fn=None))
    flows = full.select("symbol", "date").with_columns(
        pl.lit(20.).alias("super_large_net_inflow"),
        pl.lit(30.).alias("large_net_inflow"))
    monkeypatch.setattr(s03_selection, "_extension", lambda path, fields, *a, **k:
        (flows if "super_large_net_inflow" in fields else pl.DataFrame(), {}))
    monkeypatch.setattr(financial_sync, "latest_financial_revision_meta", lambda *a: {})
    financial = {
        "metrics": {"net_income_yoy": 20., "roe": 10.,
                    "debt_to_asset_ratio": 40., "eps_basic": .5},
        "income": {"net_income": 100.},
        "cash_flow": {"net_operating_cash_flow": 120.},
    }
    monkeypatch.setattr(financial_sync, "read_financials_as_of", lambda path, table, cutoff:
        pl.DataFrame([{"symbol": SYMBOL, "period_end": "2025-12-31",
                       "announce_date": "2026-03-28", **financial[table]}]))
    source = {"as_of": str(TARGET), "source_date": str(SOURCE),
        "identity": "saved-s02", "rows": [{"symbol": SYMBOL}],
        "scheduled_for": "2026-04-16T21:00:00+08:00",
        "generated_at": "2026-04-16T21:12:00+08:00"}
    def compute():
        return compute_selection(engine, service, SOURCE, TARGET, {},
            freeze_source(source, SOURCE, TARGET), datetime(2026, 4, 16, 22, tzinfo=CN_TZ))

    if available < 60:
        with pytest.raises(ValueError, match="全部无法评分"):
            compute()
        assert calls == [([SYMBOL], SOURCE, 65)]
        return
    result = compute()
    assert result["coverage"]["scorable_total"] == 1
    assert result["coverage"]["invalid_symbols"] == {}
    assert result["rows"][0]["scoring_inputs"]["ma60"] == pytest.approx(
        full.tail(60)["close"].mean())
    assert result["rows"][0]["scoring_inputs"]["momentum20"] == pytest.approx(
        full["close"][-1] / full["close"][-21] - 1)
    assert calls == ([([SYMBOL], SOURCE, 65)] if loaded < 65 else [])
    assert result["scoring_provenance"]["daily_record_policy"] == "completed_symbol_records_v1"
    assert result["scoring_provenance"]["daily_record_counts"][SYMBOL] == available


def test_repository_keeps_completed_records_across_long_gaps_and_duplicate_evidence(tmp_path):
    from app.tickflow.repository import DataStore, KlineRepository

    rows = [{"symbol": symbol, "date": SOURCE - timedelta(days=i * spacing),
             "close": float(i + 1)}
            for symbol, spacing in [(SYMBOL, 3), ("605298.SH", 1)]
            for i in range(80)]
    rows.extend([{"symbol": SYMBOL, "date": TARGET, "close": 999.},
                 {"symbol": SYMBOL, "date": SOURCE, "close": 888.}])
    folder = tmp_path / "kline_daily_enriched" / f"date={SOURCE}"
    folder.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(folder / "part.parquet")
    store = DataStore(tmp_path, read_only=True)
    try:
        repo = KlineRepository(store)
        result = repo.get_completed_daily_batch([SYMBOL], SOURCE, 65, ["symbol", "date", "close"])
        assert result.height == 65 and result["date"].max() == SOURCE
        assert result["date"].min() < SOURCE - timedelta(days=180)
        assert result["symbol"].unique().to_list() == [SYMBOL]
        assert result.filter(pl.col("date") == SOURCE).height == 2
        assert 999. not in result["close"]
        assert repo.get_completed_daily_batch(["missing"], SOURCE, 65).is_empty()
    finally:
        store.db.close()
