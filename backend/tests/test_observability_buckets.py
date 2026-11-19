"""Snapshots expose mergeable counts, including reset and process identity."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.services import observability as metrics


def test_http_histogram_roundtrips_and_can_be_differenced():
    metrics.reset_http_metrics()
    metrics.observe_http_duration(method="GET", route="/x", status_code=200, duration_ms=1)
    before = metrics.http_metrics_snapshot()
    metrics.observe_http_duration(method="GET", route="/x", status_code=200, duration_ms=70000)
    app = FastAPI()
    app.include_router(router)
    after = TestClient(app).get("/api/observability").json()
    first = before["totals"]["duration_ms"]["histogram"]
    last = after["totals"]["duration_ms"]["histogram"]
    assert first["bounds_ms"] == last["bounds_ms"]
    assert len(last["counts"]) == len(last["bounds_ms"]) + 1
    assert sum(last["counts"]) == 2
    delta = [b - a for a, b in zip(first["counts"], last["counts"], strict=True)]
    assert sum(delta) == 1 and delta[-1] == 1
    assert after["process"]["instance_id"] == before["process"]["instance_id"]
    metrics.reset_http_metrics()
    assert metrics.http_metrics_snapshot()["process"]["instance_id"] != before["process"]["instance_id"]


def test_runtime_snapshots_expose_independent_reset_identity():
    metrics.reset_runtime_metrics()
    metrics.observe_runtime_duration("monitor.compute", 3)
    app = FastAPI()
    app.include_router(router)
    before = TestClient(app).get("/api/observability").json()
    histogram = before["stages"]["monitor.compute"]["duration_ms"]["histogram"]
    assert sum(histogram["counts"]) == 1
    metrics.reset_runtime_metrics()
    after = TestClient(app).get("/api/observability").json()
    assert before["runtime_process"]["instance_id"] != after["runtime_process"]["instance_id"]
    assert "public_query_cache" in after
