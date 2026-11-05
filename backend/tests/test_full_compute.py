"""Transport and execution boundary checks for full-product private strategies."""
import json
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

if not hasattr(socket, "AF_UNIX"):
    # The broker is a host-only Unix-socket service; Windows builds without
    # AF_UNIX cannot host it, and its module cannot even be imported there.
    pytest.skip("Unix domain sockets are unavailable on this platform", allow_module_level=True)

from app.cell.context import AccessError
from app.multitenancy.compute_broker import STDERR_TAIL_BYTES, ComputeBroker, append_stderr_tail
from app.multitenancy.compute_worker import execute_request
from app.multitenancy.executor import ComputeExecutor
from app.multitenancy.strategies import definition_payload, metadata_definition
from app.strategy.engine import StrategyEngine


@pytest.fixture
def socket_dir():
    # AF_UNIX paths are limited to 104 bytes on macOS; pytest's normal
    # temporary path includes the long test name.
    with tempfile.TemporaryDirectory(prefix="tsp-ipc-", dir="/tmp") as directory:
        yield Path(directory)


def test_metadata_transfer_never_includes_callable_or_private_file_path():
    root = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    engine = StrategyEngine([root])
    for strategy in engine.strategy_definitions():
        payload = definition_payload(strategy)
        transferred = metadata_definition(json.loads(json.dumps(payload)))
        assert transferred.meta == strategy.meta
        assert transferred.required_features == strategy.required_features
        assert transferred.filter_fn is None and transferred.filter_history_fn is None
        assert transferred.matrix_strategy is None and transferred.file_path is None
        assert transferred.execution_backend == strategy.execution_backend


def test_broker_owns_image_entrypoint_and_only_mounts_public_market(socket_dir):
    tmp_path = socket_dir
    public = tmp_path / "public"
    public.mkdir()
    with ComputeBroker(
        tmp_path / "compute.sock", "pinned-image:digest", public, 4,
        host_memory_mb=65536, host_cpus=32,
    ) as broker:
        command = broker.command("tsp-test-job")
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "10001:10001"
    assert command[command.index("--memory") + 1] == "4g"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command.count("--mount") == 1
    assert command[command.index("--mount") + 1] == f"type=bind,src={public},dst=/market,readonly"
    assert command[command.index("pinned-image:digest") + 1] == "-i"
    assert "TICKFLOW_ENV_FILE=/dev/null" in command
    assert "HOME=/tmp" in command
    assert "XDG_CACHE_HOME=/tmp/xdg" in command
    assert not any("docker.sock" in part or "DSN" in part or "password" in part for part in command)
    assert not any(part.startswith("HOME=/root") for part in command)


def test_broker_small_host_budget_keeps_swap_and_memory_equal(socket_dir):
    with ComputeBroker(socket_dir / "small.sock", "pinned-image:digest", socket_dir, 1,
                       memory_mb=1536, cpus=1, queue_seconds=.25) as broker:
        command = broker.command("small-job")
        assert broker.queue_seconds == .25
        assert broker.permits.acquire(blocking=False)
        assert not broker.permits.acquire(blocking=False)
        broker.permits.release()
    assert command[command.index("--memory") + 1] == "1536m"
    assert command[command.index("--memory-swap") + 1] == "1536m"
    assert command[command.index("--cpus") + 1] == "1"
    assert "--network" in command and "--read-only" in command


@pytest.mark.parametrize("retired", [[], ["s01"], ["s01", "s02"]])
def test_broker_forwards_only_builtin_retirement_into_clean_sandbox(socket_dir, monkeypatch, retired):
    import app.multitenancy.compute_broker as module

    monkeypatch.setattr(module, "settings", SimpleNamespace(retired_builtin_strategies=retired), raising=False)
    monkeypatch.setenv("AUTH_PASSWORD", "must-remain-on-host")
    monkeypatch.setenv("TSP_CELL_DATA_DSN", "must-remain-on-host")
    with ComputeBroker(socket_dir / "retired.sock", "pinned-image:digest", socket_dir, 1) as broker:
        command = broker.command("retirement-job")
    environment = command[command.index("pinned-image:digest") + 2:]
    assert "RETIRED_BUILTIN_STRATEGIES=" + json.dumps(retired) in environment
    assert "TICKFLOW_ENV_FILE=/dev/null" in environment
    assert not any("must-remain-on-host" in value or "AUTH_PASSWORD=" in value
                   or "TSP_CELL_DATA_DSN=" in value for value in command)


def test_broker_health_runs_one_real_sandbox_probe_and_caches_it(socket_dir, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"type":"result","payload":{"ready":true}}\n',
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    with ComputeBroker(
        socket_dir / "health.sock", "pinned-image:digest", socket_dir, 1,
        health_cache_seconds=60,
    ) as broker:
        first = broker.health(socket_dir)
        second = broker.health(socket_dir)
    assert first == {"ready": True, "cached": False, "age_seconds": 0.0}
    assert second["ready"] is True and second["cached"] is True
    assert len(calls) == 1
    command, options = calls[0]
    assert command[:3] == ["docker", "run", "--rm"]
    assert options["input"] == b'{"kind":"health"}\n'
    assert options["timeout"] == 20


def test_compute_health_probe_avoids_loading_market_or_private_strategy_state(tmp_path):
    assert execute_request({"kind": "health"}, tmp_path) == {"ready": True}


def test_broker_stderr_tail_is_bounded_to_latest_diagnostics():
    tail = bytearray()
    append_stderr_tail(tail, b"old-" + b"x" * STDERR_TAIL_BYTES)
    append_stderr_tail(tail, b"-latest")
    assert len(tail) == STDERR_TAIL_BYTES
    assert tail.endswith(b"-latest")


@pytest.mark.parametrize("options", [
    {"capacity": 0}, {"capacity": 17}, {"memory_mb": 0},
    {"memory_mb": 65537}, {"cpus": 0}, {"cpus": float("nan")},
    {"queue_seconds": -1}, {"queue_seconds": 31},
])
def test_broker_rejects_invalid_host_budget_before_binding(socket_dir, options):
    values = {"capacity": 1, **options}
    with pytest.raises(ValueError, match="resource budget"):
        ComputeBroker(socket_dir / "invalid.sock", "pinned", socket_dir, **values)
    assert not (socket_dir / "invalid.sock").exists()


def test_executor_streams_progress_result_and_rejects_unknown_operations(socket_dir):
    tmp_path = socket_dir
    path = tmp_path / "compute.sock"
    events = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen()

        def respond():
            connection, _ = server.accept()
            with connection, connection.makefile("rb") as request:
                payload = json.loads(request.readline())
                assert payload["tenant_id"] == "tenant-a"
                connection.sendall(b'{"type":"progress","payload":{"step":1}}\n')
                connection.sendall(b'{"type":"heartbeat"}\n')
                connection.sendall(b'{"type":"result","payload":{"total":1}}\n')

        thread = threading.Thread(target=respond)
        thread.start()
        executor = ComputeExecutor(str(path))
        assert executor.execute({"kind": "strategy", "tenant_id": "tenant-a"}, events.append) == {"total": 1}
        thread.join(timeout=2)
    assert events == [{"step": 1}]
    with pytest.raises(ValueError, match="unsupported compute"):
        executor.execute({"kind": "shell", "command": "anything"})


def test_monitor_broker_returns_host_timings_after_cleanup(socket_dir, monkeypatch):
    import sys

    from app.services.observability import reset_runtime_metrics, runtime_metrics_snapshot

    cleanup = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *_args, **_kwargs: cleanup.append(True) or SimpleNamespace(returncode=0))
    reset_runtime_metrics()
    with ComputeBroker(socket_dir / "timing.sock", "pinned", socket_dir, 1) as broker:
        monkeypatch.setattr(broker, "command", lambda *_: [
            sys.executable, "-c",
            'import sys; sys.stdin.readline(); print(\'{"type":"result","payload":{"events":[]}}\')',
        ])
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        try:
            result = ComputeExecutor(str(socket_dir / "timing.sock")).execute({
                "kind": "monitor", "tenant_id": "tenant-a",
            })
            timing = result["_compute_timing"]
            assert timing["queued_at"] <= timing["started_at"] <= timing["finished_at"]
            assert timing["queue_ms"] >= 0 and timing["sandbox_ms"] >= 0
            assert cleanup == [True] and broker.permits.usage()["active"] == 0
            assert broker.active_tenants == set() and broker._tenant_counts == {}
            metrics = runtime_metrics_snapshot()
            assert metrics["compute.queue"]["calls"] == metrics["compute.sandbox"]["calls"] == 1
        finally:
            broker.shutdown()
            thread.join(2)


def test_broker_cli_emits_stage_info_without_external_logging_config(socket_dir):
    import os
    import sys

    code = """
import sys
from app.multitenancy import compute_broker as broker
class Server:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def serve_forever(self, **kwargs):
        broker.logger.info('compute job finished kind=monitor release=fixture queue_ms=1 sandbox_ms=2')
broker.ComputeBroker = Server
broker.os.chmod = lambda *_: None
sys.argv = ['broker', '--socket', sys.argv[1] + '/probe.sock',
            '--market-root', sys.argv[1], '--image', 'fixture']
broker.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(socket_dir)],
        env={**os.environ, "TICKFLOW_ENV_FILE": "/dev/null"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "compute job finished kind=monitor release=fixture" in result.stderr


def test_executor_cancellation_closes_transport_and_service_unavailability_is_503(socket_dir):
    tmp_path = socket_dir
    path = tmp_path / "compute.sock"
    disconnected = threading.Event()
    cancel = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen()

        def respond():
            connection, _ = server.accept()
            with connection, connection.makefile("rb") as request:
                request.readline()
                cancel.set()
                assert connection.recv(1) == b""
                disconnected.set()

        thread = threading.Thread(target=respond)
        thread.start()
        with pytest.raises(AccessError, match="已取消"):
            ComputeExecutor(str(path)).execute({"kind": "strategy", "tenant_id": "tenant-a"}, cancel=cancel)
        thread.join(timeout=3)
    assert disconnected.is_set()
    with pytest.raises(AccessError) as unavailable:
        ComputeExecutor(str(tmp_path / "absent.sock")).execute({"kind": "describe"})
    assert unavailable.value.status == 503


def test_read_only_repository_does_not_create_missing_market_generation(tmp_path):
    from app.enriched_generation import EnrichedGenerationUnavailableError
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path, read_only=True)
    try:
        repo = KlineRepository(store)
        with pytest.raises(EnrichedGenerationUnavailableError, match="marker is unavailable"):
            repo.get_matrix_data_generation()
        assert list(tmp_path.iterdir()) == []
    finally:
        store.db.close()


@pytest.mark.parametrize("ids", [["first"], ["first", "second"]])
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("empty_current", [False, True])
def test_compute_scan_keeps_holding_context_scoped_and_clock_explicit(
    tmp_path, monkeypatch, ids, live, empty_current,
):
    from datetime import date, datetime

    import polars as pl

    from app.multitenancy.compute_worker import execute
    from app.strategy.engine import StrategyDataContext, StrategyResult

    day = date(2026, 9, 7)
    now = datetime(2026, 9, 7, 10, 30, 7)
    shared = StrategyDataContext("stock", "1m", day, history=pl.DataFrame(),
                                 current=pl.DataFrame() if empty_current else None)
    captured = {}

    class Engine:
        def __init__(self, *_args, **_kwargs):
            pass

        def load_errors(self):
            return []

        def get(self, _sid):
            return SimpleNamespace(meta={})

        def run(self, sid, context, **_kwargs):
            captured[sid] = context
            return StrategyResult(strategy_id=sid, as_of=day)

        def run_all(self, *_args, **_kwargs):
            pytest.fail("holding snapshots require separate strategy contexts")

    # Load the backtest module before replacing the engine for this transport
    # test, so its module-level import cannot retain the temporary substitute.
    monkeypatch.setattr("app.backtest.strategy.StrategyEngine", StrategyEngine)
    monkeypatch.setattr("app.strategy.engine.StrategyEngine", Engine)
    monkeypatch.setattr("app.backtest.engine.BacktestEngine", lambda _repo: None)
    monkeypatch.setattr("app.tickflow.repository.DataStore", lambda *_args, **_kw:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr("app.tickflow.repository.KlineRepository", lambda _store: None)
    monkeypatch.setattr("app.services.screener.ScreenerService", lambda *_args, **_kw:
                        SimpleNamespace(build_strategy_context=lambda *_args, **_kw: shared))
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path:
                        iter(()) if str(path) == "/market" else original(path))
    monkeypatch.setattr("app.market_time.cn_today", lambda: day if live else date(2026, 11, 2))
    monkeypatch.setattr("app.market_time.cn_now", lambda: now)
    positions = [{"symbol": "600000.SH", "entry_date": "2026-09-04",
                  "qty": 100, "available_qty": 100}]
    response = execute({
        "kind": "strategy", "as_of": str(day), "timeframe": "1m", "strategy_ids": ids,
        "positions": {"first": positions}, "previous_states": {"first": {"previous": True}},
    }, tmp_path)
    assert set(response["results"]) == set(ids)
    first = captured["first"]
    assert first.positions == positions
    assert first.observed_at == (now if live else datetime(2026, 9, 7, 15))
    assert first.previous_states == {"first": {"previous": True}}
    if len(ids) > 1:
        assert captured["second"].positions is None
        assert captured["second"] is not first
        assert shared.positions is None and shared.observed_at is None
