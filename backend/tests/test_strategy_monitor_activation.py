"""开启策略监控时重读目标配置, 不把展开的成员写回规则。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import monitor_rules as api
from app.api import strategy as strategy_api
from app.config import settings
from app.services import watchlist
from app.strategy import config, monitor_rules


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "multitenancy_enabled", False)
    engine = SimpleNamespace(get=lambda _: object(), validate_context=lambda *_: None)
    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    return tmp_path, request


def test_each_enabled_save_reloads_current_targets_and_keeps_rule_unexpanded(local, monkeypatch):
    data_dir, request = local
    _, group = watchlist.create_group("核心", symbols=["600000.SH"])
    config.save_override(data_dir, "demo", {"basic_filter": {
        "target_mode": "selected", "boards": [], "include_custom": True,
        "watchlist_group_ids": [group["id"]],
    }})
    load = config.load_override
    loaded = []

    def record(storage, sid):
        value = load(storage, sid)
        loaded.append(value["basic_filter"]["target_symbols"])
        return value

    monkeypatch.setattr(config, "load_override", record)
    rule = api.RuleModel(id="demo_monitor", name="策略监控", type="strategy",
                         scope="all", strategy_id="demo")
    api.save_rule(rule, request)
    assert loaded == [["600000.SH"]]
    api.save_rule(rule.model_copy(update={"enabled": False}), request)
    assert len(loaded) == 1
    watchlist.add("300750.SZ", group_id=group["id"])
    api.save_rule(rule, request)
    assert loaded == [["600000.SH"], ["300750.SZ", "600000.SH"]]
    saved = monitor_rules.load_one(data_dir, rule.id)
    assert saved["symbols"] == []
    assert "target_symbols" not in saved["basic_filter"]


def test_target_reload_failure_does_not_enable_monitor(local, monkeypatch):
    data_dir, request = local
    rule = api.RuleModel(id="demo_monitor", name="策略监控", type="strategy",
                         scope="all", strategy_id="demo", enabled=False)
    api.save_rule(rule, request)

    def fail(*_):
        raise OSError("target storage unavailable")

    monkeypatch.setattr(config, "load_override", fail)
    with pytest.raises(HTTPException) as error:
        api.save_rule(rule.model_copy(update={"enabled": True}), request)
    assert error.value.status_code == 503
    assert monitor_rules.load_one(data_dir, rule.id)["enabled"] is False
