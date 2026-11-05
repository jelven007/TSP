"""Catalogue responses preserve the metadata needed by the next-selection entry."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy as strategy_api
from app.strategy.engine import StrategyEngine


@pytest.mark.parametrize("flag", ["signals", None])
def test_catalogue_and_detail_preserve_signal_sections_for_the_real_ui(tmp_path, flag):
    folder = tmp_path / "strategies/custom"
    folder.mkdir(parents=True)
    meta = {
        "id": "custom_catalog", "name": "s02",
        "timeframes": ["1m"], "asset_types": ["stock"],
    }
    if flag:
        meta["result_sections"] = flag
    (folder / "catalog.py").write_text(
        "META=" + repr(meta) + "\nEXECUTION_BACKEND='minute_filter'\n"
        "def filter_minute_history(df, params):\n    return df.head(0)\n",
    )
    engine = StrategyEngine([folder])
    assert engine.has("custom_catalog")
    app = FastAPI()
    app.state.strategy_engine = engine
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(strategy_api.router)
    with TestClient(app) as client:
        for path in ("/api/strategies?timeframe=1m", "/api/strategies/custom_catalog"):
            response = client.get(path)
            assert response.status_code == 200
            data = response.json()
            detail = data["strategies"][0] if "strategies" in data else data
            assert detail.get("result_sections") == flag
            assert detail["minute_execution"] is None
