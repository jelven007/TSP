"""Exercise phase scheduling under blocked IO and bounded host capacity."""
import threading
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.market_time import CN_TZ
from app.multitenancy import monitor_compute
from app.services.strategy_resources import BudgetPermits

SID = "custom_mvxxcs01"
NOW = datetime(2026, 11, 24, 14, 46, 1, tzinfo=CN_TZ)
SIGNAL = {"id": "late_session_strength_s01", "template": "late_session_strength",
          "enabled": True, "params": {"time": "14:45:00", "end_time": "15:00:00",
          "evaluation_delay_minutes": 1, "pool_up_min_pct": 65,
          "change_min_pct": 2, "change_max_pct": 3, "amount_min_100m": 1,
          "amplitude_max_pct": 5.5, "ma5_1m_count": None, "ma5_5m_count": None,
          "turnover_min_pct": None, "volume_ratio_min": None}}


@pytest.fixture(autouse=True)
def phase_clock(monkeypatch):
    # Modules capture cn_now/cn_today at import; freeze every phase owner, rather
    # than allowing the real calendar day to alter this fixed Nov24 scenario.
    from app.multitenancy import monitor_phase_processes
    from app.strategy import late_session_strength

    class MarketClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(NOW.timestamp(), tz)

    monkeypatch.setattr("app.market_time.datetime", MarketClock)
    for target in (monitor_phase_processes, monitor_compute, late_session_strength):
        monkeypatch.setattr(target, "cn_now", lambda: NOW, raising=False)
        monkeypatch.setattr(target, "cn_today", lambda: NOW.date(), raising=False)
    monkeypatch.setattr("app.market_time.cn_now", lambda: NOW)
    monkeypatch.setattr("app.market_time.cn_today", lambda: NOW.date())


def payload(ids=(SID,)):
    value = {"kind": "monitor", "tenant_id": "test", "state": {"engine": {}},
             "snapshot": {"frames": {}, "status": {"is_trading_hours": False}},
             "rules": [{"strategy_id": sid, "id": sid, "type": "strategy"} for sid in ids],
             "sources": [{"id": sid, "code": sid, "source": "custom"} for sid in ids],
             "strategy_task_ids": list(ids), "strategy_task_plans": {},
             "strategy_task_windows": {}, "strategy_task_receipts": {},
             "strategy_task_results": {}, "daily_pool_snapshots": {},
             "daily_identities": {}, "late_task_scopes": {},
             "selection_snapshots": {}, "sell_rosters": {}, "signals": [], "factors": []}
    for sid in ids:
        value["strategy_task_plans"][sid] = {"actions": [
            {"kind": "buy", "signal_id": SIGNAL["id"], "phase": phase,
             "scheduled_at": NOW.replace(second=0).isoformat(),
             "retry_until": NOW.replace(minute=55, second=0).isoformat()}
            for phase in ("breadth", "stocks")]}
        value["strategy_task_windows"][sid] = {"buy": [
            {"signal": SIGNAL, "start": "14:45:00", "end": "15:00:00",
             "phase_policy": "independent_v1"}], "sell": []}
        value["daily_identities"][sid] = {"asset_type": "stock", "timeframe": "1m",
                                         "runtime_identity": sid}
        value["daily_pool_snapshots"][sid] = {"symbols": ["600001.SH", "600002.SH"]}
        value["late_task_scopes"][sid] = sid
    return value


def reply(task, phase, *, failed=False):
    sid = task["strategy_task_ids"][0]
    step = {"phase": phase, "state": "failed" if failed else "success",
            "terminal": not failed, "passed": None if failed else True,
            "errors": ["blocked breadth"] if failed else [], "attempts": 1,
            "completed_at": None if failed else NOW.isoformat()}
    if phase == "stocks":
        step.update(rows=[{"symbol": "600001.SH", "close": 10.25}],
                    hits=[{"symbol": "600001.SH", "signals": ["csgi_" + SIGNAL["id"]]}])
    else:
        step.update(pool_total=2, pool_usable=2, pool_up_pct=100)
    daily = {"as_of": str(NOW.date()), "rows": [], "total": 0,
             "monitor_tasks": {"buy": {"signals": {SIGNAL["id"]: {phase: step}}}},
             "sections": {"buy": [], "sell": [], "select": []}}
    state = {"engine": {"late_session_tasks": {
        sid + ":csgi_" + SIGNAL["id"]: {"as_of": str(NOW.date()),
                                       "independent_v1": {phase: {"phase_proof": phase}}}}}}
    return {"events": [], "execution_events": [], "results": {sid: daily}, "state": state}


def test_selected_process_starts_while_breadth_read_is_blocked(monkeypatch):
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: NOW)
    monkeypatch.setattr("app.market_time.cn_now", lambda: NOW)
    entered, release, selected = threading.Event(), threading.Event(), threading.Event()
    outputs, errors = [], []

    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        raise OSError("blocked breadth")

    def execute(task, **_kwargs):
        if task["kind"] == "monitor_breadth":
            blocked()
        selected.set()
        return reply(task, "stocks")

    def run():
        try:
            outputs.append(monitor_compute.evaluate_strategy_phases(
                payload(), SimpleNamespace(get_minute_batch=blocked),
                SimpleNamespace(execute=execute, isolated=True)))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        started_while_blocked = selected.wait(.3)
    finally:
        release.set()
        worker.join(4)
    assert not worker.is_alive() and not errors
    assert started_while_blocked, "selected-stock process waited for breadth IO"
    saved = outputs[0]["results"][SID]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]
    assert saved["stocks"]["state"] == "success"
    assert saved["breadth"]["state"] == "failed"
    assert not outputs[0]["results"][SID]["rows"]


def test_each_selected_job_contains_only_its_own_source_and_merges_phase_state(monkeypatch):
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: NOW)
    monkeypatch.setattr("app.market_time.cn_now", lambda: NOW)
    calls = []

    def execute(task, **_kwargs):
        assert len(task["strategy_task_ids"]) == 1, "strategies shared one compute process"
        sid = task["strategy_task_ids"][0]
        assert [s["id"] for s in task.get("sources", [])] == (
            [] if task["kind"] == "monitor_breadth" else [sid])
        calls.append(deepcopy(task))
        phase = "breadth" if task["kind"] == "monitor_breadth" else "stocks"
        return reply(task, phase)

    output = monitor_compute.evaluate_strategy_phases(
        payload((SID, "custom_other")), SimpleNamespace(),
        SimpleNamespace(execute=execute, isolated=True))
    assert len(calls) >= 4
    for sid in (SID, "custom_other"):
        daily = output["results"][sid]
        signal = daily["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]
        assert signal["breadth"]["state"] == signal["stocks"]["state"] == "success"
        assert daily["rows"][0]["symbol"] == "600001.SH"
        phase_state = output["state"]["engine"]["late_session_tasks"][
            sid + ":csgi_" + SIGNAL["id"]]["independent_v1"]
        assert phase_state["breadth"]["phase_proof"] == "breadth"
        assert phase_state["stocks"]["phase_proof"] == "stocks"


def test_breadth_cannot_consume_the_reserved_selected_process_slot():
    policy = {"effective_concurrency": 2, "task_memory_mb": 512, "task_cpus": .5,
              "memory_budget_mb": 1024, "cpu_budget": 1.,
              "realtime_reserved_slots": 2, "stocks_reserved_slots": 1}
    permits = BudgetPermits(lambda: policy)
    assert permits.acquire(blocking=False, priority=True)
    try:
        second = permits.acquire(blocking=False, priority=True)
        if second:
            permits.release()
        assert not second, "breadth occupied the selected-stock reservation"
        assert permits.acquire(blocking=False, priority=True, stocks=True)
        permits.release()
    finally:
        permits.release()
    assert permits.usage()["active"] == 0


def test_broker_runs_distinct_phase_processes_and_reclaims_cancelled_stock(monkeypatch):
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from app.cell.context import AccessError
    from app.multitenancy.compute_broker import ComputeBroker
    from app.multitenancy.executor import ComputeExecutor

    processes = []
    original = subprocess.Popen
    started = threading.Event()
    cancel = threading.Event()
    errors = []

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    with tempfile.TemporaryDirectory(prefix="tsp-phase-", dir="/tmp") as directory:
        root = Path(directory)
        with ComputeBroker(root / "sock", "test", root, 2,
                           host_memory_mb=4096, host_cpus=4, memory_mb=512, cpus=.5) as broker:
            monkeypatch.setattr(broker, "_resource_policy", lambda: {
                "effective_concurrency": 2, "task_memory_mb": 512, "task_cpus": .5,
                "memory_budget_mb": 2048, "cpu_budget": 2,
                "realtime_reserved_slots": 2, "stocks_reserved_slots": 1})
            monkeypatch.setattr(broker, "command", lambda *_: [
                sys.executable, "-c",
                "import json,sys,time,os; p=json.loads(sys.stdin.readline()); "
                "time.sleep(10) if p['kind']=='monitor' else None; "
                "print(json.dumps({'type':'result','payload':{'pid':os.getpid()}}))"])
            server = threading.Thread(target=broker.serve_forever, daemon=True)
            server.start()
            executor = ComputeExecutor(str(root / "sock"))

            def stocks():
                try:
                    executor.execute({"kind": "monitor", "monitor_phase": "stocks",
                                      "tenant_id": "same-tenant"}, cancel=cancel)
                except AccessError as exc:
                    errors.append(str(exc))

            worker = threading.Thread(target=stocks)
            worker.start()
            try:
                assert started.wait(2)
                breadth = executor.execute({"kind": "monitor_breadth", "tenant_id": "same-tenant"})
                assert breadth["pid"] != processes[0].pid
                assert processes[0].poll() is None
                timing = breadth["_compute_timing"]
                assert timing["process_pid"] == breadth["pid"] and timing["sandbox_name"]
                cancel.set()
                worker.join(3)
            finally:
                cancel.set()
                worker.join(3)
                broker.shutdown()
                server.join(2)
        assert not worker.is_alive()
        assert errors and "已取消" in errors[0]
        assert len(processes) == 2 and all(process.poll() is not None for process in processes)
        assert broker.permits.usage()["active"] == 0 and broker.active_tenants == set()


def test_monitor_timeout_kills_real_process_and_returns_retryable_failure(monkeypatch):
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from app.cell.context import AccessError
    from app.multitenancy.compute_broker import ComputeBroker
    from app.multitenancy.executor import ComputeExecutor
    processes = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        processes.append(original(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    with tempfile.TemporaryDirectory(prefix="tsp-timeout-", dir="/tmp") as directory:
        root = Path(directory)
        with ComputeBroker(root / "sock", "test", root, 1) as broker:
            monkeypatch.setattr(broker, "command", lambda *_: [
                sys.executable, "-c", "import sys,time; sys.stdin.readline(); time.sleep(10)"])
            server = threading.Thread(target=broker.serve_forever, daemon=True)
            server.start()
            try:
                with pytest.raises(AccessError, match="超时"):
                    ComputeExecutor(str(root / "sock")).execute({
                        "kind": "monitor_breadth", "tenant_id": "test", "phase_timeout_seconds": .1})
                assert broker.permits.usage()["active"] == 0
                assert processes[0].poll() is not None
            finally:
                broker.shutdown()
                server.join(2)


def test_bad_strategy_source_does_not_block_other_stock_or_trusted_breadth(monkeypatch):
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: NOW)
    monkeypatch.setattr("app.market_time.cn_now", lambda: NOW)
    value = payload((SID, "custom_broken"))
    value["sources"][1]["code"] = "broken syntax"

    def execute(task):
        if task["kind"] == "monitor_breadth":
            assert not task["sources"] and not task["signals"] and not task["factors"]
            return reply(task, "breadth")
        if task["sources"][0]["code"] == "broken syntax":
            raise ValueError("strategy load failed")
        return reply(task, "stocks")

    result = monitor_compute.evaluate_strategy_phases(
        value, SimpleNamespace(), SimpleNamespace(execute=execute, isolated=True))
    assert result["results"][SID]["rows"]
    broken = result["results"]["custom_broken"]["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]
    assert broken["stocks"]["state"] == "failed"
    assert broken["breadth"]["state"] == "success"
    assert result["results"]["custom_broken"]["rows"] == []


def test_expired_breadth_receipt_is_not_overwritten_by_old_snapshot():
    from app.multitenancy.monitor_phase_processes import merge_daily
    before = reply({"strategy_task_ids": [SID]}, "breadth")["results"][SID]
    expired = deepcopy(before)
    expired["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["breadth"]["state"] = "expired"
    result = merge_daily(before, expired, "stocks", [{"signal_id": SIGNAL["id"],
                        "phase": "expire", "expired_phase": "breadth"}])
    assert result["monitor_tasks"]["buy"]["signals"][SIGNAL["id"]]["breadth"]["state"] == "expired"


def test_serial_strategy_retains_its_new_breadth_receipt():
    from app.multitenancy.monitor_phase_processes import merge_daily
    old = reply({"strategy_task_ids": [SID]}, "breadth", failed=True)["results"][SID]
    updated = reply({"strategy_task_ids": [SID]}, "breadth")["results"][SID]
    assert merge_daily(old, updated, "serial", []) == updated


def test_real_s01_joins_once_without_loading_selected_minutes_again(tmp_path, monkeypatch):
    import json
    from datetime import date
    from pathlib import Path

    import polars as pl

    from app.services.screener import ScreenerService
    from app.services.strategy_monitor_tasks import task_windows
    from app.strategy import custom_signals, version
    from app.strategy.engine import StrategyEngine

    for module in (monitor_compute, __import__("app.market_time", fromlist=["cn_now"])):
        monkeypatch.setattr(module, "cn_now", lambda: NOW)
        monkeypatch.setattr(module, "cn_today", lambda: NOW.date())
    source = Path(__file__).resolve().parents[2] / "examples/strategies/s01_signal_sections.py"
    directory = tmp_path / "strategies" / "custom"
    directory.mkdir(parents=True)
    (directory / (SID + ".py")).write_bytes(source.read_bytes())
    custom_signals.save_one(tmp_path, {**SIGNAL, "kind": "entry", "timeframe": "intraday",
                                     "name": "s01", "conditions": []})
    override = {"entry_signals": ["csgi_" + SIGNAL["id"]], "exit_signals": []}
    overrides = tmp_path / "user_data" / "strategy_overrides"
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / (SID + ".json")).write_text(json.dumps(override))
    selected_reads = []

    def selected(symbols, days):
        selected_reads.append(list(symbols))
        assert symbols == ["600001.SH"] and days == [NOW.date()]
        return pl.DataFrame({"symbol": ["600001.SH"] * 2,
            "datetime": [NOW.replace(hour=9, minute=31, second=0, tzinfo=None),
                         NOW.replace(minute=45, second=0, tzinfo=None)],
            "open": [10., 10.], "high": [10.1, 10.3], "low": [10., 10.],
            "close": [10., 10.25], "amount": [40e6, 60e6], "volume": [1000., 1000.]})

    repo = SimpleNamespace(get_minute_by_dates=selected,
        get_minute_batch=lambda *_a, **_k: pl.DataFrame({
            "symbol": ["600001.SH", "600002.SH"],
            "datetime": [NOW.replace(minute=45, second=0, tzinfo=None)] * 2,
            "close": [10.25, 10.25]}))
    monkeypatch.setattr(ScreenerService, "_load_enriched_history", lambda *_a:
        pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
                      "date": [date(2026, 11, 23)] * 2, "close": [10., 10.]}))
    value = payload()
    value["sources"][0]["code"] = source.read_text()
    value["overrides"] = {SID: override}
    value["selection_snapshots"] = {SID: {"as_of": str(NOW.date()), "source_date": "2026-11-23",
        "rows": [{"symbol": "600001.SH", "reference_close": 10.}]}}
    value["rules"][0].update(notify_events=["buy_signal"], enabled=True, asset_type="stock",
                            scope="all", name="s01", cooldown=0)
    setup = StrategyEngine([directory])
    setup._user_data_dir = lambda: tmp_path
    value["strategy_task_windows"][SID] = task_windows(setup, setup.get(SID), override)
    value["late_task_scopes"][SID] = SID + ":" + version.runtime_identity(setup.get(SID), override)

    def execute(task):
        if task["kind"] == "monitor_breadth":
            return monitor_compute.evaluate_breadth_phase(task, repo)
        engine = StrategyEngine([directory])
        engine._user_data_dir = lambda: tmp_path
        return monitor_compute.evaluate_snapshot(task, repo, engine, tmp_path)

    result = monitor_compute.evaluate_strategy_phases(
        value, repo, SimpleNamespace(execute=execute, isolated=True))
    daily = result["results"][SID]
    assert daily["monitor_tasks"]["buy"]["state"] == "success", daily
    assert len(daily["rows"]) == 1 and len(selected_reads) == 1
    assert len([e for e in result["events"] if e["type"] == "buy_signal"]) == 1
    value["state"] = result["state"]
    value["strategy_task_receipts"][SID] = {"tasks": daily["monitor_tasks"]}
    value["strategy_task_results"][SID] = daily
    value["strategy_task_plans"][SID]["actions"] = [{
        "kind": "buy", "signal_id": SIGNAL["id"], "phase": "breadth",
        "scheduled_at": NOW.isoformat(), "retry_until": NOW.replace(minute=55, second=0).isoformat(),
        "observation_only": True}]
    observed = monitor_compute.evaluate_strategy_phases(
        value, repo, SimpleNamespace(execute=execute, isolated=True))
    assert not observed["events"] and len(selected_reads) == 1
    assert observed["results"][SID]["rows"] == daily["rows"]
