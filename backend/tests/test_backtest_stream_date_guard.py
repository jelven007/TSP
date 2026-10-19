"""回测 SSE 端点的 start/end 入参校验 — 非法日期返回 400 而不是 500。

signals.py 的 `/intraday/replay` 与 mining.py 的 `MiningRunRequest` 都把
`date.fromisoformat` 包在校验里, 非法日期给出 400/422; backtest 的三个 SSE
端点直接裸调 `date.fromisoformat`, 同样的入参会抛未捕获 ValueError。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router
from app.config import settings

# (路径, 该端点的必填参数)
STREAMS = [
    ("/api/backtest/strategy/stream", {"strategy_id": "ma_cross"}),
    ("/api/backtest/optimize/stream", {"strategy_id": "ma_cross", "param_grid": '{"n": [5]}'}),
    ("/api/backtest/walkforward/stream", {"strategy_id": "ma_cross", "param_grid": '{"n": [5]}'}),
]
STREAM_IDS = [path.rsplit("/", 2)[1] for path, _ in STREAMS]

BAD_DATES = ["not-a-date", "2026-13-01", "2026/09/04", ""]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # raise_server_exceptions=False: 未捕获异常表现为 500 响应, 与线上行为一致
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,extra", STREAMS, ids=STREAM_IDS)
@pytest.mark.parametrize("bad", BAD_DATES)
def test_malformed_end_returns_400(client, path, extra, bad):
    if not bad:
        pytest.skip("空串走默认值分支, 不属于非法日期")
    resp = client.get(path, params={**extra, "end": bad})
    assert resp.status_code == 400, resp.text
    assert "日期" in resp.json()["detail"]


@pytest.mark.parametrize("path,extra", STREAMS, ids=STREAM_IDS)
@pytest.mark.parametrize("bad", BAD_DATES)
def test_malformed_start_returns_400(client, path, extra, bad):
    if not bad:
        pytest.skip("空串走默认值分支, 不属于非法日期")
    resp = client.get(path, params={**extra, "start": bad, "end": "2026-09-04"})
    assert resp.status_code == 400, resp.text
    assert "日期" in resp.json()["detail"]


@pytest.mark.parametrize("path,extra", STREAMS, ids=STREAM_IDS)
def test_valid_dates_still_accepted(client, monkeypatch, path, extra):
    """合法日期必须照旧进入事件流, 证明这不是把入口收窄成"什么都不收"。

    打开服务端范围保护并给一个超阈值的窗口, 事件流会立刻以 error 事件收尾,
    既不触发真实回测, 又能证明日期解析已经放行。
    """
    monkeypatch.setattr(settings, "backtest_range_guard", True)
    params = {**extra, "start": "2020-01-01", "end": "2026-09-04"}
    if "walkforward" in path:
        # walkforward 的 guard 作用于单折窗口, 不是总区间
        params.update({"train_days": 400, "test_days": 400})
    resp = client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    assert "event: error" in resp.text


@pytest.mark.parametrize("extra", [
    {"window_unit": "weekdays"}, {"purge_days": -1},
    {"train_days": 0}, {"test_days": -1}, {"step_days": 0},
])
def test_walkforward_invalid_window_policy_returns_400(client, extra):
    resp = client.get("/api/backtest/walkforward/stream", params={
        "strategy_id": "s", "param_grid": '{"n":[1]}',
        "start": "2024-01-01", "end": "2024-12-31", **extra,
    })
    assert resp.status_code == 400


def test_walkforward_scoped_job_keeps_calendar_purge_costs_and_distinct_key(client, monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    from fastapi.responses import JSONResponse

    from app.api import strategy
    from app.multitenancy import research

    rows = []
    keys = []

    def submit(task, *, key):
        rows.append(task)
        keys.append(key)
        return task

    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr(research, "jobs_for", lambda request: SimpleNamespace(submit=submit))
    monkeypatch.setattr(research, "stream", lambda *args: JSONResponse({"ok": True}))
    monkeypatch.setattr(strategy, "_get_engine", lambda request: SimpleNamespace(
        task=lambda kind, config: {"kind": kind, "config": config},
    ))
    client.app.state.repo = SimpleNamespace(list_daily_dates=lambda *args: [
        date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9, 10, 11)
    ])
    params = {
        "strategy_id": "s", "param_grid": '{"n":[1]}',
        "start": "2024-01-02", "end": "2024-01-11",
        "train_days": 2, "test_days": 2, "step_days": 2,
        "purge_days": 1, "window_unit": "trading_days",
        "minimum_commission": 5, "transfer_fee_pct": 0.00001,
    }
    assert client.get("/api/backtest/walkforward/stream", params=params).status_code == 200
    config = rows[-1]["config"]
    assert config["window_unit"] == "trading_days"
    assert config["purge_days"] == 1
    assert config["backtest_kwargs"]["minimum_commission"] == 5
    assert config["backtest_kwargs"]["transfer_fee_pct"] == 0.00001
    for change in ({"purge_days": 0}, {"window_unit": "calendar_days"}):
        assert client.get("/api/backtest/walkforward/stream", params={**params, **change}).status_code == 200
    assert len(set(keys)) == 3
