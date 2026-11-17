"""A completed empty buy must survive later attempts and remain a sell dependency."""
# ruff: noqa: F401,F811 -- shared disposable PostgreSQL fixtures.
import gzip
import json
from copy import deepcopy
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest
from test_full_identity import env, full_cluster
from test_full_runtime import owned, price_rule

from app.api import screener
from app.market_time import CN_TZ, cn_today
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.runtime import fingerprint
from app.services.strategy_results import ResultIdentity


def completion(day, runtime_identity="current"):
    return {
        "strategy_id": "custom_test", "as_of": str(day), "state": "succeeded", "buy_count": 0,
        "runtime_identity": runtime_identity,
        "completed_at": int(datetime.combine(day, time(14, 31), CN_TZ).timestamp() * 1000),
        "confirmation_at": datetime.combine(day, time(14, 30, 30), CN_TZ).isoformat(),
        "deadline_at": datetime.combine(day, time(14, 55), CN_TZ).isoformat(),
    }


def selection(day):
    return {
        "as_of": str(day), "computed_at": 1, "runtime_identity": "current", "selection_only": True,
        "rows": [], "total": 0, "section_mode": "signals",
        "sections": {"sell": [], "select": [{"symbol": "600000.SH"}], "buy": []},
        "section_errors": {"buy": "仅预选, 买入尚未计算"},
    }


def test_successful_zero_receipt_overrules_later_expired_attempt(owned, monkeypatch):
    day = cn_today()
    sid = "custom_test"
    with owned.documents.transaction() as unit:
        observed = fingerprint(unit.connection, include_state=False)
        unit.put("critical_strategy_runs", f"{sid}:{day}", completion(day))
        unit.put("strategy_execution_status", sid, {
            "state": "expired", "as_of": str(day), "runtime_identity": "current",
            "reason": "超过14:55截止", "fingerprint": observed, "updated_at": 1,
        })
    monkeypatch.setattr(screener, "_private_data_dir", lambda *_: owned.documents)
    identity = SimpleNamespace(strategy_id=sid, runtime_identity="current")
    result = screener._with_execution_status(None, {sid: selection(day)}, str(day), identities=[identity])[sid]
    assert result["section_statuses"]["buy"] == "success"
    assert result["execution_status"]["state"] == "success"
    assert result["selection_only"] is False
    assert not result.get("showing_last_success")
    assert result["sections"]["buy"] == []
    assert result["sections"]["select"] == [{"symbol": "600000.SH"}]


def test_deadline_keeps_completed_zero_even_without_live_or_archive(owned, monkeypatch):
    day = cn_today()
    sid = "ma_golden_cross"
    owned.clock["now"] = datetime.combine(day, time(15, 1), CN_TZ).timestamp()
    Rules(owned.documents).save_one(price_rule())
    key = ResultIdentity(sid, "S02", "v1", "{}", "{}", "null", "stock", "1m", runtime_identity="current")
    owned.documents.put("critical_strategy_runs", f"{sid}:{day}", {**completion(day), "strategy_id": sid})
    prepare = owned.worker.prepare

    def inject(*args):
        account, context, payload, observed = prepare(*args)
        payload["daily_identities"][sid] = __import__("dataclasses").asdict(key)
        payload["entry_deadlines"][sid] = f"{day}T14:55:00"
        return account, context, payload, observed

    monkeypatch.setattr(owned.worker, "prepare", inject)
    owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert owned.documents.get("strategy_execution_status", sid)["state"] == "success"


def test_selection_only_never_counts_as_last_success():
    from app.strategy.execution_status import merge_result
    daily = selection(cn_today())
    result = merge_result(daily, {**deepcopy(daily), "execution_status": {
        "state": "expired", "reason": "未完成确认", "updated_at": 1}})
    assert not result.get("showing_last_success")
    assert result.get("last_success_at") is None


def test_completed_zero_is_materialized_and_confirms_tomorrow_empty_sell(owned):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_results import StrategyResults

    day = cn_today()
    sid = "custom_test"
    key = ResultIdentity(sid, "S02", "v1", "{}", "{}", "null", "stock", "1m", runtime_identity="current")
    owned.documents.put("critical_strategy_runs", f"{sid}:{day}", completion(day))
    store = StrategyResults(owned.documents)
    DailySelections(owned.documents).save(key, {
        "as_of": str(day), "source_date": str(day - timedelta(days=1)),
        "rows": [{"symbol": "600000.SH"}],
    }, started_at=1)
    current = json.loads(gzip.decompress(store.day_view(sid, str(day))[1]))["result"]
    assert current["section_statuses"]["buy"] == "success"
    assert current["selection_only"] is False
    assert current["sections"]["select"] == [{"symbol": "600000.SH"}]
    tomorrow = day + timedelta(days=1)
    future = DailySelections(owned.documents).save(key, {
        "as_of": str(tomorrow), "source_date": str(day), "rows": [],
    }, started_at=2)["result"]
    assert "sell" not in future.get("section_errors", {})
    assert future["section_statuses"]["sell"] == "success"
    assert future["sections"]["sell"] == []
    assert "buy" in future["section_errors"]
    assert future["selection_only"] is True


def test_archived_selection_maps_its_identity_before_zero_status_overlay(owned, monkeypatch):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_results import StrategyResults

    day = cn_today()
    key = ResultIdentity("custom_test", "S02", "v1", "{}", "{}", "null",
                         "stock", "1m", runtime_identity="current")
    store = StrategyResults(owned.documents)
    DailySelections(owned.documents).save(key, {
        "as_of": str(day), "source_date": str(day - timedelta(days=1)), "rows": [],
    }, started_at=1)
    with owned.documents.transaction() as unit:
        archived = unit.records("strategy_result_history")[0]
        archived["value"]["result"].pop("runtime_identity", None)
        unit.put("strategy_result_history", archived["id"], archived["value"])
    owned.documents.put("critical_strategy_runs", f"custom_test:{day}", completion(day))
    monkeypatch.setattr(screener, "_private_data_dir", lambda *_: owned.documents)
    monkeypatch.setattr(screener, "_history_identities", lambda *a, **kw: (store, [key]))
    monkeypatch.setattr(screener, "_strategy_engine", lambda *_: SimpleNamespace(
        get=lambda *_: SimpleNamespace(meta={"result_sections": "signals"})))
    result = screener._history_payload(None, day, "stock", "1m")["results"]["custom_test"]
    assert result["section_statuses"]["buy"] == "success"
    assert result["execution_status"]["state"] == "success"
    assert result["runtime_identity"] == "current"
    assert result["selection_only"] is False


@pytest.mark.parametrize("change", [
    {"state": "expired"}, {"buy_count": 1}, {"buy_count": None}, {"buy_count": False},
    {"runtime_identity": "other"}, {"as_of": "2026-11-16"},
    {"completed_at": float("nan")}, {"completed_at": float("inf")}, {"deadline_at": "bad"},
])
def test_invalid_or_nonempty_receipt_does_not_convert_unknown_to_zero(change):
    from app.strategy.execution_status import with_zero_completion

    day = cn_today()
    daily = selection(day)
    assert with_zero_completion(daily, {**completion(day), **change}, "custom_test") is daily


def test_next_day_cached_archive_refreshes_sell_dependency_from_completed_zero(owned, monkeypatch):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_results import StrategyResults

    day = cn_today()
    tomorrow = day + timedelta(days=1)
    key = ResultIdentity("custom_test", "S02", "v1", "{}", "{}", "null",
                         "stock", "1m", runtime_identity="current")
    selected = DailySelections(owned.documents)
    selected.save(key, {"as_of": str(day), "source_date": str(day - timedelta(days=1)), "rows": []}, started_at=1)
    selected.save(key, {"as_of": str(tomorrow), "source_date": str(day), "rows": []}, started_at=2)
    owned.documents.put("critical_strategy_runs", f"custom_test:{day}", completion(day))
    monkeypatch.setattr(screener, "_private_data_dir", lambda *_: owned.documents)
    monkeypatch.setattr(screener, "_history_identities", lambda *a, **kw: (StrategyResults(owned.documents), [key]))
    monkeypatch.setattr(screener, "_strategy_engine", lambda *_: SimpleNamespace(
        get=lambda *_: SimpleNamespace(meta={"result_sections": "signals"})))
    result = screener._history_payload(None, tomorrow, "stock", "1m")["results"]["custom_test"]
    assert not result["section_errors"].get("sell")
    assert result["section_statuses"]["sell"] == "success"
    assert result["sections"]["sell"] == []
    assert result["section_errors"]["buy"]


@pytest.mark.parametrize("observer", ["record", "watchdog"])
def test_late_completion_atomically_refreshes_today_and_next_saved_view(owned, observer):
    from test_critical_strategy_schedule import configure

    from app.multitenancy import critical_schedule
    from app.services.daily_selections import DailySelections
    from app.services.strategy_results import StrategyResults

    sid = configure(owned)
    day = cn_today()
    tomorrow = day + timedelta(days=1)
    key = ResultIdentity(sid, "S02", "v1", "{}", "{}", "null",
                         "stock", "1m", runtime_identity="current")
    selected = DailySelections(owned.documents)
    for target in (day, tomorrow):
        selected.save(key, {"as_of": str(target), "source_date": str(target - timedelta(days=1)),
                            "rows": [{"symbol": "600000.SH"}]}, started_at=1)
    original = owned.documents.records("strategy_result_history")
    moment = datetime.combine(day, time(14, 31), CN_TZ)
    daily = {**selection(day), "computed_at": completion(day)["completed_at"],
             "selection_only": False, "section_errors": {},
             "section_statuses": {"buy": "success"}}
    if observer == "record":
        with owned.documents.transaction() as unit:
            critical_schedule.record_completion(unit, sid, daily, now=moment,
                                                fingerprint=fingerprint(unit.connection, include_state=False))
    else:
        with owned.documents.transaction() as unit:
            unit.put("monitor_results", "default", {
                "fingerprint": fingerprint(unit.connection, include_state=False),
                "results": {sid: daily},
            })
        critical_schedule.check_completion(owned.worker, owned.documents, now=moment + timedelta(minutes=1))
    store = StrategyResults(owned.documents)
    today = json.loads(gzip.decompress(store.day_view(sid, str(day))[1]))["result"]
    future = json.loads(gzip.decompress(store.day_view(sid, str(tomorrow))[1]))["result"]
    assert today["section_statuses"]["buy"] == "success"
    assert not today["selection_only"]
    assert not future["section_errors"].get("sell")
    assert future["section_statuses"]["sell"] == "success"
    assert future["selection_only"]
    assert owned.documents.records("strategy_result_history") == original


def test_preselection_without_completion_cannot_prove_next_day_empty_sell(owned):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_results import StrategyResults

    day = cn_today()
    tomorrow = day + timedelta(days=1)
    key = ResultIdentity("custom_test", "S02", "v1", "{}", "{}", "null",
                         "stock", "1m", runtime_identity="current")
    for target in (day, tomorrow):
        DailySelections(owned.documents).save(key, {
            "as_of": str(target), "source_date": str(target - timedelta(days=1)), "rows": [],
        }, started_at=1)
    future = json.loads(gzip.decompress(StrategyResults(owned.documents).day_view("custom_test", str(tomorrow))[1]))["result"]
    assert future["section_errors"].get("sell")
    assert future.get("section_statuses", {}).get("sell") != "success"
