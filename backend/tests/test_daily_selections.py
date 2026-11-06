"""Post-close selection is a durable input to next-session entries."""
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.cell.context import AccessError
from app.services.strategy_results import ResultIdentity, StrategyResults

CN = ZoneInfo("Asia/Shanghai")
SOURCE = date(2026, 10, 30)
TARGET = date(2026, 11, 2)


def key():
    return ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")


def snapshot(rows=()):
    return {"as_of": str(TARGET), "source_date": str(SOURCE), "rows": list(rows),
            "scheduled_for": "2026-10-30T16:00:00+08:00",
            "generated_at": "2026-10-30T16:01:00+08:00"}


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 30, 15, 59, 59, tzinfo=CN), None),
    (datetime(2026, 10, 30, 16, tzinfo=CN), (SOURCE, TARGET)),
    (datetime(2026, 10, 31, 12, tzinfo=CN), (SOURCE, TARGET)),
    (datetime(2026, 11, 2, 8, tzinfo=CN), (SOURCE, TARGET)),
    (datetime(2026, 11, 2, 9, 30, tzinfo=CN), None),
])
def test_due_window_uses_confirmed_trading_calendar_and_recovers_before_open(now, expected):
    from app.services.daily_selections import due_window
    assert due_window(now) == expected


def test_archive_is_idempotent_preserves_identity_and_successful_empty(tmp_path):
    from app.services.daily_selections import DailySelections
    store = DailySelections(tmp_path)
    saved = store.save(key(), snapshot(), started_at=1)
    again = DailySelections(tmp_path).save(replace(key(), strategy_version="changed"),
                                         snapshot([{"symbol": "LATE"}]), started_at=2)
    assert again == saved
    loaded = DailySelections(tmp_path).read("custom_test", TARGET)
    assert loaded["rows"] == []
    assert loaded["source_date"] == str(SOURCE)
    assert loaded["identity"] == key().fingerprint
    assert StrategyResults(tmp_path).read(key(), str(TARGET))["result"]["selection_only"]
    assert StrategyResults(tmp_path).summaries([key()], str(TARGET))["custom_test"]["as_of"] == str(TARGET)


def test_selection_archive_is_removed_and_old_writers_fenced_on_strategy_deletion(tmp_path):
    from app.services.daily_selections import DailySelections
    store = DailySelections(tmp_path)
    store.save(key(), snapshot(), started_at=1)
    with StrategyResults(tmp_path).deleting_strategy("custom_test"):
        pass
    assert store.read("custom_test", TARGET) is None
    with pytest.raises(AccessError):
        store.save(key(), snapshot(), started_at=2)


def test_intraday_archive_cannot_replace_frozen_selection_or_count_preselection_as_buy(tmp_path):
    from app.services.daily_selections import DailySelections
    pre = DailySelections(tmp_path)
    pre.save(key(), snapshot([{"symbol": "A", "reference_date": str(SOURCE)}]), started_at=1)
    store = StrategyResults(tmp_path)
    intraday = {"as_of": str(TARGET), "section_mode": "signals", "rows": [], "total": 0,
                "sections": {"sell": [], "select": [{"symbol": "WRONG"}], "buy": []}}
    saved = store.save(key(), intraday, started_at=2)
    assert saved["result"]["sections"]["select"] == [{"symbol": "A", "reference_date": str(SOURCE)}]
    assert pre.read("custom_test", TARGET)["rows"] == [{"symbol": "A", "reference_date": str(SOURCE)}]
    next_day = {**intraday, "as_of": "2026-11-03"}
    # A preselection-only record must not be mistaken for a successful empty buy archive.
    pre.save(key(), {**snapshot(), "as_of": "2026-11-03", "source_date": str(TARGET)}, started_at=3)
    missing = store.save(key(), next_day, started_at=4)
    assert missing["result"]["sections"]["sell"] == []
    assert not missing["result"]["section_errors"].get("sell")


@pytest.mark.parametrize("rows", [[], [{"symbol": "A", "reference_date": str(SOURCE)}]])
def test_read_merges_later_selection_without_rewriting_earlier_minute_archive(tmp_path, rows):
    from app.services.daily_selections import DailySelections
    store = StrategyResults(tmp_path)
    old = store.save(key(), {
        "as_of": str(TARGET), "section_mode": "signals", "rows": [], "total": 0,
        "sections": {"sell": [], "select": [], "buy": []},
        "section_errors": {"select": "缺少已保存选名单", "buy": "缺少已保存选名单"},
    }, started_at=1)
    DailySelections(tmp_path).save(key(), snapshot(rows), started_at=2)
    current = store.read(key(), str(TARGET))
    assert current["result"]["sections"]["select"] == rows
    assert current["result"]["selection_snapshot"]["source_date"] == str(SOURCE)
    assert "select" not in current["result"]["section_errors"]
    assert current["result"]["section_errors"]["buy"] == "缺少已保存选名单"
    assert current["record_id"] == old["record_id"]
    assert store.detail("custom_test", old["record_id"]) == old


def test_postclose_context_never_reads_target_minutes_or_future_daily(scan, monkeypatch):
    from app.services.screener import ScreenerService
    engine, context, overrides = scan
    daily = context.daily_history
    service = ScreenerService(SimpleNamespace(
        get_instruments_asset=lambda *_: pl.DataFrame(),
        get_daily_close_inputs=lambda day: (
            daily.filter(pl.col("date") == day), daily.select("symbol", "close"),
        ),
    ))
    future = context.daily_history.with_columns(pl.lit(TARGET).alias("date"))
    history = pl.concat([context.daily_history, future])
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: history)
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *_: context.daily_history)
    monkeypatch.setattr(service, "_load_minute_history", lambda *_: pytest.fail("target minutes loaded"))
    built = service.build_selection_context(engine, SOURCE, TARGET, ["custom_test"],
                                             overrides_map={"custom_test": overrides})
    assert built.as_of == TARGET
    assert built.daily_history["date"].max() == SOURCE
    assert built.current["date"].unique().to_list() == [SOURCE]
    assert built.history is None


def test_local_schedule_waits_for_completed_source_then_saves_and_survives_restart(scan, tmp_path):
    from app.services.daily_selections import DailySelections, generate_scheduled_local
    from app.services.screener import ScreenerService
    engine, context, _ = scan
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments_asset=lambda *_: pl.DataFrame(),
                           get_daily_close_inputs=lambda day: (
                               context.daily_history.filter(pl.col("date") == day),
                               context.daily_history.select("symbol", "close"),
                           ))
    service = ScreenerService(repo)
    service._load_enriched_history = lambda *_: context.daily_history
    now = datetime(2026, 10, 30, 16, tzinfo=CN)
    waiting = generate_scheduled_local(engine, service, tmp_path, now=now)
    assert waiting["errors"]
    assert DailySelections(tmp_path).read("custom_test", TARGET) is None
    assert DailySelections(tmp_path).job(str(TARGET))["state"] == "waiting_data"
    (tmp_path / "settlement_ready.json").write_text('{"day":"2026-10-30"}')
    done = generate_scheduled_local(engine, service, tmp_path, now=now)
    assert done == {"saved": ["custom_test"], "errors": {}}
    assert DailySelections(tmp_path).read("custom_test", TARGET)["rows"][0]["symbol"] == "600001.SH"
    assert generate_scheduled_local(engine, service, tmp_path, now=now)["saved"] == []
    assert DailySelections(tmp_path).job(str(TARGET))["attempts"] == 2


@pytest.mark.parametrize("change", ["source", "deletion", "compute_failure"])
def test_local_schedule_discards_changed_source_and_records_failure(scan, tmp_path, monkeypatch, change):
    from app.services import daily_selections
    from app.services.screener import ScreenerService
    engine, _context, _ = scan
    service = ScreenerService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    (tmp_path / "settlement_ready.json").write_text('{"day":"2026-10-30"}')
    def compute(*_args, **_kwargs):
        if change == "source":
            engine._strategies["custom_test"] = replace(engine.get("custom_test"), logic_version="changed")
        elif change == "deletion":
            with StrategyResults(tmp_path).deleting_strategy("custom_test"):
                engine._strategies.pop("custom_test")
        else:
            raise RuntimeError("compute unavailable")
        return {"selections": {"custom_test": snapshot()}, "errors": {}}
    monkeypatch.setattr(daily_selections, "compute_selections", compute)
    result = daily_selections.generate_scheduled_local(
        engine, service, tmp_path, now=datetime(2026, 10, 30, 16, tzinfo=CN))
    assert result["errors"]
    assert daily_selections.DailySelections(tmp_path).read("custom_test", TARGET) is None
    assert daily_selections.DailySelections(tmp_path).job(str(TARGET))["state"] == "failed"


def test_preselection_excludes_source_dated_known_suspension(scan, monkeypatch):
    from app.services.daily_selections import compute_selections
    from app.services.screener import ScreenerService
    engine, context, _overrides = scan
    daily = context.daily_history.with_columns(pl.lit(True).alias("is_suspended"))
    service = ScreenerService(SimpleNamespace(
        get_instruments_asset=lambda *_: pl.DataFrame(),
        get_daily_close_inputs=lambda day: (
            daily.filter(pl.col("date") == day), daily.select("symbol", "close"),
        ),
    ))
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: daily)
    result = compute_selections(engine, service, SOURCE, TARGET, ["custom_test"],
                                {"custom_test": {"basic_filter": {"exclude_suspended": True}}})
    assert result["errors"] == {}
    assert result["selections"]["custom_test"]["rows"] == []


def test_compute_dispatch_generates_archives_then_buys_only_saved_members(tmp_path, monkeypatch):
    from pathlib import Path

    from test_minute_signal_sections import CODE

    from app.multitenancy.compute_worker import execute
    from app.services.daily_selections import DailySelections
    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyDataContext
    daily = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "date": [SOURCE, SOURCE],
                          "close": [10., 10.], "change_pct": [.02, .07]})
    current = daily.with_columns(pl.lit(TARGET).alias("date"), pl.lit(.5).alias("change_pct"))
    minute = pl.DataFrame({"symbol": daily["symbol"],
                           "datetime": [datetime(2026, 11, 2, 14, 30)] * 2,
                           "close": [10.2, 10.3], "csgi_trigger": [True, True]})
    repo = SimpleNamespace(
        get_instruments_asset=lambda *_: pl.DataFrame(),
        get_enriched_history=lambda *_: daily,
        get_daily_close_inputs=lambda day: (
            daily.filter(pl.col("date") == day), daily.select("symbol", "close"),
        ),
    )
    monkeypatch.setattr("app.tickflow.repository.DataStore", lambda *_args, **_kwargs:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr("app.tickflow.repository.KlineRepository", lambda *_: repo)
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", lambda *_: current)
    monkeypatch.setattr(ScreenerService, "_load_minute_history", lambda *_: minute)
    monkeypatch.setattr(ScreenerService, "build_daily_pool_context", lambda *_args, **_kwargs:
                        StrategyDataContext("stock", "1m", TARGET, pool_history=daily))
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path:
                        iter(()) if str(path) == "/market" else original(path))
    roots = [tmp_path / name for name in ("select", "buy", "missing")]
    for root in roots:
        root.mkdir()
    payload = {"sources": [{"id": "custom_test", "source": "custom", "code": CODE}],
               "as_of": str(TARGET), "strategy_ids": ["custom_test"], "timeframe": "1m"}
    generated = execute({**payload, "kind": "daily_selections", "source_date": str(SOURCE)}, roots[0])
    assert generated["errors"] == {}
    assert [row["symbol"] for row in generated["selections"]["custom_test"]["rows"]] == ["600001.SH"]
    archive = DailySelections(tmp_path)
    archive.save(key(), generated["selections"]["custom_test"], started_at=1)
    saved = archive.read("custom_test", TARGET)
    bought = execute({**payload, "kind": "strategy",
                      "selection_snapshots": {"custom_test": saved}}, roots[1])["results"]["custom_test"]
    assert [row["symbol"] for row in bought["sections"]["buy"]] == ["600001.SH"]
    assert bought["sections"]["select"] == saved["rows"]
    assert bought["sections"]["buy"][0]["close"] == 10.2
    missing = execute({**payload, "kind": "strategy"}, roots[2])["results"]["custom_test"]
    assert missing["sections"]["buy"] == []
    assert "已保存的盘后选名单" in missing["section_errors"]["buy"]


@pytest.mark.parametrize("role", ["standalone", "platform"])
def test_completed_pipeline_marks_source_ready_in_both_supported_modes(tmp_path, monkeypatch, role):
    from app.jobs import daily_pipeline
    monkeypatch.setattr(daily_pipeline.settings, "service_role", role)
    monkeypatch.setattr(daily_pipeline, "cn_now", lambda: datetime(2026, 10, 30, 16, tzinfo=CN))
    daily_pipeline.mark_settlement_ready(tmp_path, SOURCE, "release")
    import json
    assert json.loads((tmp_path / "settlement_ready.json").read_text()) == {
        "day": str(SOURCE), "release_id": "release"}
    (tmp_path / "settlement_ready.json").unlink()
    daily_pipeline.mark_settlement_ready(tmp_path, date(2026, 10, 29), "old")
    assert not (tmp_path / "settlement_ready.json").exists()


@pytest.fixture
def scan(tmp_path, monkeypatch):
    from test_minute_signal_sections import CODE

    from app.strategy.engine import StrategyDataContext, StrategyEngine
    (tmp_path / "custom.py").write_text(CODE)
    engine = StrategyEngine([tmp_path])
    daily = pl.DataFrame({"symbol": ["600001.SH"], "date": [SOURCE],
                          "close": [10.], "change_pct": [.02]})
    return engine, StrategyDataContext("stock", "1m", TARGET, daily_history=daily), {}
