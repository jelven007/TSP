"""Scheduled monitoring uses the same saved name as the strategy catalog."""
from types import SimpleNamespace

import pytest

from app.api import monitor_rules as api
from app.api import strategy as strategy_api
from app.strategy.config import save_override
from app.strategy.monitor_rules import load_all


@pytest.mark.parametrize("saved_name,expected", [
    ("s05", "s05"), ("", "173004552 三分支"), (None, "173004552 三分支"),
])
def test_monitoring_uses_saved_display_name(tmp_path, monkeypatch, saved_name, expected):
    sid = "custom_mvxxcs05"
    if saved_name is not None:
        save_override(tmp_path, sid, {"name": saved_name})
    strategy = SimpleNamespace(meta={"id": sid, "name": "173004552 三分支"})
    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: SimpleNamespace(get=lambda _: strategy))
    monkeypatch.setattr(api, "_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(api, "_sync_engine", lambda _: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    state = api.set_strategy_monitoring(sid, api.StrategyMonitoringModel(enabled=True), request)
    rule, = load_all(tmp_path)
    assert state["enabled"]
    assert rule["strategy_id"] == sid
    assert rule["name"] == f"策略监控 · {expected}"
