"""当前60秒确认只改时钟, 启动分钟行情与冻结90秒定义保持。"""
from datetime import timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_late_session_strength import DAY, POOL, TARGET, bars, definition, evaluate


def test_current_default_waits_sixty_seconds_with_same_complete_input_results():
    from app.strategy.late_session_strength import DEFAULT_PARAMS, bar_time, evaluation_time

    current = definition(**DEFAULT_PARAMS)
    frozen = definition(**{**DEFAULT_PARAMS, "evaluation_delay_minutes": 1.5})
    assert evaluation_time(current, DAY) == TARGET + timedelta(seconds=60)
    assert bar_time(current, DAY) == bar_time(frozen, DAY) == TARGET
    later = bars().filter(pl.col("datetime") == TARGET).with_columns(
        (pl.col("datetime") + pl.duration(minutes=1)).alias("datetime"),
        pl.lit(5.).alias("close"))
    frame = pl.concat([bars(), later])
    assert not evaluate(frame=frame, sig=current,
                        completed_through=TARGET + timedelta(seconds=59)).symbols
    result = evaluate(frame=frame, sig=current,
                      completed_through=TARGET + timedelta(seconds=60))
    assert result == evaluate(frame=frame, sig=frozen,
                              completed_through=TARGET + timedelta(seconds=90))
    assert result.symbols == POOL[:3]
    assert result.pool_up_pct == 60
    assert frame.filter((pl.col("datetime") == bar_time(current, DAY))
                        & pl.col("symbol").is_in(result.symbols))["close"].to_list() == pytest.approx([10.159]*3)


def test_http_sixty_seconds_preserves_exact_start_and_all_other_values(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router
    from app.strategy import custom_signals
    from app.strategy.late_session_strength import bar_time, evaluation_time

    frozen = definition(time="14:30:20", evaluation_delay_minutes=1.5,
                        pool_up_min_pct=65, change_min_pct=0.6, change_max_pct=3.5,
                        ma5_1m_count=8, ma5_5m_count=3)
    custom_signals.save_one(tmp_path, frozen)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/custom-signals").json()["signals"] == [frozen]
    upgraded = {**frozen, "params": {**frozen["params"], "evaluation_delay_minutes": 1}}
    response = client.post("/api/custom-signals", json=upgraded)
    assert response.status_code == 200, response.text
    saved = response.json()["signal"]
    assert saved["params"] == upgraded["params"]
    assert evaluation_time(saved, DAY) == TARGET + timedelta(seconds=80)
    assert bar_time(saved, DAY) == TARGET
    assert evaluate(sig=saved, completed_through=TARGET+timedelta(seconds=79)).symbols == []
    assert client.get("/api/custom-signals").json()["signals"][0]["params"] == upgraded["params"]
    response = client.post("/api/custom-signals", json=frozen)
    assert response.status_code == 200, response.text
    assert response.json()["signal"]["params"] == frozen["params"]
