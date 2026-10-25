"""配置/凭证 JSON 存储的原子写测试 — 写到一半不该把已有配置换成空文件。

`fs_utils` 的模块 docstring 写着「新代码统一用本模块的 atomic_write_text」,
`lots.py`、`monitor_rules.py` 也都照做了; 但下面这些存储还在裸 `write_text`:
secrets_store、auth、preferences、ExtConfigStore、策略 override、自定义信号、
自定义因子、自定义分析菜单、自定义数据源 YAML。它们的读侧都吞掉解析错误返回
默认值 (`{}` 或跳过该项), 所以半截文件不会报错, 只会安静地把配置清空。

这里用「写入过程中失败」模拟磁盘写满/进程被杀: 让 `Path.write_text` 只写前几个
字节就抛 OSError。裸写会把目标文件本身截断; 原子写截断的是 .tmp, `os.replace`
不会执行, 目标文件原封不动。
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api import analysis
from app.data_providers.custom import loader as custom_loader
from app.factors import store as factor_store
from app.services import preferences
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.services.fs_utils import atomic_write_text
from app.strategy import config as strat_config
from app.strategy import custom_signals


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "platform_config_dir", None)
    monkeypatch.setattr(settings, "service_role", "standalone")
    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    preferences._invalidate_cache()
    yield tmp_path
    preferences._invalidate_cache()


@pytest.fixture()
def torn_write(monkeypatch: pytest.MonkeyPatch):
    """让下一次 write_text 只写前 8 个字节然后失败。"""
    real = Path.write_text

    def _torn(self: Path, data: str, *args, **kwargs):
        real(self, data[:8], *args, **kwargs)
        raise OSError(28, "No space left on device")

    def _arm() -> None:
        monkeypatch.setattr(Path, "write_text", _torn)

    return _arm


def test_preferences_survive_a_torn_write(data_dir: Path, torn_write) -> None:
    preferences.save({"theme": "dark", "kline_compress": True})
    assert preferences.load()["theme"] == "dark"

    torn_write()
    with pytest.raises(OSError):
        preferences.save({"theme": "light"})

    preferences._invalidate_cache()
    assert preferences.load() == {"theme": "dark", "kline_compress": True}


def test_secrets_survive_a_torn_write(data_dir: Path, torn_write) -> None:
    secrets_store.save({"tickflow_token": "keep-me"})
    assert secrets_store.load()["tickflow_token"] == "keep-me"

    torn_write()
    with pytest.raises(OSError):
        secrets_store.save({"tickflow_token": "replacement"})

    assert secrets_store.load() == {"tickflow_token": "keep-me"}


def test_ext_config_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    store = ExtConfigStore(data_dir / "ext_data")
    config = ExtConfig(
        id="hot",
        label="人气",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    )
    store.upsert(config)
    assert [c.id for c in store.load_all()] == ["hot"]

    config.label = "人气榜"
    torn_write()
    with pytest.raises(OSError):
        store.upsert(config)

    reloaded = ExtConfigStore(data_dir / "ext_data").load_all()
    assert [c.id for c in reloaded] == ["hot"]
    assert reloaded[0].label == "人气"


def test_strategy_override_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """load_override 吞异常返回 {} —— 半截文件会让策略参数静默回默认值。"""
    strat_config._override_cache.clear()
    strat_config._override_cache_sig.clear()
    strat_config.save_override(data_dir, "s1", {"params": {"period": 20}})
    assert strat_config.load_override(data_dir, "s1") == {"params": {"period": 20}}

    torn_write()
    with pytest.raises(OSError):
        strat_config.save_override(data_dir, "s1", {"params": {"period": 60}})

    strat_config._override_cache.clear()
    strat_config._override_cache_sig.clear()
    assert strat_config.load_override(data_dir, "s1") == {"params": {"period": 20}}


def test_custom_signal_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """custom_signals.load_all 跳过损坏文件 —— 半截文件会让该信号静默消失。"""
    sig = {
        "id": "vol_up", "name": "放量", "kind": "entry", "enabled": True,
        "conditions": [{"left": "volume", "op": ">", "right": "0", "leftDays": 0, "rightDays": 0}],
    }
    custom_signals.save_one(data_dir, sig)
    assert [s["id"] for s in custom_signals.load_all(data_dir)] == ["vol_up"]

    torn_write()
    with pytest.raises(OSError):
        custom_signals.save_one(data_dir, {**sig, "name": "放量2"})

    assert custom_signals.load_all(data_dir) == [sig]


def test_custom_factor_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """factors.store.load_all 跳过损坏文件 —— 半截文件会让该因子静默消失。"""
    definition = {"id": "uf_mom", "kind": "custom", "label": "动量", "status": "draft"}
    factor_store.save_one(data_dir, definition)
    assert [d["id"] for d in factor_store.load_all(data_dir)] == ["uf_mom"]

    torn_write()
    with pytest.raises(OSError):
        factor_store.save_one(data_dir, {**definition, "label": "动量2"})

    assert factor_store.load_all(data_dir) == [definition]


def test_analysis_menu_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """analysis._load_saved 遇到解析失败直接 continue —— 半截文件会让菜单静默消失。"""
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))))
    )
    analysis._save(request, analysis.AnalysisMenu(id="limit_up", label="涨停分析", data_source="hot"))
    assert [m.label for m in analysis._load_saved(request)] == ["涨停分析"]

    torn_write()
    with pytest.raises(OSError):
        analysis._save(request, analysis.AnalysisMenu(id="limit_up", label="涨停复盘", data_source="hot"))

    assert [(m.id, m.label) for m in analysis._load_saved(request)] == [("limit_up", "涨停分析")]


def test_custom_source_yaml_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """自定义数据源 YAML 被截断后, loader.load_all 按单个文件 load_config 会失败或读到残缺配置。"""
    config = {
        "name": "demo",
        "display_name": "演示源",
        "datasets": {"daily": {"url": "https://example.test/daily", "method": "GET"}},
    }
    path = custom_loader.save_config("demo", config)
    before = path.read_text(encoding="utf-8")
    assert custom_loader.load_config(path).display_name == "演示源"

    torn_write()
    with pytest.raises(OSError):
        custom_loader.save_config("demo", {**config, "display_name": "演示源2"})

    assert path.read_text(encoding="utf-8") == before
    reloaded = custom_loader.load_config(path)
    assert reloaded.display_name == "演示源"
    assert list(reloaded.datasets) == ["daily"]


def test_a_normal_save_still_writes_what_it_was_given(data_dir: Path) -> None:
    """没有失败时行为不变 —— 内容、合并语义和文件位置都照旧。"""
    preferences.save({"theme": "dark"})
    preferences.save({"kline_compress": True})
    assert preferences.load() == {"theme": "dark", "kline_compress": True}

    secrets_store.save({"a": "1"})
    secrets_store.save({"b": "2"})
    assert secrets_store.load() == {"a": "1", "b": "2"}

    written = json.loads(
        (data_dir / "user_data" / "preferences.json").read_text(encoding="utf-8")
    )
    assert written == {"theme": "dark", "kline_compress": True}


def test_no_tmp_file_is_left_behind(data_dir: Path) -> None:
    preferences.save({"theme": "dark"})
    secrets_store.save({"a": "1"})
    strat_config.save_override(data_dir, "s1", {"params": {}})
    custom_signals.save_one(data_dir, {"id": "vol_up", "conditions": []})
    factor_store.save_one(data_dir, {"id": "uf_mom", "label": "动量"})
    custom_loader.save_config("demo", {"name": "demo", "datasets": {}})

    leftovers = sorted(p.name for p in data_dir.rglob("*.tmp"))
    assert leftovers == []


def test_concurrent_atomic_writes_do_not_share_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    barrier = threading.Barrier(2)
    real_replace = os.replace

    def replace(source, target):
        barrier.wait(timeout=5)
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(atomic_write_text, path, json.dumps({"value": value}), mode=0o600)
            for value in ("first", "second")
        ]
        for future in futures:
            future.result(timeout=5)
    assert json.loads(path.read_text()) in ({"value": "first"}, {"value": "second"})
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("operation", ["save", "clear"])
def test_concurrent_secrets_changes_preserve_both_updates(data_dir, monkeypatch, operation):
    secrets_store.save({"ai_model": "old-model", "ai_api_key": "test-key"})
    entered, release, second_started, second_done = (threading.Event() for _ in range(4))
    real_write = secrets_store.atomic_write_text

    def write(path, text, **kwargs):
        if json.loads(text).get("ai_model") == "new-model" and not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return real_write(path, text, **kwargs)

    def second_change():
        second_started.set()
        try:
            if operation == "save":
                secrets_store.save({"tickflow_api_key": "other-test-key"})
            else:
                secrets_store.clear("ai_api_key")
        finally:
            second_done.set()

    monkeypatch.setattr(secrets_store, "atomic_write_text", write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(secrets_store.save, {"ai_model": "new-model"})
        assert entered.wait(timeout=5)
        second = pool.submit(second_change)
        assert second_started.wait(timeout=5)
        # The second operation can finish only if it is allowed to race the first.
        second_done.wait(timeout=0.5)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    expected = {"ai_model": "new-model"}
    if operation == "save":
        expected.update(ai_api_key="test-key", tickflow_api_key="other-test-key")
    assert secrets_store.load() == expected


def test_secret_write_is_private_before_content_is_written(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    real_write = Path.write_text

    def write(temp_path, text, *args, **kwargs):
        if os.name != "nt":
            assert temp_path.stat().st_mode & 0o777 == 0o600
        return real_write(temp_path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    atomic_write_text(path, '{"key": "test-key"}', mode=0o600)
    assert json.loads(path.read_text()) == {"key": "test-key"}


def test_failed_atomic_write_cleans_temporary_file(tmp_path, torn_write):
    path = tmp_path / "secrets.json"
    path.write_text('{"key": "test-key"}')
    torn_write()
    with pytest.raises(OSError):
        atomic_write_text(path, '{"key": "replacement"}', mode=0o600)
    assert json.loads(path.read_text()) == {"key": "test-key"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_secrets_cache_is_invalidated_even_when_file_signature_is_unchanged(data_dir, monkeypatch):
    secrets_store.save({"value": "a"})
    assert secrets_store.load() == {"value": "a"}
    path = secrets_store._path()
    old = path.stat()
    real_write = secrets_store.atomic_write_text

    def write(*args, **kwargs):
        real_write(*args, **kwargs)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))

    monkeypatch.setattr(secrets_store, "atomic_write_text", write)
    secrets_store.save({"value": "b"})
    assert secrets_store.load() == {"value": "b"}
