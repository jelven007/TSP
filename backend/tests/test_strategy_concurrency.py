"""Global strategy concurrency, resource budgets and live configuration."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import settings as settings_api
from app.config import settings
from app.services import preferences, strategy_resources


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "service_role", "standalone")
    monkeypatch.setattr(settings, "multitenancy_enabled", False)
    monkeypatch.setattr(settings, "platform_config_dir", tmp_path)
    monkeypatch.setattr(strategy_resources, "_local_host", (65536, 32))
    preferences._invalidate_cache()
    yield tmp_path
    preferences._invalidate_cache()


@pytest.fixture
def broker():
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Host compute requires Unix sockets")
    from app.multitenancy.compute_broker import ComputeBroker

    with tempfile.TemporaryDirectory(prefix="tsp-cap-", dir="/tmp") as directory:
        root = Path(directory)
        config = root / "preferences.json"
        config.write_text("{}", encoding="utf-8")
        with ComputeBroker(
            root / "compute.sock", "pinned", root, 16, config_file=config,
            host_memory_mb=16384, host_cpus=32, memory_mb=1024, cpus=1,
        ) as value:
            yield value, config


def test_quarter_of_host_memory_is_for_all_strategy_jobs_not_system(broker):
    value, _ = broker
    status = value.resource_status()
    assert status["host_memory_mb"] == 16384
    assert status["memory_budget_mb"] == 4096
    assert status["cpu_budget"] == 8
    assert status["recommended_concurrency"] == 4
    assert status["effective_concurrency"] == 4
    assert status["strategy_concurrency"] is None


def test_manual_concurrency_is_live_and_shares_the_same_total_budget(broker):
    value, config = broker
    config.write_text('{"strategy_concurrency":8}', encoding="utf-8")
    status = value.resource_status()
    assert status["effective_concurrency"] == 8
    assert status["task_memory_mb"] == 512
    assert status["task_cpus"] == 1
    assert status["task_memory_mb"] * 8 <= status["memory_budget_mb"]
    assert status["task_cpus"] * 8 <= status["cpu_budget"]


def test_reducing_concurrency_does_not_replace_permits_or_kill_active_jobs(broker):
    value, config = broker
    permits = value.permits
    assert permits.acquire(blocking=False)
    assert permits.acquire(blocking=False)
    config.write_text('{"strategy_concurrency":1}', encoding="utf-8")
    assert not permits.acquire(blocking=False)
    permits.release()
    assert not permits.acquire(blocking=False)
    permits.release()
    assert permits.acquire(blocking=False)
    permits.release()
    assert value.permits is permits
    assert value.resource_status()["active"] == 0


def test_new_allocations_cannot_exceed_budget_while_old_jobs_finish(broker):
    value, config = broker
    assert value.permits.acquire(blocking=False)  # 1024 MiB
    config.write_text('{"strategy_concurrency":8}', encoding="utf-8")
    acquired = 0
    while value.permits.acquire(blocking=False):
        acquired += 1
    assert acquired == 6  # 1024 + 6 * 512 = 4096, not 8 new slots
    for _ in range(acquired + 1):
        value.permits.release()
    assert value.resource_status()["active"] == 0


def test_corrupt_configuration_preserves_last_valid_global_limit(broker):
    value, config = broker
    config.write_text('{"strategy_concurrency":2}', encoding="utf-8")
    assert value.resource_status()["effective_concurrency"] == 2
    config.write_text('{"strategy_concurrency":', encoding="utf-8")
    assert value.resource_status()["effective_concurrency"] == 2
    config.write_text('{"strategy_concurrency":true}', encoding="utf-8")
    assert value.resource_status()["effective_concurrency"] == 2


def test_saved_setting_survives_reload_without_overwriting_other_preferences(runtime):
    preferences.save({"screener_auto_run": False, "pipeline_pull_etf": False})
    req = settings_api.StrategyConcurrencyPrefs(strategy_concurrency=3)
    saved = settings_api.update_strategy_concurrency(req)
    assert saved["strategy_concurrency"] == 3
    preferences._invalidate_cache()
    actual = preferences.load()
    assert actual["strategy_concurrency"] == 3
    assert actual["screener_auto_run"] is False
    assert actual["pipeline_pull_etf"] is False
    settings_api.update_strategy_concurrency(
        settings_api.StrategyConcurrencyPrefs(strategy_concurrency=None),
    )
    assert preferences.load()["strategy_concurrency"] is None


@pytest.mark.parametrize("value", [0, 17, True, "2", 2.5])
def test_concurrency_setting_is_a_strict_bounded_integer_or_auto(value):
    with pytest.raises(ValidationError):
        settings_api.StrategyConcurrencyPrefs(strategy_concurrency=value)


def test_invalid_save_does_not_mutate_preferences(runtime, monkeypatch):
    preferences.save({"screener_auto_run": False})
    monkeypatch.setattr(
        "app.services.strategy_resources.local_status",
        lambda: {"max_concurrency": 2, "config_enabled": True},
    )
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        settings_api.update_strategy_concurrency(
            settings_api.StrategyConcurrencyPrefs(strategy_concurrency=3),
        )
    assert error.value.status_code == 422
    assert preferences.load() == {"screener_auto_run": False}


def test_resource_queries_never_accept_remote_configuration_overrides(broker):
    value, config = broker
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    try:
        from app.multitenancy.executor import ComputeExecutor
        status = ComputeExecutor(str(value.server_address)).resources()
        assert status["memory_budget_mb"] == 4096
        with pytest.raises(ValueError, match="unsupported"):
            ComputeExecutor(str(value.server_address)).execute({
                "kind": "configure", "strategy_concurrency": 16,
            })
        assert json.loads(config.read_text()) == {}
    finally:
        value.shutdown()
        thread.join(timeout=2)


def test_standalone_strategy_is_admitted_before_large_context_allocation(runtime, monkeypatch):
    from fastapi import HTTPException

    from app.api import screener
    from app.services import strategy_resources

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def latest_date(self):
            return "2026-10-25"

        def build_strategy_context(self, *args, **kwargs):
            assert strategy_resources.LOCAL_PERMITS.usage()["active"] == 1
            raise ValueError("stop-after-admission")

    engine = SimpleNamespace(has=lambda sid: True, get=lambda sid: SimpleNamespace(meta={}))
    monkeypatch.setattr(screener, "ScreenerService", Service)
    monkeypatch.setattr(screener, "_strategy_engine", lambda req: engine)
    monkeypatch.setattr(screener, "_private_data_dir", lambda req: runtime)
    monkeypatch.setattr(screener, "_load_ext_value_maps", lambda *args: {})
    monkeypatch.setattr(screener.strategy_config, "load_override", lambda *args: {})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))
    with pytest.raises(HTTPException) as error:
        screener.run_preset(screener.PresetRequest(strategy_id="test"), request)
    assert error.value.status_code == 400
    assert strategy_resources.LOCAL_PERMITS.usage()["active"] == 0


def test_local_corrupt_configuration_keeps_the_last_applied_limit(runtime):
    preferences.save({"strategy_concurrency": 2})
    assert settings_api.get_strategy_concurrency()["effective_concurrency"] == 2
    (runtime / "preferences.json").write_text('{"strategy_concurrency":', encoding="utf-8")
    status = settings_api.get_strategy_concurrency()
    assert status["strategy_concurrency"] == 2
    assert status["effective_concurrency"] == 2


def test_auto_and_restart_use_the_current_host_not_the_example_machine(broker):
    value, config = broker
    config.write_text('{"strategy_concurrency":8}', encoding="utf-8")
    assert value.resource_status()["effective_concurrency"] == 8
    config.write_text('{"strategy_concurrency":null}', encoding="utf-8")
    assert value.resource_status()["effective_concurrency"] == 4
    assert strategy_resources.ConcurrencyConfig(config).read() is None
    status = strategy_resources.budget_status(262144, 32, None)
    assert status["memory_budget_mb"] == 65536
    assert status["recommended_concurrency"] == 4  # CPU is the smaller budget.


def test_waiting_thread_wakes_after_old_allocation_is_released(broker):
    value, config = broker
    config.write_text('{"strategy_concurrency":1}', encoding="utf-8")
    assert value.permits.acquire(blocking=False)
    entered, acquired = threading.Event(), threading.Event()
    errors = []

    def wait():
        entered.set()
        try:
            if value.permits.acquire(timeout=1):
                acquired.set()
                value.permits.release()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        assert entered.wait(1)
        assert not acquired.wait(.05)
    finally:
        value.permits.release()
        thread.join(timeout=2)
    assert acquired.is_set() and not errors and not thread.is_alive()
    assert value.permits.usage() == {"active": 0, "reserved_memory_mb": 0, "reserved_cpus": 0}
    with pytest.raises(ValueError, match="no compute permit"):
        value.permits.release()


def test_fractional_cpu_allocations_and_docker_limits_never_round_up(broker):
    value, config = broker
    config.write_text('{"strategy_concurrency":3}', encoding="utf-8")
    value.host_cpus = 8
    for _ in range(3):
        assert value.permits.acquire(blocking=False)
    command = value.command("fractional")
    quota = float(command[command.index("--cpus") + 1])
    assert quota == .666666
    assert quota * 3 <= value.resource_status()["cpu_budget"]
    assert "POLARS_MAX_THREADS=1" in command
    assert not value.permits.acquire(blocking=False)
    for _ in range(3):
        value.permits.release()


def test_unavailable_host_budget_admits_no_jobs():
    status = strategy_resources.budget_status(0, 0, None)
    permits = strategy_resources.BudgetPermits(lambda: status)
    assert status["recommended_concurrency"] == status["effective_concurrency"] == 0
    assert not permits.acquire(timeout=.01)


def test_http_contract_persists_auto_and_rejects_invalid_body(runtime):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        for body in ({}, {"strategy_concurrency": True}, {"strategy_concurrency": 17},
                     {"strategy_concurrency": 2, "cpu_budget": 999}):
            assert client.post("/api/settings/strategy-concurrency", json=body).status_code == 422
        assert not (runtime / "preferences.json").exists()
        saved = client.post("/api/settings/strategy-concurrency", json={"strategy_concurrency": 3})
        assert saved.status_code == 200 and saved.json()["effective_concurrency"] == 3
        assert client.get("/api/settings/strategy-concurrency").json()["strategy_concurrency"] == 3
        auto = client.post("/api/settings/strategy-concurrency", json={"strategy_concurrency": None})
        assert auto.status_code == 200 and auto.json()["effective_concurrency"] == 4


@pytest.mark.parametrize("method", ["get", "post"])
def test_system_permission_is_required_before_platform_forwarding(runtime, method):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.multitenancy import access

    permissions, calls = [], []
    account = {"id": "user", "default_tenant_id": "private", "auth_version": 1,
               "permissions": permissions}

    class Platform:
        async def forward(self, request):
            calls.append(request.url.path)
            return JSONResponse({"forwarded": True})

    app = FastAPI()
    app.state.account_identity = SimpleNamespace(current_user=lambda cookie: account)
    app.state.platform_admin = Platform()
    app.middleware("http")(access.middleware)
    app.include_router(settings_api.router)
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        send = getattr(client, method)
        args = {"json": {"strategy_concurrency": 3}} if method == "post" else {}
        assert send("/api/settings/strategy-concurrency", **args).status_code == 403
        assert not calls
        permissions.append("settings:manage")
        assert send("/api/settings/strategy-concurrency", **args).json() == {"forwarded": True}
        assert calls == ["/api/settings/strategy-concurrency"]
        assert not (runtime / "preferences.json").exists()


def test_standalone_legacy_strategy_entrypoint_cannot_bypass_admission(runtime, monkeypatch):
    from app.api import strategy

    def engine(request):
        assert strategy_resources.LOCAL_PERMITS.usage()["active"] == 1
        raise ValueError("stop-after-admission")

    monkeypatch.setattr(strategy, "_get_engine", engine)
    with pytest.raises(ValueError, match="stop-after-admission"):
        strategy.run_strategy(strategy.RunRequest(strategy_id="test"), None)
    with pytest.raises(ValueError, match="stop-after-admission"):
        strategy.run_all(strategy.RunAllRequest(), None)
    assert strategy_resources.LOCAL_PERMITS.usage()["active"] == 0


def test_cancelled_broker_job_cleans_process_before_releasing_budget(broker, monkeypatch):
    import subprocess
    import sys

    from app.cell.context import AccessError
    from app.multitenancy.executor import ComputeExecutor

    value, _ = broker
    released, cancel = threading.Event(), threading.Event()
    cleanup_active = []
    original_release = value.permits.release

    def release():
        original_release()
        released.set()

    def cleanup(command, **kwargs):
        assert command[:3] == ["docker", "rm", "-f"]
        cleanup_active.append(value.permits.usage()["active"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(value.permits, "release", release)
    monkeypatch.setattr(subprocess, "run", cleanup)
    monkeypatch.setattr(value, "command", lambda *args: [
        sys.executable, "-u", "-c",
        'import sys,time; sys.stdin.readline(); '
        'print(\'{"type":"progress","payload":{"running":true}}\'); time.sleep(30)',
    ])
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(AccessError) as error:
            ComputeExecutor(str(value.server_address)).execute(
                {"kind": "strategy", "tenant_id": "cancelled"},
                progress=lambda payload: cancel.set(), cancel=cancel,
            )
        assert error.value.status == 409
        assert released.wait(3)
        assert cleanup_active == [1]
        assert value.resource_status()["active"] == 0
        assert value.active_tenants == set()
    finally:
        value.shutdown()
        thread.join(timeout=2)


def test_tenants_have_one_slot_while_administrators_share_the_global_budget(broker):
    value, _ = broker
    assert value.acquire_tenant("ordinary", administrator=False)
    assert not value.acquire_tenant("ordinary", administrator=False)
    assert value.acquire_tenant("admin", administrator=True)
    assert value.acquire_tenant("admin", administrator=True)
    value.release_tenant("admin")
    assert "admin" in value.active_tenants
    value.release_tenant("admin")
    value.release_tenant("ordinary")
    assert value.active_tenants == set()


def test_tenant_reads_a_fixed_limit_without_host_configuration(runtime):
    request = SimpleNamespace(state=SimpleNamespace(account={"permissions": ["business:read"]}))
    status = settings_api.get_strategy_concurrency(request)
    assert status == {"available": True, "editable": False, "strategy_concurrency": 1,
                      "effective_concurrency": 1, "recommended_concurrency": 1}


def test_trusted_platform_admin_role_reads_the_host_budget(runtime):
    request = SimpleNamespace(state=SimpleNamespace(account={"role": "admin", "email": ""}))
    status = settings_api.get_strategy_concurrency(request)
    assert status["recommended_concurrency"] == 4
    assert status["host_memory_mb"] == 65536
