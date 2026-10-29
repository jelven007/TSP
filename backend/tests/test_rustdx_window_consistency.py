"""A TCP identity does not freeze the history offset or certify another node."""
import json
from datetime import datetime, timedelta

import pytest

from app.plugins.rustdx.client import RustdxError, RustdxSourceChangedError
from app.plugins.rustdx.provider import RustdxProvider


def rows():
    latest = datetime(2026, 10, 29, 14, 55)
    return [{"datetime": latest - timedelta(minutes=i),
             "open": 10., "high": 11., "low": 9., "close": 10.,
             "vol": 100., "amount": 1000.} for i in range(1600)]


@pytest.mark.parametrize("always", [False, True])
def test_same_session_anchor_change_restarts_whole_window(monkeypatch, always):
    from app.plugins.rustdx import provider as module

    monkeypatch.setattr(module, "cn_now", lambda: datetime(2026, 10, 29, 14, 59))

    class Moving:
        def __init__(self):
            self.data = rows()
            self.attempts = 0

        def bars_with_source(self, symbol, *, start, **options):
            if start == 0:
                self.attempts += 1
            return [{**row, "close": float(self.attempts)}
                    for row in self.data[start:start + 800]], {"connection_id": 1}

        def bar_anchor(self, symbol, *, frequency, expected_source):
            newest = {**self.data[0], "close": float(self.attempts)}
            if self.attempts == 1 or always:
                newest["datetime"] += timedelta(minutes=1)
            return newest

    client = Moving()
    provider = RustdxProvider()
    if always:
        with pytest.raises(RustdxSourceChangedError, match="锚点"):
            provider._bar_records(client, "000001.SZ", 8,
                                  client.data[-1]["datetime"], datetime(2026, 10, 29, 15))
    else:
        result = provider._bar_records(client, "000001.SZ", 8,
                                       client.data[-1]["datetime"], datetime(2026, 10, 29, 15))
        assert {row["close"] for row in result} == {2.}
    assert client.attempts == 2


def test_conflicting_duplicate_time_key_is_rejected():
    class Conflict:
        def bars(self, *args, **options):
            original = rows()[0]
            return [original, {**original, "close": 99.}]

    with pytest.raises(RustdxError, match="冲突"):
        RustdxProvider()._bar_records(
            Conflict(), "000001.SZ", 8, datetime(2026, 10, 1), datetime(2026, 10, 30),
        )


def test_identical_boundary_duplicate_preserves_single_record():
    class Overlap:
        def bars(self, *args, **options):
            original = rows()[0]
            return [original, dict(original)]

    result = RustdxProvider()._bar_records(
        Overlap(), "000001.SZ", 8, datetime(2026, 10, 1), datetime(2026, 10, 30),
    )
    assert len(result) == 1


@pytest.mark.parametrize("difference", ["price", "missing_key", "same"])
def test_unqualified_history_window_uses_complete_reference_on_conflict(difference):
    from app.plugins.rustdx.routing import TaskRuntime

    candidate = rows()[:5]
    reference = [dict(row) for row in candidate]
    if difference == "price":
        reference[2]["close"] = 99.
    elif difference == "missing_key":
        reference.pop(2)
    calls = []
    runtime = TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709")
    scope = ("000001.SZ", 8, candidate[-1]["datetime"], candidate[0]["datetime"])
    source = {"endpoint": "127.0.0.3:7709", "connection_id": 1, "observed_date": 20261029}

    def read_reference():
        calls.append(True)
        return reference

    result = runtime.validate_bar_window(
        candidate, scope=scope, source=source, reference_read=read_reference,
    )
    assert result == reference
    assert len(calls) == 1
    if difference == "same":
        runtime.validate_bar_window(
            candidate, scope=scope, source=source, reference_read=read_reference,
        )
        assert len(calls) == 1
        updated = [dict(row) for row in candidate]
        updated[0]["close"] = 77.
        runtime.validate_bar_window(
            updated, scope=scope, source=source, reference_read=read_reference,
        )
        assert len(calls) == 2


def test_reference_failure_does_not_certify_unchecked_candidate():
    from app.plugins.rustdx.routing import TaskRuntime

    runtime = TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709")
    def fail():
        raise RustdxError("参考窗口不可用")
    with pytest.raises(RustdxError, match="参考窗口"):
        runtime.validate_bar_window(
            rows(), scope=("000001.SZ", 8, None, None),
            source={"endpoint": "127.0.0.3:7709"}, reference_read=fail,
        )


def test_window_qualification_expires_and_does_not_cross_observation_day(monkeypatch):
    from app.plugins.rustdx import routing

    clock = [100.]
    monkeypatch.setattr(routing.time, "monotonic", lambda: clock[0])
    runtime = routing.TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709")
    candidate = rows()[:5]
    scope = ("000001.SZ", 8, candidate[-1]["datetime"], candidate[0]["datetime"])
    source = {"endpoint": "127.0.0.3:7709", "observed_date": 20261029, "generation": 1}
    calls = []

    def reference():
        calls.append(True)
        return candidate, 2

    for _ in range(2):
        runtime.validate_bar_window(candidate, scope=scope, source=source, reference_read=reference)
    assert len(calls) == 1
    clock[0] = 131.
    runtime.validate_bar_window(candidate, scope=scope, source=source, reference_read=reference)
    assert len(calls) == 2
    runtime.validate_bar_window(candidate, scope=scope, source={**source, "observed_date": 20261030},
                                reference_read=reference)
    assert len(calls) == 3
    assert json.loads(runtime.stats_json())["window_validation"]["reference_requests"] == 6
