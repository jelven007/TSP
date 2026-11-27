"""Current S05 API saves never recreate the removed dedicated configuration."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.signals import router
from app.strategy.late_session_strength import DEFAULT_PARAMS


@pytest.mark.parametrize("branch", ["tail", "low_gap", "board"])
@pytest.mark.parametrize("stale_config", [False, True])
def test_common_api_save_removes_dedicated_config_and_preserves_shared_values(
    tmp_path, branch, stale_config,
):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), clear_cache=lambda: None)
    app.include_router(router)
    client = TestClient(app)
    params = {**deepcopy(DEFAULT_PARAMS), "return_5m_min_pct": -10.,
              "return_15m_min_pct": 100., "position_min_pct": .1, "gap_max_pct": 100.}
    if stale_config:
        params["s05_entry"] = {"sort_by": "four_dimension"}
    signal = {"id": branch + "_entry_s05", "name": branch, "kind": "entry",
              "timeframe": "intraday", "template": "late_session_strength", "params": params}
    response = client.post("/api/custom-signals", json=signal)
    assert response.status_code == 200, response.text
    stored = response.json()["signal"]
    assert "s05_entry" not in stored["params"]
    assert stored["params"] == {k: v for k, v in params.items() if k != "s05_entry"}
    assert client.get("/api/custom-signals").json()["signals"] == [stored]
