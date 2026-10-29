"""Real sockets must obey task, endpoint and process quotas."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_rustdx_native_pool import _Server


def _wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_three_task_groups_hold_twenty_four_business_connections():
    import psutil
    import tsp_rustdx_native as native

    servers = [_Server(idle_timeout=30, probe_delay=0.15) for _ in range(3)]
    clients = [
        native.RustdxClient(
            8, server.address, 2, task_group=group, background_connections=6,
        )
        for group, server in zip(("realtime", "minute", "history"), servers, strict=True)
    ]
    securities = [(0, f"{index:06d}") for index in range(1_200)]
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda client: client.quotes_json(securities), clients))
        assert all(len(json.loads(result)) == len(securities) for result in results)
        _wait(lambda: sum(server.live for server in servers) == 24)
        assert all(server.live == 8 for server in servers)
        for client in clients:
            stats = json.loads(client.stats_json())
            assert stats["network"]["active"] == 24
            assert stats["network"]["peak"] <= 24
            assert stats["probes"]["peak"] <= 3
            assert stats["task_quota"]["active"] == 8
        endpoints = {tuple(server.listener.getsockname()) for server in servers}

        def sockets():
            # Reader/heartbeat clones share a TCP, but macOS reports every FD.
            unique = {(tuple(connection.laddr), tuple(connection.raddr))
                      for connection in psutil.Process().net_connections()
                      if connection.raddr and tuple(connection.raddr) in endpoints
                      and connection.status == psutil.CONN_ESTABLISHED}
            return [remote for _, remote in unique]

        # Exercise independent probes while all three business pools remain full.
        with ThreadPoolExecutor(max_workers=3) as executor:
            probes = [executor.submit(client.node_pool_json, True) for client in clients]
            _wait(lambda: len(sockets()) == 27)
            observed = [27]
            while any(not probe.done() for probe in probes):
                current = sockets()
                assert len(current) <= 27
                assert all(current.count(endpoint) <= 9 for endpoint in endpoints)
                assert json.loads(clients[0].stats_json())["network"]["active"] == 24
                observed.append(len(current))
                time.sleep(0.005)
            assert max(observed) == 27
            assert all(len(json.loads(probe.result())["valid_nodes"]) == 1 for probe in probes)
        assert json.loads(clients[0].stats_json())["probes"]["active"] == 0
        for client in clients:
            client.close()
        _wait(lambda: all(server.live == 0 for server in servers))
        assert json.loads(clients[0].stats_json())["network"]["active"] == 0
    finally:
        for client in clients:
            client.close()
        for server in servers:
            server.close()


def test_same_task_group_cannot_multiply_its_budget_with_another_pool():
    import tsp_rustdx_native as native

    server = _Server()
    first = native.RustdxClient(8, server.address, 2, task_group="minute")
    second = native.RustdxClient(1, server.address, 2, task_group="minute")
    securities = [(0, f"{index:06d}") for index in range(1_200)]
    try:
        first.quotes_json(securities)
        _wait(lambda: server.live == 8)
        with pytest.raises(RuntimeError, match=r"quota|TimedOut"):
            second.xdxr_json(0, "000001")
        assert json.loads(second.stats_json())["task_quota"]["active"] == 8
        assert server.peak <= 9
        first.close()
        assert json.loads(second.xdxr_json(0, "000001")) == []
    finally:
        first.close()
        second.close()
        server.close()


def test_distinct_groups_share_the_physical_endpoint_budget():
    import tsp_rustdx_native as native

    server = _Server()
    first = native.RustdxClient(8, server.address, 2, task_group="realtime")
    second = native.RustdxClient(1, server.address, 2, task_group="history")
    try:
        first.quotes_json([(0, f"{index:06d}") for index in range(1_200)])
        _wait(lambda: server.live == 8)
        with pytest.raises(RuntimeError, match=r"quota|TimedOut"):
            second.xdxr_json(0, "000001")
        assert server.peak <= 9
        first.close()
        assert json.loads(second.xdxr_json(0, "000001")) == []
    finally:
        first.close()
        second.close()
        server.close()


def test_group_failover_drains_all_old_sockets_and_metadata():
    import tsp_rustdx_native as native

    first, backup = _Server(valid_bars=True), _Server(valid_bars=True)
    client = native.RustdxClient(
        8, first.address, 2, task_group="minute", fallback_servers=[backup.address],
    )
    try:
        client.quotes_json([(0, f"{index:06d}") for index in range(1_200)])
        initial = json.loads(client.bars_json(0, "000001", 8, with_source=True))
        _wait(lambda: first.live == 8)
        first.incomplete = True
        assert json.loads(client.quotes_json([(0, "000001")]))[0]["code"] == "000001"
        _wait(lambda: first.live == 0)
        stats = json.loads(client.stats_json())
        assert stats["generation"] == 1
        assert stats["total_connections"] == 1
        assert all(row["source_endpoint"] == backup.address for row in stats["quote_metadata"])
        changed = json.loads(client.bars_json(
            0, "000001", 8, 800, with_source=True,
            expected_source=json.dumps(initial["source"]),
        ))
        assert "[SourceChanged]" in changed["error"]
        assert changed["requests"] == 0
    finally:
        client.close()
        first.close()
        backup.close()


def test_dropping_client_releases_connections_and_stops_pool():
    import gc

    import tsp_rustdx_native as native

    server = _Server()
    client = native.RustdxClient(1, server.address, 2, task_group="history")
    try:
        client.xdxr_json(0, "000001")
        _wait(lambda: server.live == 1)
        del client
        gc.collect()
        _wait(lambda: server.live == 0)
    finally:
        server.close()


def test_reference_read_reuses_minute_slots_when_configured_main_differs():
    import tsp_rustdx_native as native

    candidate, reference = _Server(valid_bars=True), _Server(valid_bars=True)
    client = native.RustdxClient(8, candidate.address, 2, task_group="minute")
    try:
        client.quotes_json([(0, f"{index:06d}") for index in range(1_200)])
        _wait(lambda: candidate.live == 8)
        result = json.loads(client.bars_json(
            0, "000001", 8, with_source=True, reference_server=reference.address,
        ))
        assert result["error"] is None
        assert result["source"]["endpoint"] == reference.address
        stats = json.loads(client.stats_json())
        assert stats["active_server"] == candidate.address
        assert stats["task_quota"]["active"] == 8
        assert stats["total_connections"] == 8
        second = json.loads(client.bars_json(
            0, "000001", 8, 800, with_source=True,
            expected_source=json.dumps(result["source"]), reference_server=reference.address,
        ))
        assert second["source"] == result["source"]
        _wait(lambda: candidate.live + reference.live == 8)
    finally:
        client.close()
        candidate.close()
        reference.close()


def test_shared_group_and_endpoint_quota_across_processes(tmp_path):
    import os
    import subprocess
    import sys

    servers = [_Server(idle_timeout=30) for _ in range(3)]
    directory = tmp_path / "permits"
    script = """
import json, sys
import tsp_rustdx_native as native
groups = ('realtime', 'minute', 'history')
clients = [native.RustdxClient(8, address, 2, task_group=group)
           for group, address in zip(groups, sys.argv[1:])]
for client in clients:
    client.quotes_json([(0, f'{i:06d}') for i in range(1200)])
print('READY', flush=True)
sys.stdin.readline()
for client in clients:
    client.close()
"""
    env = {**os.environ, "RUSTDX_NETWORK_SHARED_DIR": str(directory)}
    first = subprocess.Popen(
        [sys.executable, "-c", script, *(server.address for server in servers)],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    second = None
    try:
        assert first.stdout.readline().strip() == "READY"
        _wait(lambda: sum(server.live for server in servers) == 24)
        attempt = """
import sys
import tsp_rustdx_native as native
client = native.RustdxClient(1, sys.argv[1], 2, task_group='minute')
try:
    client.xdxr_json(0, '000001')
except RuntimeError as error:
    assert 'quota' in str(error) or 'TimedOut' in str(error), str(error)
    print('BOUNDED')
else:
    raise AssertionError('shared quota exceeded')
finally:
    client.close()
"""
        second = subprocess.run([sys.executable, "-c", attempt, servers[1].address],
                                env=env, capture_output=True, text=True, timeout=20)
        assert second.returncode == 0, second.stderr
        assert "BOUNDED" in second.stdout
        assert sum(server.live for server in servers) == 24
        first.stdin.write("close\n")
        first.stdin.flush()
        _, error = first.communicate(timeout=20)
        assert first.returncode == 0, error
        _wait(lambda: all(server.live == 0 for server in servers))
        # A fresh process can reacquire all permits released by process shutdown.
        recovered = subprocess.run(
            [sys.executable, "-c",
             "import sys,tsp_rustdx_native as n; c=n.RustdxClient(1,sys.argv[1],2,task_group='minute'); c.xdxr_json(0,'000001'); c.close()",
             servers[1].address], env=env, capture_output=True, text=True, timeout=20,
        )
        assert recovered.returncode == 0, recovered.stderr
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)
        for server in servers:
            server.close()


def test_three_independent_probe_slots_are_observable_and_close_cancels():
    import psutil
    import tsp_rustdx_native as native

    servers = [_Server(probe_delay=0.15) for _ in range(3)]
    clients = [native.RustdxClient(1, server.address, 2, task_group=group)
               for group, server in zip(("realtime", "minute", "history"), servers, strict=True)]
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(client.node_pool_json, False) for client in clients]
            _wait(lambda: json.loads(clients[0].stats_json())["probes"]["active"] == 3)
            endpoints = {tuple(server.listener.getsockname()) for server in servers}
            observed = []
            while any(not future.done() for future in futures):
                connections = [connection.raddr for connection in psutil.Process().net_connections()
                               if connection.raddr and tuple(connection.raddr) in endpoints
                               and connection.status == psutil.CONN_ESTABLISHED]
                assert len(connections) <= 3
                assert len(connections) == len(set(connections))
                observed.append(len(connections))
                time.sleep(0.005)
            assert max(observed) == 3
            assert all(len(json.loads(future.result())["valid_nodes"]) == 1 for future in futures)
        assert json.loads(clients[0].stats_json())["probes"]["active"] == 0
        servers[0].probe_delay = 2
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(clients[0].node_pool_json, True)
            _wait(lambda: json.loads(clients[0].stats_json())["probes"]["active"] == 1)
            started = time.monotonic()
            clients[0].close()
            with pytest.raises(RuntimeError, match=r"Interrupted|closed"):
                pending.result(timeout=5)
            assert time.monotonic() - started < 2
        _wait(lambda: json.loads(clients[1].stats_json())["probes"]["active"] == 0)
    finally:
        for client in clients:
            client.close()
        for server in servers:
            server.close()


@pytest.mark.parametrize("dimension", ["global", "group", "endpoint", "probes"])
def test_shared_capacity_mismatch_is_rejected_before_business_connect(tmp_path, dimension):
    import os
    import subprocess
    import sys

    server = _Server()
    directory = tmp_path / "permits"
    scoped = {
        "global": directory,
        "group": directory / "business" / "group-minute",
        "endpoint": directory / "business" / f"endpoint-{server.address.encode().hex()}",
        "probes": directory / "probes",
    }[dimension]
    scoped.mkdir(parents=True)
    (scoped / "capacity.lock").write_text("99")
    script = """
import sys, tsp_rustdx_native as native
client = None
try:
    client = native.RustdxClient(1, sys.argv[1], 2, task_group='minute')
    client.xdxr_json(0, '000001')
except RuntimeError as error:
    assert 'InvalidInput' in str(error), str(error)
    print('REJECTED')
else:
    raise AssertionError('different shared capacity accepted')
finally:
    if client is not None:
        client.close()
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, server.address],
            env={**os.environ, "RUSTDX_NETWORK_SHARED_DIR": str(directory)},
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert "REJECTED" in result.stdout
        assert server.batches == []
        assert "0f00" not in server.opcodes
    finally:
        server.close()
