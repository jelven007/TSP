from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest

from app.services import strategy_cache
from app.strategy.engine import StrategyEngine

CODE = '''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock", "etf"],
        "params": [{"id": "threshold", "default": 10}]}
BASIC_FILTER = {"exclude_new_days": 0}
def filter(df, params):
    return pl.col("close") > params["threshold"]
'''


def history():
    from app import services

    assert importlib.util.find_spec("app.services.strategy_results"), "缺少持久结果仓库"
    return importlib.import_module(f"{services.__name__}.strategy_results")


@pytest.fixture
def engine(tmp_path):
    directory = tmp_path / "strategies"
    directory.mkdir()
    (directory / "sample.py").write_text(CODE)
    return StrategyEngine([directory])


def identity(module, engine, tmp_path, **kwargs):
    return module.capture_identity(engine, tmp_path, "sample", **kwargs)


def test_dates_survive_restart_and_legacy_cache_clear(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    store.save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}, started_at=10)
    store.save(key, {"as_of": "2026-10-23", "total": 1, "rows": [{"symbol": "B"}]}, started_at=20)
    module.clear_memory()
    strategy_cache.clear_cache(tmp_path)
    restarted = module.StrategyResults(tmp_path)
    assert restarted.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "A"}]
    assert restarted.read(key, "2026-10-23")["result"]["rows"] == [{"symbol": "B"}]
    assert restarted.summaries([key], "2026-10-22")[key.strategy_id]["total"] == 1
    assert "rows" not in restarted.summaries([key], "2026-10-22")[key.strategy_id]


def test_identity_dimensions_and_effective_defaults(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    assert key == identity(module, engine, tmp_path, params={"threshold": 10})
    assert key == identity(module, engine, tmp_path, params={}, overrides={"params": {"threshold": 10}})
    variants = [
        identity(module, engine, tmp_path, params={"threshold": 11}),
        identity(module, engine, tmp_path, asset_type="etf"),
        identity(module, engine, tmp_path, timeframe="1m"),
        identity(module, engine, tmp_path, pool=["A"]),
        identity(module, engine, tmp_path, overrides={"display_limit": 5}),
        replace(key, strategy_name="新名称"),
        replace(key, strategy_version="different"),
        replace(key, strategy_id="other"),
    ]
    assert len({key.fingerprint, *(item.fingerprint for item in variants)}) == 9


def test_version_is_logic_not_name_or_comments(tmp_path, engine):
    original = engine.get("sample")
    assert getattr(original, "logic_version", ""), "加载时未捕获逻辑版本"
    path = original.file_path
    path.write_text(CODE.replace("示例", "新名称") + "\n# 展示注释\n")
    renamed = StrategyEngine._load_file(path)
    assert renamed.logic_version == original.logic_version
    path.write_text(CODE.replace('> params', '>= params'))
    changed = StrategyEngine._load_file(path)
    assert changed.logic_version != original.logic_version
    assert engine.get("sample").logic_version == original.logic_version


def test_success_union_and_late_old_run_do_not_overwrite(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    first = store.save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}, started_at=10)
    store.save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "B"}]}, started_at=30)
    store.save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "OLD"}]}, started_at=20)
    saved = store.read(key, "2026-10-22")
    assert saved["record_id"] == first["record_id"]
    assert saved["result"]["rows"] == [{"symbol": "B"}]
    assert set(saved["ever_rows"]) == {"A", "B"}
    store.save(key, {"as_of": "2026-10-22", "total": 0, "rows": []}, started_at=40)
    assert store.read(key, "2026-10-22")["result"]["rows"] == []
    assert store.summaries([key], "2026-10-22")["sample"]["ever_count"] == 2


def test_failure_does_not_replace_success_and_reads_do_not_create_db(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    assert store.read(key, "2026-10-22") is None
    assert not (tmp_path / "user_data" / "strategy_results.sqlite3").exists()
    store.save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}, started_at=10)
    with pytest.raises(Exception, match=r"格式|JSON"):
        store.save(key, {"as_of": "2026-10-22", "rows": [{"score": float("nan")}]}, started_at=20)
    assert store.read(key, "2026-10-22")["result"]["total"] == 1


def test_history_is_paginated_isolated_and_returns_old_parameters(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        store.save(key, {"as_of": day, "total": 0, "rows": []}, started_at=1)
    page = store.list_history("sample", limit=2)
    assert [r["as_of"] for r in page["records"]] == ["2026-10-22", "2026-10-21"]
    assert page["next_cursor"]
    second = store.list_history("sample", limit=2, cursor=page["next_cursor"])
    assert [r["as_of"] for r in second["records"]] == ["2026-10-20"]
    record = page["records"][0]
    assert record["params"] == {"threshold": 10}
    assert "rows" not in record
    assert store.detail("other", record["record_id"]) is None
    assert module.StrategyResults(tmp_path / "another").detail("sample", record["record_id"]) is None


def test_concurrent_successes_and_external_updates_invalidate_memory(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(
            lambda n: store.save(key, {"as_of": str(date(2026, 10, n)), "total": 1,
                                     "rows": [{"symbol": str(n)}]}, started_at=n),
            range(20, 24),
        ))
    saved = store.read(key, "2026-10-22")
    module.StrategyResults(tmp_path).save(
        key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "NEW"}]}, started_at=30,
    )
    assert saved["result"]["rows"] == [{"symbol": "22"}]
    assert store.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "NEW"}]


def test_helper_logic_changes_version_and_oversized_write_keeps_success(tmp_path, engine):
    module = history()
    original = engine.get("sample")
    helper = original.file_path.parent / "_helper.py"
    helper.write_text("VALUE = 1\n")
    first = StrategyEngine._load_file(original.file_path).logic_version
    helper.write_text("VALUE = 2\n")
    assert StrategyEngine._load_file(original.file_path).logic_version != first
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    store.save(key, {"as_of": "2026-10-22", "total": 0, "rows": []}, started_at=1)
    from app.cell.context import AccessError
    with pytest.raises(AccessError) as error:
        store.save(key, {"as_of": "2026-10-22", "rows": [{"name": "x" * (16 * 1024 * 1024)}]},
                   started_at=2)
    assert error.value.status == 413
    assert store.read(key, "2026-10-22")["result"]["rows"] == []


def test_execution_snapshot_keeps_loaded_definition_when_engine_reloads(tmp_path, engine):
    module = history()
    snapshot = module.execution_snapshot(engine)
    old = identity(module, snapshot, tmp_path)
    engine.get("sample").file_path.write_text(CODE.replace('> params', '>= params'))
    engine.reload()
    assert identity(module, engine, tmp_path).strategy_version != old.strategy_version
    assert identity(module, snapshot, tmp_path) == old
