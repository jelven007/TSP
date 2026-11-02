"""Custom minute results separate selection membership from entry triggers."""
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.screener import ScreenerService
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.monitor import MonitorRuleEngine

CODE = '''
import polars as pl
META = {"id": "custom_test", "name": "s02", "timeframes": ["1m"],
        "asset_types": ["stock"], "result_sections": "signals",
        "scoring": {"change_pct": 1}, "tie_break": "symbol", "limit": 1}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"boards": ["沪主板"], "data_reference": "previous_trade_day",
                "change_pct_min": .5, "change_pct_max": 6,
                "price_min": None, "price_max": None, "market_cap_min": None,
                "amount_min": None, "exclude_new_days": 0}
ENTRY_SIGNALS = ["csgi_trigger"]
def filter_minute_history(df, params):
    return (df.filter(pl.col("csgi_trigger"))
            .sort("symbol", "datetime").unique("symbol", keep="first")
            .rename({"datetime": "last_datetime"}))
'''


@pytest.fixture
def scan(tmp_path, monkeypatch):
    (tmp_path / "custom.py").write_text(CODE)
    engine = StrategyEngine([tmp_path])
    monkeypatch.setattr(engine, "_inject_intraday_signal_columns", lambda frame, **kw: frame)
    symbols = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH", "300001.SZ"]
    day = date(2026, 11, 2)
    prior = pl.DataFrame({
        "symbol": symbols, "date": [date(2026, 10, 30)] * 6,
        "close": [10., 10., 10., 8., 10., 10.],
        "change_pct": [.02, .02, .02, .02, .07, .02],
    })
    current = prior.with_columns(pl.lit(day).alias("date"), pl.lit("名称").alias("name"))
    bars = pl.DataFrame({
        "symbol": symbols * 2, "datetime": [datetime(2026, 11, 2, 14, 30)] * 6
        + [datetime(2026, 11, 2, 15)] * 6,
        "close": [10.2, 10.3, 10.4, 8.2, 10.2, 10.2] + [11.] * 6,
        "csgi_trigger": [True, True, False, True, True, True] + [False] * 6,
    })
    context = StrategyDataContext("stock", "1m", day, current=current, history=bars,
                                  daily_history=prior, completed_through=datetime(2026, 11, 2, 14, 30))
    overrides = {"overlay_filter": [{"left": "close", "op": ">=", "right": "9"}],
                 "display_limit": 1}
    return engine, context, overrides


def test_select_uses_all_conditions_but_not_trigger_or_display_cap(scan):
    engine, context, overrides = scan
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.section_mode == "signals"
    assert {row["symbol"] for row in result.sections["select"]} == {
        "600001.SH", "600002.SH", "600003.SH",
    }
    assert {row["symbol"] for row in result.sections["buy"]} == {"600001.SH", "600002.SH"}
    assert result.rows == result.sections["buy"]
    assert result.total == 2
    assert all(row["reference_date"] == "2026-10-30" for row in result.sections["select"])
    assert result.sections["buy"][0]["close"] == 10.3


def test_select_survives_missing_minute_data_and_keeps_missing_score_members(scan):
    engine, context, overrides = scan
    current = context.current.with_columns(pl.lit(None, dtype=pl.Float64).alias("change_pct"))
    result = engine.run("custom_test", replace(context, current=current,
                                             history=None), overrides=overrides)
    assert result.sections["buy"] == []
    assert len(result.sections["select"]) == 3
    assert all(row.get("score") is None for row in result.sections["select"])
    assert result.section_errors["buy"]


def test_missing_minute_partition_does_not_block_daily_selection_context(scan, monkeypatch):
    engine, context, overrides = scan
    service = ScreenerService(SimpleNamespace(get_instruments_asset=lambda *_: pl.DataFrame()))
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *_: context.current)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: context.daily_history)
    monkeypatch.setattr(service, "_load_minute_history",
                        lambda *_: (_ for _ in ()).throw(ValueError("缺少指定日分钟行情")))
    built = service.build_strategy_context(engine, context.as_of, ["custom_test"],
                                           timeframe="1m", overrides_map={"custom_test": overrides})
    result = engine.run("custom_test", built, overrides=overrides)
    assert len(result.sections["select"]) == 3
    assert result.sections["buy"] == []
    assert result.section_errors["buy"]


def test_generic_minute_strategy_retains_existing_result_contract(scan):
    engine, context, overrides = scan
    engine.get("custom_test").meta.pop("result_sections")
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.sections is None
    assert result.total == 1


def test_buy_retains_trigger_snapshot_instead_of_using_final_selection(scan):
    engine, context, overrides = scan
    current = context.current.with_columns(
        pl.when(pl.col("symbol") == "600001.SH").then(8.).otherwise(pl.col("close")).alias("close"),
    )
    overrides = {**overrides, "basic_filter": {"data_reference": "signal_day", "price_min": 9.,
                                              "change_pct_min": None, "change_pct_max": None}}
    result = engine.run("custom_test", replace(context, current=current), overrides=overrides)
    assert "600001.SH" not in {row["symbol"] for row in result.sections["select"]}
    buy = next(row for row in result.sections["buy"] if row["symbol"] == "600001.SH")
    assert buy["close"] == 10.2
    assert buy["last_datetime"] == datetime(2026, 11, 2, 14, 30)


def test_live_signal_result_keeps_minute_identity_and_sections(scan):
    engine, context, overrides = scan
    result = engine.run("custom_test", context, overrides=overrides)
    payload = MonitorRuleEngine._strategy_result_payload(result)
    assert payload["section_mode"] == "signals"
    assert len(payload["sections"]["select"]) == 3
    assert MonitorRuleEngine._live_result_identity(
        engine.get("custom_test"), overrides, "stock",
    )["timeframe"] == "1m"


def signal_result(day, rows=()):
    return {"as_of": day, "rows": list(rows), "total": len(rows), "section_mode": "signals",
            "sections": {"sell": [], "select": [], "buy": list(rows)}}


def test_sell_uses_exact_previous_session_latest_success_across_config_changes(tmp_path):
    store = StrategyResults(tmp_path)
    key = ResultIdentity("custom_test", "s02", "old", "{}", "{}", "null", "stock", "1m")
    store.save(key, signal_result("2026-10-29", [{"symbol": "STALE"}]), started_at=1)
    buy = {"symbol": "600001.SH", "name": "旧名称", "close": 10.2,
           "last_datetime": "2026-10-30T14:30:00"}
    prior = store.save(key, signal_result("2026-10-30", [buy]), started_at=10)
    store.save(replace(key, strategy_version="failed"),
               {**signal_result("2026-10-30"), "entry_filter_error": "missing history"}, started_at=50)
    store.save(replace(key, timeframe="1d"),
               {"as_of": "2026-10-30", "rows": [{"symbol": "WRONG_TIMEFRAME"}]}, started_at=100)
    saved = store.save(replace(key, strategy_version="current"),
                       signal_result("2026-11-02"), started_at=60)
    sell = saved["result"]["sections"]["sell"]
    assert sell == [{**buy, "entry_date": "2026-10-30", "entry_record_id": prior["record_id"]}]
    assert not saved["result"].get("section_errors", {}).get("sell")
    assert store.save(replace(key, strategy_version="current"),
                      signal_result("2026-11-02", [{"symbol": "OLD"}]), started_at=55) == saved


def test_sell_distinguishes_missing_archive_from_successful_empty_and_legacy_rows(tmp_path):
    store = StrategyResults(tmp_path)
    key = ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")
    missing = store.save(key, signal_result("2026-11-02"), started_at=10)
    assert "2026-10-30" in missing["result"]["section_errors"]["sell"]
    store.save(key, {"as_of": "2026-10-30", "rows": []}, started_at=20)
    empty = store.save(key, signal_result("2026-11-02"), started_at=30)
    assert empty["result"]["sections"]["sell"] == []
    assert not empty["result"].get("section_errors", {}).get("sell")
    store.save(key, {"as_of": "2026-10-30", "rows": [{"symbol": "LEGACY"}]}, started_at=40)
    legacy = store.save(key, signal_result("2026-11-02"), started_at=50)
    assert legacy["result"]["sections"]["sell"][0]["symbol"] == "LEGACY"
