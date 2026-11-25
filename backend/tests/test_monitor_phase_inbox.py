"""Completed phases stay unacknowledged until the current Worker commits them."""
# ruff: noqa: F401 -- fixed phase-clock fixture is intentionally imported.
import importlib
import importlib.util
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_monitor_phase_processes import SID, SIGNAL, phase_clock, reply
from test_monitor_phase_processes import payload as original_payload


def payload():
    return deepcopy(original_payload())


def inbox():
    name = "app.multitenancy.monitor_phase_inbox"
    assert importlib.util.find_spec(name), "nonblocking phase receipt inbox is missing"
    return importlib.import_module(name).PhaseInbox()


def ready(queue, task, execute):
    end = time.monotonic() + 2
    while time.monotonic() < end:
        result = queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute, isolated=True))
        if result.get("results"):
            return result
        threading.Event().wait(.005)
    pytest.fail("Completed phase was not available to the single writer")


def test_blocked_observation_does_not_hold_selected_stock_receipt():
    queue = inbox()
    blocked, release = threading.Event(), threading.Event()
    task = payload()
    task["strategy_task_windows"][SID]["buy"][0]["observe_breadth"] = True
    task["strategy_task_windows"][SID]["buy"][0]["signal"]["params"]["pool_up_min_pct"] = None
    calls = []

    def execute(value, **kwargs):
        phase = "breadth" if value["kind"] == "monitor_breadth" else "stocks"
        calls.append(phase)
        if phase == "breadth":
            blocked.set()
            assert release.wait(2)
        return reply(value, phase)

    try:
        started = time.monotonic()
        queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute, isolated=True))
        assert time.monotonic() - started < .2, "Worker kept the tenant lease while breadth blocked"
        assert blocked.wait(1)
        selected = ready(queue, task, execute)
        assert selected["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["stocks"]["state"] == "success"
        assert not release.is_set()
        assert calls.count("stocks") == 1
    finally:
        release.set()
        queue.close()


def test_transaction_rollback_redelivers_same_completed_phase_until_ack():
    queue = inbox()
    task = payload()
    task["strategy_task_plans"][SID]["actions"] = [task["strategy_task_plans"][SID]["actions"][1]]
    calls = []

    def execute(value, **kwargs):
        calls.append(True)
        return reply(value, "stocks")

    try:
        first = ready(queue, task, execute)
        second = queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute, isolated=True))
        assert first["_phase_ack"] == second["_phase_ack"]
        assert len(calls) == 1
        queue.ack(first["_phase_ack"])
        assert not queue.pending(task["tenant_id"])
    finally:
        queue.close()


def test_old_configuration_and_monitor_generation_cannot_deliver_completed_phase():
    queue = inbox()
    task = payload()
    task["_configuration_fingerprint"] = "old"
    task["strategy_task_generations"] = {SID: 1}
    task["strategy_task_plans"][SID]["actions"] = [task["strategy_task_plans"][SID]["actions"][1]]

    def execute(value, **kwargs):
        return reply(value, "stocks")

    try:
        old = ready(queue, task, execute)
        changed = deepcopy(task)
        changed["_configuration_fingerprint"] = "new"
        changed["strategy_task_generations"][SID] = 2
        result = queue.advance(changed, SimpleNamespace(), SimpleNamespace(execute=execute, isolated=True))
        assert set(result.get("_phase_ack", [])) <= set(queue.keys())
        assert not set(old["_phase_ack"]) & set(queue.keys())
    finally:
        queue.close()


def test_late_breadth_merges_only_its_phase_into_current_stock_terminal():
    queue = inbox()
    task = payload()
    task["strategy_task_plans"][SID]["actions"] = [task["strategy_task_plans"][SID]["actions"][0]]
    task["strategy_task_results"][SID] = reply(task, "stocks")["results"][SID]
    task["state"] = reply(task, "stocks")["state"]

    def execute(value, **kwargs):
        return reply(value, "breadth")

    try:
        result = ready(queue, task, execute)
        saved = result["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]
        assert saved["stocks"]["state"] == saved["breadth"]["state"] == "success"
        state = result["state"]["engine"]["late_session_tasks"][SID + ":csgi_" + SIGNAL["id"]]["independent_v1"]
        assert state["stocks"]["phase_proof"] == "stocks"
        assert state["breadth"]["phase_proof"] == "breadth"
    finally:
        queue.close()


def test_first_breadth_dispatch_does_not_prevent_later_due_stock_dispatch():
    queue = inbox()
    task = payload()
    breadth_action, stocks_action = task["strategy_task_plans"][SID]["actions"]
    task["strategy_task_plans"][SID]["actions"] = [breadth_action]
    blocked, release = threading.Event(), threading.Event()

    def execute(value, **kwargs):
        phase = "breadth" if value["kind"] == "monitor_breadth" else "stocks"
        if phase == "breadth":
            blocked.set()
            assert release.wait(2)
        return reply(value, phase)

    try:
        queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute, isolated=True))
        assert blocked.wait(1)
        task["strategy_task_plans"][SID]["actions"] = [breadth_action, stocks_action]
        result = ready(queue, task, execute)
        assert result["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["stocks"]["state"] == "success"
        assert not release.is_set()
    finally:
        release.set()
        queue.close()


def test_old_completed_phase_cannot_overwrite_persisted_expired_phase():
    queue = inbox()
    task = payload()
    task["strategy_task_plans"][SID]["actions"] = [task["strategy_task_plans"][SID]["actions"][0]]
    release, entered = threading.Event(), threading.Event()

    def execute(value, **kwargs):
        entered.set()
        assert release.wait(2)
        return reply(value, "breadth")

    try:
        queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute))
        assert entered.wait(1)
        expired = reply(task, "breadth")["results"][SID]
        step = expired["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["breadth"]
        step.update(state="expired", passed=None)
        task["strategy_task_results"][SID] = expired
        task["strategy_task_plans"][SID]["actions"] = []
        task["strategy_task_ids"] = []
        release.set()
        result = queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute))
        assert not result["results"] and not queue.pending(task["tenant_id"])
        assert step["state"] == "expired"
    finally:
        release.set()
        queue.close()


def test_restart_resumes_unpublished_join_without_recomputing_completed_stocks():
    from test_monitor_phase_processes import NOW

    from app.services.strategy_monitor_tasks import apply_independent_buy
    task = payload()
    stocks = reply(task, "stocks")["results"][SID]
    stocks["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["breadth"] = (
        reply(task, "breadth")["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["breadth"])
    daily = apply_independent_buy(stocks, task["strategy_task_windows"][SID], NOW)
    task["strategy_task_results"][SID] = daily
    task["strategy_task_receipts"][SID] = {"tasks": daily["monitor_tasks"]}
    task["strategy_task_ids"] = []
    task["strategy_task_plans"][SID]["actions"] = []
    queue, calls = inbox(), []

    def execute(value, **kwargs):
        calls.append(value["monitor_phase"])
        assert value["strategy_task_plans"][SID]["actions"] == []
        return {"results": {SID: deepcopy(daily)}, "events": [{"strategy_id": SID, "type": "buy_signal"}],
                "execution_events": [], "state": value["state"]}

    try:
        saved = ready(queue, task, execute)
        assert calls == ["joined"]
        assert saved["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["publication_state"] == "complete"
        queue.ack(saved["_phase_ack"])
        task["strategy_task_results"][SID] = saved["results"][SID]
        task["strategy_task_receipts"][SID] = {"tasks": saved["results"][SID]["monitor_tasks"]}
        assert not queue.advance(task, SimpleNamespace(), SimpleNamespace(execute=execute))["events"]
        assert calls == ["joined"]
    finally:
        queue.close()
