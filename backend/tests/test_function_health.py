"""Background failures must remain visible without withdrawing usable market reads."""
from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.multitenancy.health import readiness


@pytest.fixture
def healthy_state(tmp_path):
    workers = [
        {"kind": kind, "healthy": True, "age_seconds": 0}
        for kind in ("research", "realtime", "notifications", "scheduled")
    ]
    failed_database = []

    @contextmanager
    def connection():
        if failed_database:
            raise OSError("database unavailable")
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(
            fetchall=lambda: workers, fetchone=lambda: {"value": 1},
        ))

    db = SimpleNamespace(
        settings=SimpleNamespace(id="full"),
        control=SimpleNamespace(connection=connection),
        data=SimpleNamespace(connection=connection),
    )
    (tmp_path / ".public_release.json").write_text('{"id":"release-1"}')
    state = SimpleNamespace(
        account_identity=SimpleNamespace(db=db),
        quote_service=SimpleNamespace(
            snapshot={"platform_ready": True}, received_at=time.monotonic(),
            bridge_status=lambda: {"connected": True},
        ),
        indicators_ready=True,
        compute_executor=SimpleNamespace(health=lambda: {"ready": True}),
    )
    return state, workers, failed_database


@pytest.mark.parametrize(("dependency", "unavailable"), [
    ("research", {"research"}),
    ("realtime", {"monitoring", "paper_trading"}),
    ("notifications", {"notifications"}),
    ("scheduled", {"scheduled", "paper_trading"}),
    ("compute", {"research"}),
    ("indicators", {"research", "monitoring", "paper_trading"}),
])
def test_background_failure_only_changes_dependent_functions(
    tmp_path, healthy_state, dependency, unavailable,
):
    state, workers, _ = healthy_state
    if dependency == "compute":
        state.compute_executor.health = lambda: {"ready": False}
    elif dependency == "indicators":
        state.indicators_ready = False
    else:
        next(row for row in workers if row["kind"] == dependency)["healthy"] = False

    result = readiness(state, tmp_path)

    assert result["ready"] is True
    assert result["status"] == "degraded"
    assert result["functions"]["market"]["ready"] is True
    assert {name for name, value in result["functions"].items() if not value["ready"]} == unavailable
    assert any(check["status"] == "error" for check in result["checks"].values())


@pytest.mark.parametrize("dependency", ["database", "public_files", "platform"])
def test_core_dependency_failure_still_blocks_market_and_trading(
    tmp_path, healthy_state, dependency,
):
    state, _, failed_database = healthy_state
    if dependency == "database":
        failed_database.append(True)
    elif dependency == "public_files":
        (tmp_path / ".public_release.json").write_text("broken")
    else:
        state.quote_service.snapshot["platform_ready"] = False

    result = readiness(state, tmp_path)

    assert result["ready"] is False
    assert result["status"] == "error"
    assert result["functions"]["market"]["ready"] is False
    assert result["functions"]["paper_trading"]["ready"] is False


def test_http_readiness_preserves_background_diagnostics_and_200_for_market(
    tmp_path, healthy_state, monkeypatch,
):
    state, workers, _ = healthy_state
    workers[0]["healthy"] = False
    monkeypatch.setattr(routes, "settings", SimpleNamespace(
        multitenancy_enabled=True, service_role="api", data_dir=tmp_path,
    ))
    app = FastAPI()
    app.include_router(routes.router)
    for name, value in vars(state).items():
        setattr(app.state, name, value)
    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["research_worker"]["status"] == "error"
    assert response.json()["functions"]["research"]["ready"] is False
