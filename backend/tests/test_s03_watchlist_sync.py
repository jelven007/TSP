"""Persisted file watchlists, archive guards and retry-only synchronization."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from test_minute_signal_sections import CODE

from app.market_time import CN_TZ
from app.services import daily_selections, s03_watchlist, watchlist
from app.services.daily_selections import DailySelections
from app.services.quote_service import QuoteSubscriber
from app.services.strategy_results import ResultIdentity

SID = "custom_mvxxcs03"
RUN = datetime(2026, 10, 30, 21, 5, tzinfo=CN_TZ)
TARGET = "2026-11-02"


def snapshot(rows=None):
    return {"source_date": "2026-10-30", "as_of": TARGET,
            "rows": [{"symbol": "600030.SH"}, {"symbol": "600001.SH"}] if rows is None else rows,
            "scoring_policy": "s03_five_dimensions_v1",
            "scheduled_for": RUN.replace(minute=0).isoformat(), "generated_at": RUN.isoformat()}


def save(tmp_path, value):
    identity = ResultIdentity(SID, "s03", "v1", "{}", "{}", "null", "stock", "1m")
    DailySelections(tmp_path).save(identity, value, started_at=1)
    return DailySelections(tmp_path).read(SID, TARGET)


@pytest.fixture
def groups(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist.settings, "data_dir", tmp_path)
    _, fixed = watchlist.create_group("s03", "orange")
    _, other = watchlist.create_group("保留组")
    watchlist.add("600010.SH", "独占", group_id=fixed["id"])
    watchlist.add_batch(["600020.SH"], "共享", group_ids=[fixed["id"], other["id"]])
    watchlist.add("600030.SH", "新共享", group_id=other["id"])
    watchlist.set_strategies("600030.SH", ["kept"])
    watchlist.add("600050.SH", "未分组")
    return fixed, other


@pytest.mark.parametrize("empty", [False, True])
def test_file_replacement_persists_order_and_preserves_unrelated_members(tmp_path, groups, empty):
    fixed, other = groups
    saved = save(tmp_path, snapshot([] if empty else None))
    receipt = s03_watchlist.sync_archived_selection(tmp_path, saved, RUN)
    rows = watchlist.list_symbols()
    assert [r["symbol"] for r in rows if fixed["id"] in r["group_ids"]] == (
        [] if empty else ["600030.SH", "600001.SH"])
    by_symbol = {r["symbol"]: r for r in rows}
    assert "600010.SH" not in by_symbol
    assert by_symbol["600020.SH"]["group_ids"] == [other["id"]]
    assert by_symbol["600030.SH"]["note"] == "新共享"
    assert by_symbol["600030.SH"]["strategy_ids"] == ["kept"]
    assert by_symbol["600050.SH"]["group_ids"] == []
    revision = watchlist.revision()
    assert s03_watchlist.sync_archived_selection(tmp_path, saved, RUN) is None
    assert watchlist.revision() == revision
    assert watchlist.list_symbols() == rows
    assert receipt["selected_total"] == (0 if empty else 2)


def test_file_marker_failure_restores_original_then_retries(tmp_path, groups, monkeypatch):
    saved = save(tmp_path, snapshot())
    before, revision = watchlist._path().read_bytes(), watchlist.revision()
    marker = s03_watchlist._marker
    def broken(storage, value=None):
        if value is not None:
            raise OSError("fixture persistence failure")
        return marker(storage)
    monkeypatch.setattr(s03_watchlist, "_marker", broken)
    with pytest.raises(s03_watchlist.WatchlistSyncError, match="自选"):
        s03_watchlist.sync_archived_selection(tmp_path, saved, RUN)
    assert watchlist._path().read_bytes() == before
    assert watchlist.revision() == revision
    monkeypatch.setattr(s03_watchlist, "_marker", marker)
    assert s03_watchlist.sync_archived_selection(tmp_path, saved, RUN)["selected_total"] == 2


@pytest.mark.parametrize("case", ["unsaved", "deadline", "missing_group", "older", "duplicate", "too_many"])
def test_invalid_archive_never_clears_group(tmp_path, groups, case):
    raw = snapshot()
    if case == "duplicate":
        raw["rows"] *= 2
    if case == "too_many":
        raw["rows"] = [{"symbol": f"{i:06}.SH"} for i in range(31)]
    saved = save(tmp_path, raw)
    if case == "missing_group":
        watchlist._write_groups([])
    if case == "older":
        s03_watchlist._marker(tmp_path, {"as_of": "2026-11-03"})
    before, revision = watchlist._path().read_bytes(), watchlist.revision()
    with pytest.raises(s03_watchlist.WatchlistSyncError):
        s03_watchlist.sync_archived_selection(tmp_path, raw if case == "unsaved" else saved,
            RUN.replace(hour=22, second=1) if case == "deadline" else RUN)
    assert watchlist._path().read_bytes() == before
    assert watchlist.revision() == revision


def test_missing_or_old_policy_archive_has_no_action(tmp_path, groups):
    before = watchlist._path().read_bytes()
    assert s03_watchlist.sync_archived_selection(tmp_path, None, RUN) is None
    assert s03_watchlist.sync_archived_selection(tmp_path, {"rows": []}, RUN) is None
    assert watchlist._path().read_bytes() == before


def test_scheduled_local_retries_saved_scoring_only(tmp_path, groups, monkeypatch):
    from app.services.strategy_monitor_tasks import set_monitoring
    from app.strategy.engine import StrategyEngine
    code_dir = tmp_path / "custom"
    code_dir.mkdir()
    (code_dir / "s03.py").write_text(CODE.replace("custom_test", SID))
    engine = StrategyEngine([code_dir])
    set_monitoring(tmp_path, SID, "s03", True)
    s02 = ResultIdentity("custom_mvxxcp88", "s02", "v1", "{}", "{}", "null", "stock", "1m")
    DailySelections(tmp_path).save(s02, {**snapshot(), "scoring_policy": None,
        "scheduled_for": RUN.replace(hour=20, minute=0).isoformat(),
        "generated_at": RUN.replace(hour=20).isoformat()}, started_at=1)
    (tmp_path / "settlement_ready.json").write_text('{"day":"2026-10-30"}')
    calls = []
    monkeypatch.setattr(daily_selections, "compute_selections", lambda *a, **kw:
        calls.append(kw) or {"selections": {SID: snapshot()}, "errors": {}})
    original = s03_watchlist._marker
    def broken(storage, value=None):
        if value is not None:
            raise OSError("fixture marker")
        return original(storage)
    monkeypatch.setattr(s03_watchlist, "_marker", broken)
    service = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    failed = daily_selections.generate_scheduled_local(engine, service, tmp_path, now=RUN)
    assert "自选" in failed["errors"][SID]
    assert DailySelections(tmp_path).read(SID, TARGET) is not None
    assert DailySelections(tmp_path).job(TARGET + ":s03")["state"] == "failed"
    monkeypatch.setattr(s03_watchlist, "_marker", original)
    recovered = daily_selections.generate_scheduled_local(engine, service, tmp_path, now=RUN)
    assert recovered == {"saved": [], "errors": {}, "watchlist_changed": True}
    assert len(calls) == 1
    assert DailySelections(tmp_path).job(TARGET + ":s03")["state"] == "succeeded"


def test_subscriber_coalesces_watchlist_change_and_resets_after_delivery():
    subscriber = QuoteSubscriber()
    subscriber.notify_strategy_results(watchlist_changed=True)
    subscriber.notify_strategy_results()
    assert subscriber.pop()["watchlist_changed"] is True
    subscriber.notify_strategy_results()
    assert subscriber.pop()["watchlist_changed"] is False
