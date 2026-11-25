"""Opt-in AC-4: actual Worker, Broker, isolated Docker Compute and disposable PostgreSQL."""
# ruff: noqa: F401,F811 -- disposable database fixtures are imported deliberately.
import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from test_full_identity import env, full_cluster
from test_full_runtime import owned
from test_strategy_input_manifest import AT, SYMBOLS, build, request

from app.market_time import CN_TZ
from app.multitenancy.alerts import Alerts
from app.multitenancy.compute_broker import ComputeBroker
from app.multitenancy.executor import ComputeExecutor
from app.multitenancy.runtime import fingerprint
from app.services.strategy_input_binding import algorithm_sha256
from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy import custom_signals, version
from app.strategy.engine import StrategyEngine

pytestmark = pytest.mark.skipif(
    not os.environ.get("TSP_TEST_COMPUTE_IMAGE"), reason="requires a verified isolated candidate image")

# Clock and blocked IO exist only in this test command. Production Compute is unchanged.
FAULT_ENTRY = """
import io,json,sys,time
from datetime import datetime
def step(name):
    print(json.dumps({"type":"progress","payload":{"step":name}}),flush=True)
step("started")
raw=sys.stdin.buffer.readline()
step("input")
payload=json.loads(raw)
at=datetime.fromisoformat(payload["_test_clock"])
from app import market_time
step("market_time")
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):
        return datetime.fromtimestamp(at.timestamp(),tz)
market_time.datetime=Clock
from app.multitenancy import compute_worker,monitor_compute
step("compute_imported")
original=monitor_compute.evaluate_breadth_phase
def blocked(*args,**kwargs):
    compute_worker.emit("progress",payload={"blocked_seconds":90})
    time.sleep(90)
    return original(*args,**kwargs)
monitor_compute.evaluate_breadth_phase=blocked
sys.stdin=io.TextIOWrapper(io.BytesIO(raw))
compute_worker.main()
"""


def test_s02_stocks_commit_within_two_seconds_of_return_while_breadth_blocks_90_seconds(
        owned, tmp_path, monkeypatch):
    sid = "custom_mvxxcp88"
    now = (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ)
    owned.clock["now"] = now.timestamp()
    source = (Path(__file__).resolve().parents[2] / "examples/strategies/s02_atr14.py").read_text()
    saved = set_monitoring(owned.documents, sid, "s02", True)
    sig = {"id": "late_session_strength", "template": "late_session_strength",
           "name": "尾盘建仓", "kind": "entry", "timeframe": "intraday", "enabled": True,
           "conditions": [], "params": {**request()["signal"]["params"],
               "end_time": "14:55:00", "evaluation_delay_minutes": .5,
               "pool_up_min_pct": None, "change_min_pct": .8, "change_max_pct": 4.}}
    override = {"entry_signals": ["csgi_late_session_strength"], "exit_signals": []}
    owned.documents.put("strategy_sources", sid, {"id": sid, "source": "custom", "code": source})
    owned.documents.put("strategy_overrides", sid, override)
    custom_signals.save_one(owned.documents, sig)
    directory = tmp_path / "definitions"
    directory.mkdir()
    (directory / f"{sid}.py").write_text(source)
    custom_signals.save_one(directory, sig)
    engine = StrategyEngine([directory])
    engine._user_data_dir = lambda: directory
    identity = version.runtime_identity(engine.get(sid), override)
    record = ResultIdentity(sid, "s02", "same-source", "{}", json.dumps(override), "null", "stock", "1m",
                            runtime_identity=identity)
    req = request()
    req.update(signal=sig, algorithm_sha256=algorithm_sha256())
    req["selection"]["rows"] = [{**row, "name": row["symbol"]}
                                for row in req["selection"]["rows"]]
    frame = pl.DataFrame([
        {"symbol": symbol, "datetime": AT - timedelta(minutes=i),
         "open": 10.2 - i * .004, "high": 10.21 - i * .004,
         "low": 10.19 - i * .004, "close": 10.2 - i * .004}
        for symbol in SYMBOLS for i in range(40)])
    manifests = {phase: build(frame, phase=phase, value=req) for phase in ("stocks", "breadth")}
    original_prepare = owned.worker.prepare

    def prepared(*args):
        account, context, current, observed = original_prepare(*args)
        from app.services.strategy_monitor_tasks import task_windows
        windows = task_windows(engine, engine.get(sid), override)
        from app.services.strategy_monitor_state import independent_buy
        assert independent_buy(windows["buy"]), windows
        current.update(
            kind="monitor", strategy_task_ids=[sid], strategy_task_windows={sid: windows},
            strategy_task_plans={sid: {"actions": [
                {"kind": "buy", "signal_id": sig["id"], "phase": phase,
                 "scheduled_at": now.replace(second=30).isoformat(),
                 "retry_until": now.replace(minute=55, second=0).isoformat()}
                for phase in ("breadth", "stocks")]}},
            strategy_task_generations={sid: saved["generation"]},
            strategy_task_receipts={sid: task_receipt(owned.documents, sid, now.date())},
            daily_identities={sid: asdict(record)}, daily_pool_snapshots={sid: req["pool"]},
            selection_snapshots={sid: req["selection"]}, sell_rosters={sid: None},
            late_task_scopes={sid: sid + ":" + identity},
            sources=[{"id": sid, "source": "custom", "code": source}], overrides={sid: override},
            signals=[sig], factors=[], strategy_input_manifests={sid: manifests},
            s02_inputs_required=True, _test_clock=now.isoformat())
        current["rules"] = [{**rule, "notify_events": ["buy_signal", "sell_signal"]}
                            for rule in current["rules"] if rule.get("strategy_id") == sid]
        current["strategy_task_results"] = deepcopy(current.get("strategy_task_results") or {})
        with owned.documents.transaction(write=False) as unit:
            current["_configuration_fingerprint"] = fingerprint(unit.connection, include_state=False)
        return account, context, current, observed

    monkeypatch.setattr(owned.worker, "prepare", prepared)
    # Production's existing public root remains read-only inside the sandbox.
    public = Path(os.environ["TSP_TEST_COMPUTE_MARKET"])
    blocked, returned, progress = threading.Event(), {}, []

    class TracedExecutor(ComputeExecutor):
        def execute(self, payload, **kwargs):
            def observe(value):
                progress.append((payload["monitor_phase"], value))
                if value.get("blocked_seconds") == 90:
                    blocked.set()
            result = super().execute(payload, progress=observe, **kwargs)
            returned[payload["monitor_phase"]] = time.perf_counter()
            return result

    with tempfile.TemporaryDirectory(prefix="s02-fault-", dir="/tmp") as socket_directory, ComputeBroker(
                       Path(socket_directory) / "compute.sock", os.environ["TSP_TEST_COMPUTE_IMAGE"], public, 2,
                       memory_mb=2048, cpus=2, host_memory_mb=16384, host_cpus=8) as broker:
        original_command = broker.command

        def command(*args):
            command = original_command(*args)
            command[-3:] = [command[-3], "-c", FAULT_ENTRY]
            return command

        monkeypatch.setattr(broker, "command", command)
        server = threading.Thread(target=broker.serve_forever, daemon=True)
        server.start()
        owned.worker.executor = TracedExecutor(str(Path(socket_directory) / "compute.sock"))
        tenant = owned.documents.context.tenant_id
        try:
            started = time.perf_counter()
            owned.worker.evaluate(tenant, owned.snapshot())
            assert blocked.wait(25), {
                "jobs": {job.phase: job.future.result() if job.future.done() else "running"
                         for job in owned.worker.phase_inbox.jobs.values()},
                "resources": broker.resource_status(),
                "progress": progress,
                "receipt": task_receipt(owned.documents, sid, now.date())}
            deadline = started + 50
            while time.perf_counter() < deadline:
                owned.worker.evaluate(tenant, owned.snapshot())
                saved_receipt = task_receipt(owned.documents, sid, now.date())
                if saved_receipt.get("tasks", {}).get("buy", {}).get("terminal"):
                    break
                threading.Event().wait(.02)
            assert saved_receipt["tasks"]["buy"]["state"] == "success", saved_receipt
            assert "stocks" in returned and "breadth" not in returned
            assert time.perf_counter() - returned["stocks"] < 2
            archived = StrategyResults(owned.documents).read(record, str(now.date()))
            assert [row["symbol"] for row in archived["result"]["sections"]["buy"]] == SYMBOLS
            assert len([event for event in Alerts(owned.documents).list_recent()
                        if event.get("type") == "buy_signal" and event.get("strategy_id") == sid]) == 2
            evidence = {"stocks_returned_seconds": returned["stocks"] - started,
                        "commit_checked_seconds": time.perf_counter() - started,
                        "breadth_block_seconds": 90, "rows": SYMBOLS,
                        "input_sha256": manifests["stocks"]["input_sha256"]}
            print(json.dumps(evidence, sort_keys=True))
        finally:
            print(json.dumps({"sandbox_progress": progress}, sort_keys=True))
            # Cancels real containers; no 90-second sleep is performed by the Worker.
            owned.worker.close()
            broker.shutdown()
            server.join(2)
    assert broker.permits.usage()["active"] == 0 and not broker.active_tenants
