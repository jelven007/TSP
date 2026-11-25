"""Sparse daily qualification must not depend on first task arrival."""
import json

import pytest

from app.plugins.rustdx.routing import TaskRuntime


@pytest.mark.parametrize("first_group", ["realtime", "history"])
def test_sparse_pool_shares_qualified_binding_when_reference_is_unqualified(first_group):
    class Pool:
        def __init__(self, size, server, timeout, **options):
            self.server = server
            self.options = options

        def quotes_json(self, securities, **options):
            return json.dumps([{"code": securities[0][1], "price": 10.5}])

        def xdxr_json(self, *args):
            return "[]"

        def close(self):
            pass

    reference = "127.0.0.2:7709"
    qualified = "127.0.0.3:7709"
    runtime = TaskRuntime(Pool, connections=8, timeout=2, reference=reference,
                          ranked_nodes=[qualified])
    second_group = "history" if first_group == "realtime" else "realtime"
    try:
        runtime._get(first_group)
        assert runtime._get(second_group).server == qualified
        assert json.loads(runtime.quotes_json([(0, "000001")]))[0]["price"] == 10.5
        assert json.loads(runtime.xdxr_json(0, "000001")) == []
        assert runtime._bindings["minute"] == reference
        assert runtime.pools[second_group].options["task_group"] == second_group
    finally:
        runtime.close()


def test_shared_sparse_binding_uses_native_endpoint_quota():
    import tsp_rustdx_native as native
    from test_rustdx_native_pool import _Server

    server = _Server(idle_timeout=30)
    runtime = TaskRuntime(native.RustdxClient, connections=8, timeout=2,
                          reference="127.0.0.2:7709", ranked_nodes=[server.address])
    try:
        assert json.loads(runtime.xdxr_json(0, "000001")) == []
        assert json.loads(runtime.quotes_json([(0, "000001")]))[0]["code"] == "000001"
        stats = json.loads(runtime.stats_json())
        assert stats["bindings"] == {"minute": "127.0.0.2:7709",
                                    "history": server.address, "realtime": server.address}
        for group in ("history", "realtime"):
            quota = stats["task_groups"][group]["task_quota"]
            assert quota["active"] == 1
            assert quota["endpoints"]["endpoint-" + server.address.encode().hex()] == 2
        assert stats["network"]["active"] == 2
        assert server.peak <= 9
    finally:
        runtime.close()
        server.close()
