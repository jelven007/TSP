"""HTTP 延迟分位数: 有界直方图估算 p50/p95/p99, 不改变既有 sum/max/average 契约。"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.services.observability import (
    http_metrics_snapshot,
    observe_http_duration,
    reset_http_metrics,
)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_http_metrics()
    yield
    reset_http_metrics()


def test_duration_percentiles_bracket_known_distribution() -> None:
    # 1..100 ms 均匀分布: p50≈50, p95≈95, p99≈99 (直方图估算允许桶宽误差)。
    for ms in range(1, 101):
        observe_http_duration(method="GET", route="/x", status_code=200, duration_ms=float(ms))

    duration = http_metrics_snapshot()["totals"]["duration_ms"]
    # 既有契约键必须保留。
    assert set(duration) >= {"sum", "max", "average", "p50", "p95", "p99"}
    assert duration["max"] == 100.0
    assert 40.0 <= duration["p50"] <= 60.0
    assert 88.0 <= duration["p95"] <= 100.0
    assert duration["p95"] <= duration["p99"] <= duration["max"]
    assert 92.0 <= duration["p99"] <= 100.0


def test_percentiles_are_null_without_samples() -> None:
    duration = http_metrics_snapshot()["totals"]["duration_ms"]
    assert duration["p50"] is None
    assert duration["p95"] is None
    assert duration["p99"] is None


def test_percentiles_are_per_route() -> None:
    for _ in range(50):
        observe_http_duration(method="GET", route="/fast", status_code=200, duration_ms=2.0)
    for _ in range(50):
        observe_http_duration(method="GET", route="/slow", status_code=200, duration_ms=800.0)

    routes = {item["route"]: item for item in http_metrics_snapshot()["routes"]}
    assert routes["/fast"]["duration_ms"]["p95"] < routes["/slow"]["duration_ms"]["p95"]
