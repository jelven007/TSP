"""Saving a strategy monitor must validate its declared execution timeframe."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import monitor_rules as api
from app.api import strategy as strategy_api
from app.strategy import monitor_rules
from app.strategy.engine import StrategyEngine


@pytest.fixture
def local(tmp_path, monkeypatch):
    engine = StrategyEngine([Path(__file__).parents[1] / "app/strategy/builtin"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    contexts = []
    validate = engine.validate_context

    def record(strategy, context):
        contexts.append(context)
        validate(strategy, context)

    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "validate_context", record)
    return tmp_path, request, engine, contexts


@pytest.mark.parametrize("sid", ["s02", "ma_golden_cross"])
def test_create_edit_disable_and_reenable_use_the_declared_cycle(local, sid):
    storage, request, engine, contexts = local
    rule = api.RuleModel(id=f"{sid}_monitor", name="monitor", type="strategy",
                         strategy_id=sid, scope="all")
    for enabled in (True, True, False, True):
        result = api.save_rule(rule.model_copy(update={"enabled": enabled}), request)
        assert result["ok"] is True
        saved = monitor_rules.load_one(storage, rule.id)
        assert saved["enabled"] is enabled
        assert saved["strategy_id"] == sid
        assert saved["symbols"] == []
        assert contexts[-1].timeframe == engine.get(sid).meta["timeframes"][0]
    assert len(contexts) == 4


def test_monitor_asset_validation_is_not_bypassed(local):
    storage, request, _engine, _contexts = local
    rule = api.RuleModel(id="wrong_asset", name="monitor", type="strategy",
                         strategy_id="s02", asset_type="index", scope="all")
    with pytest.raises(HTTPException) as error:
        api.save_rule(rule, request)
    assert error.value.status_code == 400
    assert monitor_rules.load_one(storage, rule.id) is None


def test_undeclared_cycle_keeps_the_daily_default(local):
    storage, request, engine, contexts = local
    engine.get("ma_golden_cross").meta.pop("timeframes")
    rule = api.RuleModel(id="legacy_monitor", name="monitor", type="strategy",
                         strategy_id="ma_golden_cross", scope="all")
    assert api.save_rule(rule, request)["ok"] is True
    assert contexts[-1].timeframe == "1d"
    assert monitor_rules.load_one(storage, rule.id)["enabled"] is True
