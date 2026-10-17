"""Transport and execution boundary checks for full-product private strategies."""
import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

if not hasattr(socket, "AF_UNIX"):
    # The broker is a host-only Unix-socket service; Windows builds without
    # AF_UNIX cannot host it, and its module cannot even be imported there.
    pytest.skip("Unix domain sockets are unavailable on this platform", allow_module_level=True)

from app.cell.context import AccessError
from app.multitenancy.compute_broker import ComputeBroker
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
    with ComputeBroker(tmp_path / "compute.sock", "pinned-image:digest", public, 4) as broker:
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
