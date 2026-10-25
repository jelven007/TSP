"""分钟历史和盘中落盘共用选源, 旧全量分钟配置不改变路由。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api import settings as settings_api
from app.data_providers import custom
from app.data_providers.capabilities import build_capability_matrix
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.provider import GenericHTTPProvider
from app.services import preferences
from app.services.minute_refresh import MinuteRefreshService


def test_matrix_has_one_minute_route_and_does_not_promote_legacy_only_source(monkeypatch):
    monkeypatch.setattr(custom, "list_plugins", lambda: [])
    monkeypatch.setattr(custom, "list_sources", lambda: [
        {"name": "minute_source", "datasets": ["minute"]},
        {"name": "legacy_source", "datasets": ["full_minute"]},
    ])
    matrix = build_capability_matrix({
        "minute_data_provider": "minute_source",
        "full_minute_data_provider": "legacy_source",
    })
    rows = {row["id"]: row for row in matrix["capabilities"]}
    assert len(rows) == 7
    assert "full_minute" not in rows
    assert rows["minute"]["usable"]
    assert [source["name"] for source in rows["minute"]["candidates"]] == ["minute_source"]


@pytest.mark.parametrize("saved_minute", [None, "tickflow", "rustdx"])
def test_old_preference_is_read_only_alias_and_preserves_refresh_settings(
    tmp_path, monkeypatch, saved_minute,
):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    saved = {
        "full_minute_data_provider": "tickflow",
        "minute_refresh_enabled": False,
        "minute_refresh_interval": 60,
    }
    if saved_minute:
        saved["minute_data_provider"] = saved_minute
    preferences.save(saved)
    original = path.read_bytes()
    assert preferences.get_full_minute_data_provider() == (saved_minute or "rustdx")
    assert preferences.get_minute_refresh_enabled() is False
    assert preferences.get_minute_refresh_interval() == 60
    assert path.read_bytes() == original


def test_old_api_write_cannot_set_an_independent_source(monkeypatch):
    saved = Mock()
    monkeypatch.setattr(preferences, "save", saved)
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: None)
    monkeypatch.setattr("app.services.provider_audit.audit_provider_routes", lambda: {"status": "ok"})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    settings_api.update_data_providers(
        settings_api.DataProvidersIn(full_minute_data_provider="tickflow"), request,
    )
    saved.assert_not_called()


def test_api_minute_write_returns_same_legacy_alias_without_saving_old_field(monkeypatch):
    saved = Mock()
    monkeypatch.setattr(preferences, "save", saved)
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "rustdx")
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: None)
    monkeypatch.setattr("app.services.provider_audit.audit_provider_routes", lambda: {"status": "ok"})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    result = settings_api.update_data_providers(settings_api.DataProvidersIn(
        minute_data_provider="rustdx", full_minute_data_provider="tickflow",
    ), request)
    saved.assert_called_once_with({"minute_data_provider": "rustdx"})
    assert result["minute_data_provider"] == result["full_minute_data_provider"] == "rustdx"


def test_background_route_uses_minute_even_when_old_source_conflicts(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {
        "minute_data_provider": "rustdx", "full_minute_data_provider": "tickflow",
    })
    provider = object()
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: (
        name == "rustdx" and dataset == "minute"
    ))
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    service = MinuteRefreshService(SimpleNamespace())
    assert service.active_provider() == "rustdx"
    assert service._resolve_custom() == (provider, "rustdx")
    assert service.capability_ok()


def test_yaml_repair_round_uses_minute_endpoint_even_with_legacy_endpoint(monkeypatch):
    minute = DatasetConfig(
        url="https://example.test/minute",
        field_map={key: key for key in (
            "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
        )},
    )
    legacy = DatasetConfig(url="https://example.test/retired")
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="source", display_name="Source",
        datasets={"minute": minute, "full_minute": legacy},
    ))
    requested = []
    monkeypatch.setattr(provider, "_request_rows", lambda cfg, **kwargs: (
        requested.append(cfg.url) or []
    ))
    try:
        assert provider.get_intraday_batch(["600000.SH"]).is_empty()
        assert requested == [minute.url]
    finally:
        provider.close()
