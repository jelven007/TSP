"""A readable legacy archive cannot complete the current S03 selection task."""
import gzip
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from test_minute_signal_sections import CODE

from app.market_time import CN_TZ
from app.services.daily_selections import DailySelections, generate_scheduled_local
from app.services.s03_selection import POLICY, freeze_source
from app.services.strategy_monitor_tasks import monitoring_summary, set_monitoring
from app.services.strategy_results import ResultIdentity
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine

S02 = "custom_mvxxcp88"
S03 = "custom_mvxxcs03"
SOURCE = date(2026, 11, 20)
TARGET = date(2026, 11, 23)


def identity(sid):
    return ResultIdentity(sid, "s03" if sid == S03 else "s02", "v1",
                          "{}", "{}", "null", "stock", "1m")


def legacy(rows=None):
    return {
        "as_of": str(TARGET), "source_date": str(SOURCE),
        "scheduled_for": "2026-11-20T20:00:00+08:00",
        "generated_at": "2026-11-20T20:05:00+08:00",
        "rows": rows if rows is not None else [{"symbol": "600001.SH"}],
    }


def five(snapshot):
    return {
        **legacy(), "scheduled_for": "2026-11-20T22:00:00+08:00",
        "generated_at": "2026-11-20T22:05:00+08:00",
        "scoring_policy": POLICY, "source_selection": freeze_source(snapshot, SOURCE, TARGET),
        "rows": [{"symbol": "600001.SH", "selection_rank": 1, "score": 80.}],
    }


@pytest.fixture
def configured(tmp_path):
    (tmp_path / "s03.py").write_text(CODE.replace("custom_test", S03))
    custom_signals.save_one(tmp_path, {
        "id": "trigger", "name": "s03 early entry", "kind": "entry", "enabled": True,
        "timeframe": "intraday", "min_bars": 0, "conditions": [],
        "template": "late_session_strength",
        "params": {"time": "10:00:00", "end_time": "10:30:00",
                   "evaluation_delay_minutes": .5,
                   "entry_groups": {"a": {"enabled": True, "score_min": 55},
                                    "b": {"enabled": False}}},
    })
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    set_monitoring(tmp_path, S03, "s03", True)
    store = DailySelections(tmp_path)
    # S02依赖使用当前21点时钟; legacy保留为旧S03归档读取证据。
    store.save(identity(S02), {**legacy(),
        "scheduled_for": "2026-11-20T21:00:00+08:00",
        "generated_at": "2026-11-20T21:05:00+08:00"}, started_at=1)
    old = store.save(identity(S03), legacy(), started_at=2)
    return engine, store, old


def test_old_archive_does_not_complete_current_s03_nightly_task(configured, tmp_path):
    engine, _store, _old = configured
    result = monitoring_summary(engine, tmp_path,
        now=datetime(2026, 11, 20, 22, 30, tzinfo=CN_TZ))["strategies"][0]
    assert result["tasks"]["select"]["state"] != "success"


def test_new_policy_saves_separately_and_retains_original_legacy_record(configured):
    _engine, store, old = configured
    source = store.read(S02, TARGET)
    new = store.save(identity(S03), five(source), started_at=3)
    assert new["result"]["selection_snapshot"].get("scoring_policy") == POLICY
    assert new["record_id"] != old["record_id"]
    assert store.results.detail(S03, old["record_id"]) == old
    assert store.read(S03, TARGET)["scoring_policy"] == POLICY


def test_nightly_window_computes_once_despite_readable_legacy_archive(
        configured, tmp_path, monkeypatch):
    engine, store, old = configured
    (tmp_path / "settlement_ready.json").write_text('{"day":"2026-11-20"}')
    calls = []
    source = store.read(S02, TARGET)

    def compute(*args, **kwargs):
        calls.append(kwargs["source_selection"])
        return {"selections": {S03: five(source)}, "errors": {}}

    monkeypatch.setattr("app.services.daily_selections.compute_selections", compute)
    monkeypatch.setattr("app.services.s03_watchlist.sync_archived_selection", lambda *_: None)
    service = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    now = datetime(2026, 11, 20, 22, 10, tzinfo=CN_TZ)
    before = deepcopy(old)
    generated = generate_scheduled_local(engine, service, tmp_path, now=now)
    assert generated["saved"] == [S03]
    assert generated["errors"] == {}
    assert len(calls) == 1
    assert generate_scheduled_local(engine, service, tmp_path, now=now)["saved"] == []
    assert len(calls) == 1
    assert store.results.detail(S03, old["record_id"]) == before


def test_weekend_legacy_archive_reports_next_complete_cycle_without_writing(
        configured, tmp_path):
    engine, store, old = configured
    rows = monitoring_summary(engine, tmp_path,
        now=datetime(2026, 11, 22, 12, tzinfo=CN_TZ))["strategies"]
    task = rows[0]["tasks"]["buy"]
    assert task["state"] == "blocked"
    assert task["target_date"] == "2026-11-24"
    assert any("2026-11-23" in message and "2026-11-24" in message
               for message in task["errors"])
    assert store.results.detail(S03, old["record_id"]) == old


@pytest.mark.parametrize("rows", [[], [{"symbol": "600001.SH", "score": 80., "selection_rank": 1}]])
def test_current_selection_survives_minute_save_and_replaces_only_projection(
        configured, rows):
    _engine, store, old = configured
    prior = store.results.save(identity(S03), {
        "as_of": str(TARGET), "section_mode": "signals", "rows": [], "total": 0,
        "selection_snapshot": old["result"]["selection_snapshot"],
        "sections": {"sell": [], "select": old["result"]["sections"]["select"], "buy": []},
    }, started_at=3)
    store.save(identity(S03), {**five(store.read(S02, TARGET)), "rows": rows}, started_at=4)
    read = store.results.read(identity(S03), str(TARGET))
    assert read["result"]["sections"]["select"] == rows
    view = json.loads(gzip.decompress(store.results.day_view(S03, str(TARGET))[1]))
    assert view["result"]["sections"]["select"] == rows
    assert store.results.detail(S03, prior["record_id"]) == prior
    saved = store.results.save(identity(S03), {
        "as_of": str(TARGET), "section_mode": "signals", "rows": [], "total": 0,
        "sections": {"sell": [], "select": [{"symbol": "WRONG"}], "buy": []},
    }, started_at=5)
    assert saved["result"]["sections"]["select"] == rows
    assert store.read(S03, TARGET)["rows"] == rows
    assert store.results.detail(S03, old["record_id"]) == old


def test_original_slot_current_policy_is_reused_and_identity_stays_stable(configured, tmp_path):
    _engine, store, old = configured
    source = store.read(S02, TARGET)
    store = DailySelections(tmp_path / "original_slot")
    original = store.results.save(replace(identity(S03), daily_record=True), {
        **old["result"], "selection_snapshot": five(source),
    }, started_at=3)
    again = store.save(identity(S03), five(source), started_at=4)
    assert again == original
    assert store.read(S03, TARGET)["scoring_policy"] == POLICY
    policy_identity = replace(identity(S03), daily_record=True, selection_policy=POLICY)
    assert policy_identity.fingerprint == identity(S03).fingerprint
    assert len(policy_identity.record_id(str(TARGET))) == 92


@pytest.mark.parametrize("clock,target", [
    ((2026, 11, 23, 8, 0), "2026-11-24"),
    ((2026, 11, 23, 23, 0), "2026-11-24"),
    ((2026, 11, 23, 23, 1), "2026-11-25"),
])
def test_missing_archive_effective_date_respects_submission_window(configured, tmp_path, clock, target):
    engine, _store, _old = configured
    task = monitoring_summary(engine, tmp_path, now=datetime(*clock, tzinfo=CN_TZ))["strategies"][0]["tasks"]["buy"]
    assert task["state"] == "blocked"
    assert task["target_date"] == target


def test_successful_five_archive_unblocks_target_without_blocking_legacy_null_groups(
        configured, tmp_path):
    engine, store, _old = configured
    store.save(identity(S03), five(store.read(S02, TARGET)), started_at=3)
    tasks = monitoring_summary(engine, tmp_path,
        now=datetime(2026, 11, 23, 8, tzinfo=CN_TZ))["strategies"][0]["tasks"]
    assert tasks["buy"]["state"] == "waiting"
    signal = custom_signals.load_all(tmp_path)[0]
    signal["params"]["entry_groups"] = None
    custom_signals.save_one(tmp_path, signal)
    tasks = monitoring_summary(engine, tmp_path,
        now=datetime(2026, 11, 24, 8, tzinfo=CN_TZ))["strategies"][0]["tasks"]
    assert tasks["buy"]["state"] != "blocked"


def test_local_compute_cannot_complete_with_legacy_response(configured, tmp_path, monkeypatch):
    engine, store, old = configured
    (tmp_path / "settlement_ready.json").write_text('{"day":"2026-11-20"}')
    monkeypatch.setattr("app.services.daily_selections.compute_selections", lambda *_a, **_k: {
        "selections": {S03: legacy()}, "errors": {}})
    service = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    result = generate_scheduled_local(engine, service, tmp_path,
        now=datetime(2026, 11, 20, 22, 10, tzinfo=CN_TZ))
    assert result["saved"] == []
    assert "不兼容" in result["errors"][S03]
    assert store.read(S03, TARGET) is None
    assert store.results.detail(S03, old["record_id"]) == old
