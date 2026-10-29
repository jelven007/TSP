"""Task routing keeps ranked candidates and reference configuration stable."""
import json

from app.plugins.rustdx import client


def test_nine_ranked_nodes_are_partitioned_and_healthy_binding_is_sticky():
    from app.plugins.rustdx.routing import assign_candidates

    nodes = [f"127.0.0.{i}:7709" for i in range(1, 10)]
    candidates = assign_candidates(nodes)
    assert candidates == {
        "realtime": nodes[0::3], "minute": nodes[1::3], "history": nodes[2::3],
    }
    reordered = assign_candidates(list(reversed(nodes)), bindings={
        "realtime": nodes[0], "minute": nodes[1], "history": nodes[2],
    })
    assert [reordered[group][0] for group in candidates] == nodes[:3]


def test_task_operations_use_separate_pools_and_minute_foreground():
    from app.plugins.rustdx.routing import TaskRuntime

    class Pool:
        max_connections = 8

        def __init__(self, _size, server, _timeout, **options):
            self.group = options["task_group"]
            self.server = server
            self.calls = []

        def quotes_json(self, securities, **options):
            self.calls.append(("quotes", options))
            return "[]"

        def bars_json(self, *args, **options):
            self.calls.append(("bars", options))
            return "[]"

        def xdxr_json(self, *args):
            self.calls.append(("xdxr", {}))
            return "[]"

        def set_background_connections(self, limit):
            self.limit = limit

        def close(self):
            pass

    runtime = TaskRuntime(Pool, connections=8, timeout=2, reference="127.0.0.2:7709",
                          ranked_nodes=[f"127.0.0.{i}:7709" for i in range(1, 10)])
    try:
        runtime.quotes_json([(0, "000001")], priority=True)
        runtime.bars_json(0, "000001", 8, 0, 3, False, background=False)
        runtime.bars_json(0, "000001", 9, 0, 3, False)
        runtime.xdxr_json(0, "000001")
        assert set(runtime.pools) == {"realtime", "minute", "history"}
        assert runtime.pools["minute"].server == "127.0.0.2:7709"
        assert runtime.pools["minute"].calls[0][1]["background"] is False
        assert runtime.pools["realtime"].calls[0][1]["priority"] is True
        assert runtime.pools["history"].calls[-1][0] == "xdxr"
    finally:
        runtime.close()


def test_facade_marks_minute_increment_without_requiring_fake_new_keywords(monkeypatch):
    class Fake:
        def bars_json(self, *args, **options):
            self.options = options
            return json.dumps({"rows": [], "requests": 1, "error": None})

    fake = Fake()
    monkeypatch.setattr(client, "_native_client", lambda: fake)
    assert client.RustdxClient().incremental_bars_with_count(
        "000001.SZ", frequency=8, offset=3,
    ) == ([], 1)
    assert "background" not in fake.options


def test_minute_background_keeps_explicit_smaller_limit_after_session(monkeypatch):
    from app.plugins.rustdx import routing

    runtime = routing.TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709",
                                  background_connections=3)
    monkeypatch.setattr(routing, "_during_session", lambda: True)
    assert runtime._minute_limit() == 3
    monkeypatch.setattr(routing, "_during_session", lambda: False)
    assert runtime._minute_limit() == 3


def test_default_minute_background_reserves_two_then_reaches_pool_size(monkeypatch):
    from app.plugins.rustdx import routing

    runtime = routing.TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709")
    monkeypatch.setattr(routing, "_during_session", lambda: True)
    assert runtime._minute_limit() == 6
    monkeypatch.setattr(routing, "_during_session", lambda: False)
    assert runtime._minute_limit() == 8


def test_smaller_minute_pool_keeps_one_foreground_slot(monkeypatch):
    from app.plugins.rustdx import routing

    runtime = routing.TaskRuntime(None, connections=2, timeout=2, reference="127.0.0.2:7709")
    monkeypatch.setattr(routing, "_during_session", lambda: True)
    assert runtime._minute_limit() == 1
    monkeypatch.setattr(routing, "_during_session", lambda: False)
    assert runtime._minute_limit() == 2


def test_history_keeps_explicit_background_limit():
    from app.plugins.rustdx.routing import TaskRuntime

    class Pool:
        def __init__(self, _size, _server, _timeout, **options):
            self.background = options["background_connections"]

        def close(self):
            pass

    runtime = TaskRuntime(Pool, connections=8, timeout=2, reference="127.0.0.2:7709",
                          ranked_nodes=[f"127.0.0.{i}:7709" for i in range(1, 10)],
                          background_connections=3)
    try:
        assert runtime._get("history").background == 3
    finally:
        runtime.close()
