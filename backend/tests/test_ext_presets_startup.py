"""内置概念/行业默认可拉取, 兼容修复空预设并保留用户配置。

ensure_builtin_presets 只负责创建配置, 不直接等待网络请求; 随后
PullScheduler.refresh 为 enabled 配置创建后台任务。旧版自动生成的空配置
应恢复默认值, 用户自定义地址、主动关闭或已有数据的配置不被覆盖。
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.config import settings
from app.services.ext_data import ExtConfigStore
from app.services.ext_presets import (
    _concept_preset,
    _industry_preset,
    ensure_builtin_presets,
)

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


def test_feature_manual_matches_fresh_install_defaults(monkeypatch):
    monkeypatch.setattr(settings, "ext_concept_data_url", "")
    monkeypatch.setattr(settings, "ext_industry_data_url", "")
    manual = (Path(__file__).resolve().parents[2] / "docs/features.md").read_text()
    for preset in (_concept_preset(), _industry_preset()):
        assert preset.pull.url and preset.pull.enabled
    assert "新安装默认启用定时拉取" in manual
    assert "环境变量未配置或为空时使用默认 URL" in manual
    assert "未配置 URL 时默认关闭" not in manual
    assert "首次自动同步" in manual and "确认默认 rustdx" in manual
    guide = (Path(__file__).resolve().parents[2] / "操作说明书.md").read_text()
    assert "未配置 URL 时默认关闭" not in guide
    assert "新安装需要在 `.env` 设置" not in guide
    assert "自带默认拉取地址" in guide


def test_builtin_presets_have_enabled_defaults_with_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复制旧 .env 示例的空 URL 也应使用可拉取的默认值。"""
    monkeypatch.setattr(settings, "ext_concept_data_url", "")
    monkeypatch.setattr(settings, "ext_industry_data_url", "")

    for preset, endpoint in (
        (_concept_preset(), "ths-concepts"),
        (_industry_preset(), "ths-industries"),
    ):
        assert preset.pull is not None
        assert preset.pull.url == f"https://shy313.com/api/plugins/market_flow/exports/{endpoint}"
        assert preset.pull.enabled is True
        assert preset.pull.schedule_minutes == 1
        assert preset.pull.time_window_start == "09:15"
        assert preset.pull.time_window_end == "15:15"


def test_ensure_builtin_presets_writes_enabled_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式配置上游后写入启用配置, 供调度器创建盘中定时任务。"""
    monkeypatch.setattr(settings, "ext_concept_data_url", "https://data.example/concepts")
    monkeypatch.setattr(settings, "ext_industry_data_url", "https://data.example/industries")

    asyncio.run(ensure_builtin_presets(tmp_path))

    store = ExtConfigStore(tmp_path)
    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None, f"{cid} 配置未创建"
        assert config.pull is not None
        assert config.pull.url.startswith("https://data.example/")
        assert config.pull.enabled is True
        assert config.pull.schedule_minutes == 1
        assert config.pull.time_window_start == "09:15"
        assert config.pull.time_window_end == "15:15"


def test_ensure_builtin_presets_keeps_existing_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """老用户/已存在的配置一律不动, 包括用户主动关闭自动拉取。"""
    monkeypatch.setattr(settings, "ext_concept_data_url", "https://data.example/concepts")
    monkeypatch.setattr(settings, "ext_industry_data_url", "https://data.example/industries")

    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    config = store.get("ext_gn_ths")
    assert config is not None and config.pull is not None
    config.pull.enabled = False
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None and refreshed.pull is not None
    assert refreshed.pull.enabled is False, "已存在配置被静默改写, 违反「绝不覆盖」原则"


@pytest.mark.parametrize("cid", _PRESET_IDS)
@pytest.mark.parametrize("legacy_description", [False, True])
def test_empty_generated_preset_is_repaired_once(tmp_path, monkeypatch, cid, legacy_description):
    monkeypatch.setattr(settings, "ext_concept_data_url", "")
    monkeypatch.setattr(settings, "ext_industry_data_url", "")
    preset = _concept_preset() if cid == "ext_gn_ths" else _industry_preset()
    preset.pull.url = ""
    preset.pull.enabled = False
    if legacy_description:
        preset.description = preset.description.replace(
            "(交易日", "(配置上游后, 交易日"
        ).replace(", 启动时立即刷新", "")
    store = ExtConfigStore(tmp_path)
    store.upsert(preset)
    created_at = preset.created_at

    asyncio.run(ensure_builtin_presets(tmp_path))

    repaired = store.get(cid)
    assert repaired.pull.url
    assert repaired.pull.enabled is True
    assert repaired.created_at == created_at
    first = repaired.to_dict()
    asyncio.run(ensure_builtin_presets(tmp_path))
    assert store.get(cid).to_dict() == first


@pytest.mark.parametrize("change", ["label", "schedule", "auth", "data"])
def test_empty_but_customized_preset_is_preserved(tmp_path, change):
    preset = _concept_preset()
    preset.pull.url = ""
    preset.pull.enabled = False
    if change == "label":
        preset.label = "我的概念数据"
    elif change == "schedule":
        preset.pull.schedule_minutes = 30
    elif change == "auth":
        preset.pull.auth = {"type": "bearer"}
    else:
        directory = tmp_path / "ext_data" / preset.id / "snapshot"
        directory.mkdir(parents=True)
        (directory / "part.parquet").write_bytes(b"existing-data")
    store = ExtConfigStore(tmp_path)
    store.upsert(preset)
    original = store.get(preset.id).to_dict()

    asyncio.run(ensure_builtin_presets(tmp_path))

    assert store.get(preset.id).to_dict() == original


@pytest.mark.parametrize("envelope", [None, "data", "list", "rows", "result", "results", "custom"])
def test_manual_preset_fetch_uses_saved_upstream(tmp_path, monkeypatch, envelope):
    from app.services import ext_presets, ext_pull

    preset = _concept_preset()
    preset.pull.url = "https://custom.example/concepts"
    preset.pull.enabled = False
    preset.pull.headers = {"X-Custom": "configured"}
    preset.pull.response_path = ""
    store = ExtConfigStore(tmp_path)
    store.upsert(preset)
    calls = []

    async def fake_fetch(pull, config_id, day=None, extra_params=None):
        calls.append((pull.url, pull.headers, config_id, day))
        rows = [{"symbol": "600519.SH", "name": "贵州茅台", "concepts": ["白酒"]}]
        return rows if envelope is None else {envelope: rows}

    monkeypatch.setattr(ext_pull, "_request_json", fake_fetch)
    monkeypatch.setattr(ext_pull, "cn_today", lambda: date(2026, 10, 17))

    assert asyncio.run(ext_presets.fetch_preset(preset.id, tmp_path)) == 1
    assert calls == [
        ("https://custom.example/concepts", {"X-Custom": "configured"}, preset.id, date(2026, 10, 17))
    ]
    files = list((tmp_path / "ext_data" / preset.id).rglob("*.parquet"))
    assert pl.read_parquet(files[0])["所属概念"].to_list() == ["白酒"]
    assert store.get(preset.id).pull.enabled is False


def test_manual_preset_fetch_repairs_empty_config_through_api(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api import ext_data as ext_api
    from app.services import ext_pull

    preset = _industry_preset()
    preset.pull.url = ""
    preset.pull.enabled = False
    store = ExtConfigStore(tmp_path)
    store.upsert(preset)
    calls = []

    async def fake_fetch(pull, config_id, day=None, extra_params=None):
        calls.append(pull.url)
        return [{"symbol": "000001.SZ", "name": "平安银行", "industries": ["金融", "银行"]}]

    monkeypatch.setattr(ext_pull, "_request_json", fake_fetch)
    monkeypatch.setattr(ext_api, "_refresh_views", lambda request: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))

    result = asyncio.run(ext_api.fetch_preset_data(request, preset.id))

    assert result == {"status": "ok", "rows": 1}
    assert calls == [_industry_preset().pull.url]
    assert store.get(preset.id).pull.enabled is True
    files = list((tmp_path / "ext_data" / preset.id).rglob("*.parquet"))
    assert pl.read_parquet(files[0])["所属同花顺行业"].to_list() == ["金融-银行"]
