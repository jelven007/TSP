"""A portable defaults pack must preserve the full factor catalog and user changes."""
import hashlib
import json
from dataclasses import asdict

import pytest

from app.multitenancy.business_defaults import canonical


def write_bundle(root, *, extension=True, schedules=None):
    root.mkdir(exist_ok=True)
    config = {
        "id": "ext_flow", "label": "资金", "mode": "timeseries",
        "fields": [{"name": "net", "dtype": "float", "label": "净额"}],
    }
    catalog = []
    files = {}
    if extension:
        from app.factors.ext_factors import ext_factor_specs
        from app.services.ext_data import ExtConfig

        path = root / "ext_data/ext_flow/config.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(canonical(ExtConfig.from_dict(config).to_dict()))
        files["ext_data/ext_flow/config.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
        catalog = json.loads(json.dumps([asdict(row) for row in ext_factor_specs(root)],
                            default=lambda value: sorted(value) if isinstance(value, frozenset) else value))
    payload = {
        "target_username": "admin", "source": {}, "documents": [], "monitor_rules": [],
        "factor_catalog": catalog, "code_hashes": {}, "backtest": {},
    }
    if schedules is not None:
        payload["schedules"] = schedules
    snapshot = hashlib.sha256(canonical(payload)).hexdigest()
    (root / "manifest.json").write_bytes(canonical({
        "format_version": 2 if schedules is not None else 1, "snapshot_id": snapshot, "payload": payload,
    }))
    files["manifest.json"] = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    (root / "bundle.json").write_bytes(canonical({
        "format_version": 1, "snapshot_id": snapshot, "files": files,
    }))
    return config


def test_portable_bundle_keeps_extension_factor_identity(tmp_path):
    from app.multitenancy.business_defaults_bundle import load_bundle

    write_bundle(tmp_path)
    snapshot, configs = load_bundle(tmp_path)
    assert snapshot.payload.factor_catalog[0]["id"] == "ext_ext_flow_net"
    assert set(configs) == {"ext_flow"}


@pytest.mark.parametrize("change", ["modified", "missing", "traversal", "symlink", "catalog"])
def test_invalid_bundle_is_rejected_before_install(tmp_path, change):
    from app.multitenancy.business_defaults_bundle import install_extensions

    pack = tmp_path / "pack"
    write_bundle(pack)
    path = pack / "ext_data/ext_flow/config.json"
    index = json.loads((pack / "bundle.json").read_text())
    if change == "modified":
        path.write_text("{}")
    elif change == "missing":
        path.unlink()
    elif change == "traversal":
        index["files"]["../outside.json"] = "0" * 64
    elif change == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    else:
        index["files"].pop("ext_data/ext_flow/config.json")
        path.unlink()
    (pack / "bundle.json").write_bytes(canonical(index))
    target = tmp_path / "target"
    with pytest.raises(ValueError):
        install_extensions(pack, target, apply=True)
    assert not target.exists()


def test_preview_and_install_do_not_replace_saved_configuration(tmp_path):
    from app.multitenancy.business_defaults_bundle import install_extensions

    pack, target = tmp_path / "pack", tmp_path / "target"
    write_bundle(pack)
    preview = install_extensions(pack, target)
    assert preview["creates"] == ["ext_flow"]
    assert not target.exists()
    applied = install_extensions(pack, target, apply=True)
    assert applied["creates"] == ["ext_flow"]
    path = target / "ext_data/ext_flow/config.json"
    path.write_text('{"user_saved":true}')
    previous = path.read_bytes()
    repeated = install_extensions(pack, target, apply=True)
    assert repeated["preserved"] == ["ext_flow"]
    assert path.read_bytes() == previous


def test_bundle_without_extensions_remains_supported(tmp_path):
    from app.multitenancy.business_defaults_bundle import load_bundle

    write_bundle(tmp_path, extension=False)
    assert load_bundle(tmp_path)[1] == {}


def schedules(platform):
    return {"platform": platform, "review_push_mode": "manual",
            "review": {"enabled": False, "hour": 15, "minute": 40},
            "mining": {"mining_schedule_enabled": False, "mining_schedule_weekday": 4,
                       "mining_budget_profile": "balanced"}}


def test_platform_defaults_preserve_saved_switches_including_legacy_minute_alias(tmp_path, monkeypatch):
    from app.multitenancy.business_defaults_bundle import install_extensions
    from app.services import kline_periods, preferences

    pack, market, config = (tmp_path / p for p in ("pack", "market", "config"))
    write_bundle(pack, schedules=schedules({
        "data_sync_auto_enabled": True,
        "depth_finalize_time": {"hour": 16, "minute": 0},
        "kline_sync_1m": {"enabled": True, "interval_seconds": 60,
                          "windows": kline_periods.PERIOD_WINDOWS["1m"], "time": "16:00"},
    }))
    config.mkdir()
    path = config / "preferences.json"
    path.write_text('{"data_sync_auto_enabled": false, "minute_refresh_enabled": false}')
    monkeypatch.setattr(preferences, "_path", lambda: path)
    install_extensions(pack, market, platform_config=config, apply=True)
    assert not preferences.get_data_sync_auto_enabled()
    assert not kline_periods.get_configs()["1m"]["enabled"]
    assert preferences.get_depth_finalize_time() == {"hour": 16, "minute": 0}
    previous = path.read_bytes()
    install_extensions(pack, market, platform_config=config, apply=True)
    assert path.read_bytes() == previous


@pytest.mark.parametrize("prefs", [
    {"financial_sync_times": {"income": "2026-11-27"}},
    {"depth_finalize_time": {"hour": 24, "minute": 0}},
    {"kline_sync_1d": {"enabled": True, "interval_seconds": 60,
                      "windows": [["09:30", "11:30"], ["13:00", "15:00"]], "time": "16:00"}},
])
def test_invalid_schedule_aborts_before_installing_any_files(tmp_path, prefs):
    from app.multitenancy.business_defaults_bundle import install_extensions

    pack, market, config = (tmp_path / p for p in ("pack", "market", "config"))
    write_bundle(pack, schedules=schedules(prefs))
    with pytest.raises(ValueError):
        install_extensions(pack, market, platform_config=config, apply=True)
    assert not market.exists() and not config.exists()
