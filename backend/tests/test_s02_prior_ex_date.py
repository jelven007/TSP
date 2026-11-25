"""At source-day 21:00, S02 excludes that day's ex-date from next-session qualification."""
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.data_providers.corporate_actions import CorporateAction
from app.services.corporate_actions import save_actions
from app.services.daily_selections import DailySelections, compute_selections, due_window
from app.services.screener import ScreenerService
from app.services.strategy_results import ResultIdentity
from app.strategy.basic_filter import previous_session
from app.strategy.engine import StrategyEngine

SID = "custom_mvxxcp88"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples/strategies"
SYMBOLS = ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]
CN = ZoneInfo("Asia/Shanghai")


def selection_case(tmp_path, monkeypatch, target, *, all_flagged=False, no_events=False):
    source = previous_session(target)
    days, day = [], target
    for _ in range(21):
        day = previous_session(day)
        days.append(day)
    history = pl.DataFrame([
        {"symbol": symbol, "date": day, "high": 10. + spread, "low": 10. - spread,
         "close": 10., "raw_close": 10., "change_pct": .02,
         "turnover_rate": 2., "float_shares": 1e9, "amount": 2e7}
        for symbol, spread in zip(SYMBOLS, [2., .5, 1., .75], strict=True) for day in days
    ])
    event_days = [source] * 4 if all_flagged else [
        source, target, previous_session(source), None,
    ]
    save_actions(tmp_path, [] if no_events else [
        CorporateAction(id=symbol, symbol=symbol, source="fixture", ex_date=day,
                        cash_per_share=.1, dates_verified=False)
        for symbol, day in zip(SYMBOLS, event_days, strict=True) if day is not None
    ])
    instruments = pl.DataFrame({"symbol": SYMBOLS, "name": ["测试"] * 4})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments_asset=lambda *_: instruments)
    service = ScreenerService(repo)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: history)
    monkeypatch.setattr(service, "_load_minute_history", lambda *_: pytest.fail("target minutes read"))
    monkeypatch.setattr("app.services.daily_close_validation.validate_daily_close", lambda *_a, **_k: {})
    engine = StrategyEngine([EXAMPLES])
    overrides = json.loads((EXAMPLES / "s02_atr14.overrides.json").read_text())
    now = datetime.combine(source, datetime.min.time().replace(hour=21), tzinfo=CN)
    assert due_window(now, monitored_only=True, strategy_id=SID) == (source, target)
    return engine, service, overrides, source, now


@pytest.mark.parametrize("target", [date(2026, 11, 2), date(2026, 11, 25)],
                         ids=["weekend_to_monday", "adjacent_sessions"])
@pytest.mark.parametrize("all_flagged", [False, True], ids=["source_only", "successful_zero"])
def test_2100_excludes_source_ex_date_before_atr_and_freezes_without_shrinking_pool(
    tmp_path, monkeypatch, target, all_flagged,
):
    engine, service, overrides, source, now = selection_case(
        tmp_path, monkeypatch, target, all_flagged=all_flagged)
    context = service.build_selection_context(engine, source, target, [SID],
                                              overrides_map={SID: overrides})
    assert set(context.current["symbol"]) == set(SYMBOLS)
    assert context.daily_history["date"].max() == source
    computed = compute_selections(engine, service, source, target, [SID], {SID: overrides}, now=now)
    assert computed["errors"] == {}
    snapshot = computed["selections"][SID]
    expected = [] if all_flagged else ["600003.SH", "600004.SH", "600002.SH"]
    assert [row["symbol"] for row in snapshot["rows"]] == expected
    assert snapshot["coverage"]["scope_total"] == snapshot["coverage"]["available_daily"] == 4
    assert snapshot["scheduled_for"] == now.isoformat()
    for rank, row in enumerate(snapshot["rows"], 1):
        assert row["reference_prior_ex_date"] is False
        assert row["reference_date"] == str(row["atr_reference_date"]) == str(source)
        assert row["atr_rank"] == rank
        assert row["atr_rank_population"] == 3
        assert row["score"] == 100. * (3 - rank) / 2
    identity = ResultIdentity(SID, "s02", engine.get(SID).logic_version,
                              "{}", "{}", "null", "stock", "1m")
    archive = tmp_path / "archive"
    DailySelections(archive).save(identity, snapshot, started_at=1)
    restored = DailySelections(archive).read(SID, target)
    assert restored["rows"] == json.loads(json.dumps(snapshot["rows"], default=str))
    assert restored["source_date"] == str(source)
    assert restored["coverage"]["scope_total"] == 4


def test_no_recorded_ex_date_keeps_existing_qualification(tmp_path, monkeypatch):
    target = date(2026, 11, 25)
    engine, service, overrides, source, now = selection_case(
        tmp_path, monkeypatch, target, no_events=True)
    computed = compute_selections(engine, service, source, target, [SID], {SID: overrides}, now=now)
    assert computed["errors"] == {}
    snapshot = computed["selections"][SID]
    assert {row["symbol"] for row in snapshot["rows"]} == set(SYMBOLS)
    assert snapshot["coverage"]["scope_total"] == 4
