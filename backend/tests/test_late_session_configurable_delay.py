"""Configurable 30-second multiples preserve frozen delays and launch data."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_late_session_strength import DAY, POOL, TARGET, definition, evaluate

from app.strategy import custom_signals
from app.strategy.late_session_strength import DEFAULT_PARAMS, bar_time, evaluation_time, parameters


@pytest.mark.parametrize("seconds", range(30, 901, 30))
def test_supported_seconds_use_exact_confirmation_and_same_completed_input(seconds):
    sig = definition(evaluation_delay_minutes=seconds / 60)
    assert parameters(sig)["evaluation_delay_minutes"] == seconds / 60
    assert evaluation_time(sig, DAY) == TARGET + timedelta(seconds=seconds)
    assert bar_time(sig, DAY) == TARGET
    assert not evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=seconds - 1)).symbols
    assert evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=seconds)).symbols == POOL[:3]


@pytest.mark.parametrize("delay", [-1, 0.25, 0.75, 15.5, True, "0.5", float("nan"), float("inf")])
def test_invalid_delays_are_rejected(delay):
    with pytest.raises(ValueError):
        parameters(definition(evaluation_delay_minutes=delay))


@pytest.mark.parametrize("seconds", [30, 60, 90, 120, 900])
def test_last_allowed_start_confirms_at_close_and_next_second_is_rejected(seconds):
    latest = datetime.combine(DAY, datetime.min.time()).replace(hour=15) - timedelta(seconds=seconds)
    sig = definition(time=latest.strftime("%H:%M:%S"), evaluation_delay_minutes=seconds / 60)
    assert evaluation_time(sig, DAY) == latest + timedelta(seconds=seconds)
    with pytest.raises(ValueError, match="启动时间"):
        parameters(definition(time=(latest + timedelta(seconds=1)).strftime("%H:%M:%S"),
                              evaluation_delay_minutes=seconds / 60))


def test_new_default_is_thirty_seconds_and_frozen_definitions_keep_their_clock():
    assert DEFAULT_PARAMS["evaluation_delay_minutes"] == 0.5
    assert evaluation_time(definition(), DAY) == TARGET
    assert evaluation_time(definition(evaluation_delay_minutes=0), DAY) == TARGET
    for minutes in (1, 1.5):
        assert evaluation_time(definition(evaluation_delay_minutes=minutes), DAY) == TARGET + timedelta(minutes=minutes)


def test_api_defaults_only_missing_new_delay_and_preserves_saved_seconds(tmp_path):
    from app.api.signals import router

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    client = TestClient(app)
    frozen = definition(evaluation_delay_minutes=1.5)
    custom_signals.save_one(tmp_path, frozen)
    assert client.get("/api/custom-signals").json()["signals"] == [frozen]
    for seconds in (30, 60, 90, 120, 900):
        request = definition(evaluation_delay_minutes=seconds / 60)
        response = client.post("/api/custom-signals", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["signal"]["params"] == request["params"]
        assert client.get("/api/custom-signals").json()["signals"][0]["params"] == request["params"]
    response = client.post("/api/custom-signals", json=definition())
    assert response.status_code == 200, response.text
    assert response.json()["signal"]["params"]["evaluation_delay_minutes"] == 0.5
    before = client.get("/api/custom-signals").json()
    response = client.post("/api/custom-signals", json=definition(evaluation_delay_minutes=0.75))
    assert response.status_code == 400
    assert client.get("/api/custom-signals").json() == before
