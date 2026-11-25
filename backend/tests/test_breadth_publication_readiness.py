"""Reproduce the 14:45 old-release failure without mutable market inputs."""
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.multitenancy import monitor_compute
from app.services.screener import ScreenerService

SID = "custom_mvxxcs01"
SIGNAL = "late_session_strength_s01"
DAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 14, 45, 20, tzinfo=CN_TZ)


def request(completed=NOW):
    signal = {"id": SIGNAL, "template": "late_session_strength", "params": {
        "time": "14:45:00", "end_time": "15:00:00", "evaluation_delay_minutes": 1,
        "pool_up_min_pct": 65, "change_min_pct": 2, "change_max_pct": 3,
        "amount_min_100m": 1, "amplitude_max_pct": 5.5, "ma5_1m_count": None,
        "ma5_5m_count": None, "turnover_min_pct": None, "volume_ratio_min": None}}
    return {"state": {}, "rules": [], "strategy_task_ids": [SID],
        "snapshot": {"public_release": {"minute_completed_at": completed.timestamp()}},
        "strategy_task_plans": {SID: {"actions": [{
            "kind": "buy", "phase": "breadth", "signal_id": SIGNAL,
            "scheduled_at": NOW.replace(second=15).isoformat(),
            "retry_until": NOW.replace(minute=55, second=0).isoformat()}]}},
        "strategy_task_windows": {SID: {"buy": [{"signal": signal,
            "start": "14:45:00", "end": "14:55:00", "phase_policy": "independent_v1"}]}},
        "strategy_task_receipts": {}, "strategy_task_results": {},
        "daily_identities": {SID: {"runtime_identity": SID, "asset_type": "stock", "timeframe": "1m"}},
        "daily_pool_snapshots": {SID: {"symbols": ["600001.SH", "600002.SH"]}},
        "late_task_scopes": {SID: SID}, "selection_snapshots": {}, "sell_rosters": {}}


def run(monkeypatch, repo, payload=None):
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: NOW)
    monkeypatch.setattr("app.strategy.basic_filter.previous_session", lambda _: date(2026, 11, 23))
    monkeypatch.setattr(ScreenerService, "_load_enriched_history",
                        lambda *_: pytest.fail("breadth unnecessarily loaded indicator history"))
    result = monitor_compute.evaluate_breadth_phase(payload or request(), repo)
    return result["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL]["breadth"]


def test_old_public_release_waits_without_failed_attempt_or_market_io(monkeypatch):
    reads = []

    def read(*_args, **_kwargs):
        reads.append(True)
        raise OSError("14:45 minute is not in the 14:44 release")

    step = run(monkeypatch, SimpleNamespace(get_minute_batch=read, get_daily_close_inputs=read),
               request(NOW.replace(minute=44, second=45)))
    assert step["state"] == "running" and not step["terminal"]
    assert not step["errors"] and step["first_error"] is None
    assert step["passed"] is None and step["completed_at"] is None
    assert step["next_at"] and not reads


@pytest.mark.parametrize("second_close, expected", [(10.25, "success"), (9.75, "rejected")])
def test_breadth_reads_only_exact_raw_previous_close(monkeypatch, second_close, expected):
    reads = []

    def raw(day, *, include_closing_minute=True):
        reads.append((day, include_closing_minute))
        return pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
            "date": [date(2026, 11, 23)] * 2, "close": [10., 10.]}), pl.DataFrame()

    minutes = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
        "datetime": [NOW.replace(second=0, tzinfo=None)] * 2, "close": [10.25, second_close]})
    step = run(monkeypatch, SimpleNamespace(get_minute_batch=lambda *_a, **_k: minutes,
                                           get_daily_close_inputs=raw))
    assert step["state"] == expected and step["terminal"] and not step["errors"]
    assert step["pool_total"] == step["pool_usable"] == 2
    assert step["pool_up_pct"] == (100 if expected == "success" else 50)
    assert reads == [(date(2026, 11, 23), False)]


def test_raw_reference_duplicate_is_isolated_without_shrinking_pool(monkeypatch):
    daily = pl.DataFrame({"symbol": ["600001.SH", "600002.SH", "600002.SH"],
        "date": [date(2026, 11, 23)] * 3, "close": [10., 10., 11.]})
    minutes = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
        "datetime": [NOW.replace(second=0, tzinfo=None)] * 2, "close": [10.25, 10.25]})
    step = run(monkeypatch, SimpleNamespace(
        get_minute_batch=lambda *_a, **_k: minutes,
        get_daily_close_inputs=lambda *_a, **_k: (daily, pl.DataFrame())))
    assert step["state"] == "success"
    assert step["pool_total"] == 2 and step["pool_usable"] == 1
    assert step["missing_symbols"] == ["600002.SH"]
    assert "600002.SH" in step["reference_errors"]["600002.SH"]


def test_new_release_with_no_valid_target_minute_still_fails(monkeypatch):
    daily = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 23)], "close": [10.]})
    minutes = pl.DataFrame({"symbol": ["600001.SH"],
        "datetime": [NOW.replace(minute=44, second=0, tzinfo=None)], "close": [10.25]})
    step = run(monkeypatch, SimpleNamespace(
        get_minute_batch=lambda *_a, **_k: minutes,
        get_daily_close_inputs=lambda *_a, **_k: (daily, pl.DataFrame())))
    assert step["state"] == "failed" and not step["terminal"]
    assert step["errors"] and step["pool_up_pct"] is None


def test_selected_publication_can_release_wait_without_whole_market_clock(monkeypatch):
    payload = deepcopy(request(NOW.replace(minute=44)))
    payload["snapshot"]["public_release"]["minute_priority_completed_at"] = NOW.timestamp()
    daily = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 23)], "close": [10.]})
    minutes = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
        "datetime": [NOW.replace(second=0, tzinfo=None)] * 2, "close": [10.25, 10.25]})
    step = run(monkeypatch, SimpleNamespace(
        get_minute_batch=lambda *_a, **_k: minutes,
        get_daily_close_inputs=lambda *_a, **_k: (daily, pl.DataFrame())), payload)
    assert step["state"] == "success" and step["pool_total"] == 2
    assert step["pool_usable"] == 1 and step["missing_symbols"] == ["600002.SH"]


def test_completed_market_publication_isolates_absent_minutes(monkeypatch):
    daily = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 23)], "close": [10.]})
    minutes = pl.DataFrame({"symbol": ["600001.SH"],
        "datetime": [NOW.replace(second=0, tzinfo=None)], "close": [10.25]})
    step = run(monkeypatch, SimpleNamespace(
        get_minute_batch=lambda *_a, **_k: minutes,
        get_daily_close_inputs=lambda *_a, **_k: (daily, pl.DataFrame())))
    assert step["state"] == "success" and not step["errors"]
    assert step["pool_total"] == 2 and step["pool_usable"] == 1
    assert step["missing_symbols"] == ["600002.SH"]


def test_priority_fragment_still_waits_for_absent_target_minute(monkeypatch):
    payload = request(NOW.replace(minute=44))
    payload["snapshot"]["public_release"]["minute_priority_completed_at"] = NOW.timestamp()
    daily = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 23)], "close": [10.]})
    minutes = pl.DataFrame({"symbol": ["600001.SH"],
        "datetime": [NOW.replace(second=0, tzinfo=None)], "close": [10.25]})
    step = run(monkeypatch, SimpleNamespace(
        get_minute_batch=lambda *_a, **_k: minutes,
        get_daily_close_inputs=lambda *_a, **_k: (daily, pl.DataFrame())), payload)
    assert step["state"] == "failed" and not step["terminal"]
    assert "启动分钟尚未到齐" in step["errors"][0]
