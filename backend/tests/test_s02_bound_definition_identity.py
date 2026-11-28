"""Unrelated tenant catalog edits must not change a bound S02 input key."""
from copy import deepcopy
from datetime import datetime

import pytest
from test_strategy_input_manifest import request

from app.services import strategy_input_binding as binding
from app.services.strategy_input_manifest import common_key


def payload(day, version):
    req = request()
    sid = req["strategy_id"]
    return {
        "strategy_task_windows": {sid: {"buy": [{"start": "14:30:00", "signal": req["signal"]}]}},
        "daily_identities": {sid: {
            "strategy_version": version, "runtime_identity": "d" * 64,
            "params_json": "{}", "overrides_json": "{}", "asset_type": "stock", "timeframe": "1m",
        }},
        "daily_pool_snapshots": {sid: req["pool"]},
        "selection_snapshots": {sid: req["selection"]},
    }


@pytest.mark.parametrize("day", ["2026-11-26", "2026-11-27"])
def test_same_s02_definition_binds_across_unrelated_catalog_versions(day, monkeypatch):
    monkeypatch.setattr(binding, "cn_now", lambda: datetime.fromisoformat(day + "T14:30:40"))
    monkeypatch.setattr(binding, "algorithm_sha256", lambda: "b" * 64)
    monkeypatch.setattr("app.strategy.basic_filter.previous_session", lambda _: "2026-11-25")
    action = {"signal_id": "late_session_strength", "retry_until": day + "T14:55:00+08:00"}
    local = binding.input_request(payload(day, "a" * 64), binding.S02_ID, action)
    authority = binding.input_request(payload(day, "c" * 64), binding.S02_ID, action)
    assert local["definition_sha256"] == authority["definition_sha256"]
    assert common_key(local) == common_key(authority)


@pytest.mark.parametrize("field,value", [
    ("runtime_identity", "e" * 64), ("params_json", '{"threshold": 1}'),
    ("overrides_json", '{"basic_filter": {"price_min": 5}}'),
])
def test_real_s02_definition_change_still_changes_input_key(field, value, monkeypatch):
    day = "2026-11-27"
    monkeypatch.setattr(binding, "cn_now", lambda: datetime.fromisoformat(day + "T14:30:40"))
    monkeypatch.setattr(binding, "algorithm_sha256", lambda: "b" * 64)
    monkeypatch.setattr("app.strategy.basic_filter.previous_session", lambda _: "2026-11-26")
    action = {"signal_id": "late_session_strength", "retry_until": day + "T14:55:00+08:00"}
    original = payload(day, "a" * 64)
    changed = deepcopy(original)
    changed["daily_identities"][binding.S02_ID][field] = value
    assert common_key(binding.input_request(original, binding.S02_ID, action)) != common_key(
        binding.input_request(changed, binding.S02_ID, action))


@pytest.mark.parametrize("field", [
    "gap_max_pct", "position_min_pct", "return_5m_min_pct", "return_15m_min_pct",
])
def test_disabled_new_signal_option_matches_older_missing_default(field, monkeypatch):
    day = "2026-11-27"
    monkeypatch.setattr(binding, "cn_now", lambda: datetime.fromisoformat(day + "T14:30:40"))
    monkeypatch.setattr(binding, "algorithm_sha256", lambda: "b" * 64)
    monkeypatch.setattr("app.strategy.basic_filter.previous_session", lambda _: "2026-11-26")
    action = {"signal_id": "late_session_strength", "retry_until": day + "T14:55:00+08:00"}
    old = payload(day, "a" * 64)
    newer = deepcopy(old)
    newer["strategy_task_windows"][binding.S02_ID]["buy"][0]["signal"]["params"][field] = None
    assert common_key(binding.input_request(old, binding.S02_ID, action)) == common_key(
        binding.input_request(newer, binding.S02_ID, action))
    newer["strategy_task_windows"][binding.S02_ID]["buy"][0]["signal"]["params"][field] = 1
    assert common_key(binding.input_request(old, binding.S02_ID, action)) != common_key(
        binding.input_request(newer, binding.S02_ID, action))
