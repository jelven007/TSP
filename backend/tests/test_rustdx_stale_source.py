"""A stale managed TCP must recover before inspecting its pagination identity."""
import json
import os
import socket

import pytest
from test_rustdx_native_pool import _Server


def _invalidate_business_socket(endpoint):
    """Shutdown this test's existing TCP, as a failed idle heartbeat does."""
    host, port = endpoint.split(":")
    for entry in os.listdir("/dev/fd"):
        try:
            duplicate = os.dup(int(entry))
        except OSError:
            continue
        try:
            candidate = socket.socket(fileno=duplicate)
        except OSError:
            os.close(duplicate)
            continue
        with candidate:
            try:
                if candidate.getpeername() == (host, int(port)):
                    candidate.shutdown(socket.SHUT_RDWR)
                    return
            except OSError:
                continue
    raise AssertionError("test business TCP not found")


@pytest.mark.skipif(os.name != "posix", reason="POSIX test socket fault injection")
@pytest.mark.parametrize("pinned", [False, True])
def test_stale_socket_is_replaced_before_reading_bar_source(pinned):
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(valid_bars=True)
    client = module.RustdxClient(1, server.address, 2)
    try:
        first = json.loads(client.bars_json(0, "000001", 8, 0, 800,
            with_request_count=True, with_source=True))
        assert first["error"] is None
        _invalidate_business_socket(server.address)
        second = json.loads(client.bars_json(0, "000001", 8, 800, 800,
            with_request_count=True, with_source=True,
            expected_source=json.dumps(first["source"]) if pinned else None))
        if pinned:
            assert "[SourceChanged]" in second["error"], second
            assert second["requests"] == 0 and second["rows"] == []
            second = json.loads(client.bars_json(0, "000001", 8, 0, 800,
                with_request_count=True, with_source=True))
        assert second["error"] is None, second
        assert second["source"]["endpoint"] == first["source"]["endpoint"]
        assert second["source"]["connection_id"] != first["source"]["connection_id"]
        assert json.loads(client.stats_json())["total_connections"] == 1
    finally:
        client.close()
        server.close()
