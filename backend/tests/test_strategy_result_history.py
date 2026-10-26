from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Barrier

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


@pytest.mark.parametrize("rows", [[], [{"symbol": "A", "score": 8}]])
def test_duplicate_daily_result_reuses_payload_and_tracks_latest_run(tmp_path, engine, rows):
    import sqlite3

    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    result = {"as_of": "2026-10-22", "total": len(rows), "rows": rows, "elapsed_ms": 10}
    first = store.save(key, result, started_at=10, input_generation={"market": 1})
    store.read(key, result["as_of"])
    repeated = store.save(key, {**result, "elapsed_ms": 20, "computed_at": 999},
                          started_at=30, input_generation={"market": 2})
    assert repeated["result"] == first["result"]
    assert repeated["_cache_token"] == first["_cache_token"]
    assert repeated["summary"]["computed_at"] == first["summary"]["computed_at"]
    assert repeated["summary"]["started_at"] == 30
    assert repeated["summary"]["input_generation"] == {"market": 2}
    assert repeated["ever_rows"] == first["ever_rows"]
    assert store.read(key, result["as_of"]) == repeated
    with sqlite3.connect(store.path) as conn:
        revision = conn.execute("SELECT revision FROM strategy_results").fetchone()[0]
    # Retrying the same run and a late older run must not write again.
    assert store.save(key, result, started_at=30, input_generation={"market": 2}) == repeated
    assert store.save(key, {**result, "rows": [{"symbol": "OLD"}]}, started_at=20) == repeated
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT revision FROM strategy_results").fetchone()[0] == revision
    module.clear_memory()
    assert module.StrategyResults(tmp_path).read(key, result["as_of"]) == repeated
    assert len(store.list_history("sample")["records"]) == 1


def test_late_older_result_does_not_write_or_invalidate_detail_cache(tmp_path, engine):
    import sqlite3

    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    latest = store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "NEW"}]}, started_at=30)
    store.read(key, "2026-10-22")
    with sqlite3.connect(store.path) as conn:
        before = conn.execute("SELECT summary,value,revision FROM strategy_results").fetchone()
    assert store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "OLD"}]},
                      started_at=20) == latest
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT summary,value,revision FROM strategy_results").fetchone() == before


@pytest.mark.parametrize("single_first", [True, False])
def test_single_and_batch_empty_optional_fields_are_equivalent(tmp_path, engine, single_first):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    batch = {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}
    single = {**batch, "strategy_id": "sample", "scores": {}, "warnings": [],
              "entry_signal_hits": [], "exit_signal_hits": [], "entry_filter_error": None}
    first, second = (single, batch) if single_first else (batch, single)
    saved = store.save(key, first, started_at=10)
    repeated = store.save(key, second, started_at=20)
    assert repeated["result"] == saved["result"]
    assert repeated["_cache_token"] == saved["_cache_token"]
    assert len(store.list_history("sample")["records"]) == 1


@pytest.mark.parametrize("changed", [
    {"rows": [{"symbol": "A", "score": 9}, {"symbol": "B", "score": 7}]},
    {"rows": [{"symbol": "B", "score": 7}, {"symbol": "A", "score": 8}]},
    {"total": 3},
    {"warnings": ["数据不足"]},
    {"scores": {"A": 9}},
    {"entry_signal_hits": ["A"]},
    {"exit_signal_hits": ["B"]},
    {"entry_filter_error": "入场失败"},
])
def test_meaningful_result_changes_replace_daily_snapshot(tmp_path, engine, changed):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    result = {"as_of": "2026-10-22", "total": 2,
              "rows": [{"symbol": "A", "score": 8}, {"symbol": "B", "score": 7}]}
    first = store.save(key, result, started_at=10)
    updated = store.save(key, {**result, **changed}, started_at=20)
    assert updated["_cache_token"] != first["_cache_token"]
    for field, value in changed.items():
        assert store.read(key, result["as_of"])["result"][field] == value
    assert len(store.list_history("sample")["records"]) == 1


def test_identical_rows_on_other_dates_or_configurations_remain_separate(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    result = {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}
    keys = [key, replace(key, strategy_version="v2"), replace(key, params_json='{"threshold":11}')]
    for variant in keys:
        store.save(variant, result, started_at=10)
        store.save(variant, result, started_at=20)
    store.save(key, {**result, "as_of": "2026-10-23"}, started_at=30)
    assert len(store.list_history("sample")["records"]) == 4


def test_duplicate_metadata_update_respects_document_limit_and_keeps_success(tmp_path, engine, monkeypatch):
    import app.multitenancy.documents as documents
    from app.cell.context import AccessError

    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    result = {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A", "name": "x" * 500}]}
    first = store.save(key, result, started_at=10)
    monkeypatch.setattr(documents, "MAX_DOCUMENT_BYTES", len(module._encode(first).encode()) + 200)
    with pytest.raises(AccessError) as error:
        store.save(key, result, started_at=30, input_generation={"market": "x" * 500})
    assert error.value.status == 413
    assert store.read(key, result["as_of"]) == first
    updated = store.save(key, {**result, "rows": [{"symbol": "B"}]}, started_at=20)
    assert updated["result"]["rows"] == [{"symbol": "B"}]


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


@pytest.mark.parametrize("separate_instances", [False, True])
def test_concurrent_first_writes_initialize_wal_without_losing_results(
    tmp_path, separate_instances,
):
    module = history()
    key = module.ResultIdentity(
        "sample", "示例", "v1", "{}", "{}", "null", "stock", "1d",
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        for attempt in range(32):
            directory = tmp_path / str(attempt)
            shared = module.StrategyResults(directory)
            ready = Barrier(8)

            def save(day, directory=directory, shared=shared, ready=ready):
                store = module.StrategyResults(directory) if separate_instances else shared
                ready.wait(timeout=10)
                return store.save(
                    key, {"as_of": f"2026-10-{day}", "rows": [{"symbol": str(day)}]},
                    started_at=day,
                )

            list(executor.map(save, range(20, 28)))
            assert len(shared.list_history("sample")["records"]) == 8
            for day in range(20, 28):
                assert shared.read(key, f"2026-10-{day}")["result"]["rows"] == [
                    {"symbol": str(day)}
                ]


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


def test_history_filters_are_combined_before_pagination(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    target = replace(key, strategy_name="旧名称_100%", strategy_version="Version-ABC",
                     asset_type="etf", timeframe="1m")
    for day in ("2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"):
        store.save(target, {"as_of": day, "rows": []}, started_at=1)
        store.save(key, {"as_of": day, "rows": []}, started_at=1)
    filters = dict(q="旧名称_100%", start_date="2026-10-20", end_date="2026-10-22",
                   asset_type="etf", timeframe="1m", limit=2)
    page = store.list_history("sample", **filters)
    assert [r["as_of"] for r in page["records"]] == ["2026-10-22", "2026-10-21"]
    second = store.list_history("sample", cursor=page["next_cursor"], **filters)
    assert [r["as_of"] for r in second["records"]] == ["2026-10-20"]
    assert second["next_cursor"] is None
    assert len(store.list_history("sample", q="version-abc")["records"]) == 4
    assert store.list_history("sample", q="no match")["records"] == []


@pytest.mark.parametrize("filters", [
    {"start_date": "invalid"}, {"end_date": "2026-02-30"},
    {"start_date": "2026-10-23", "end_date": "2026-10-22"},
    {"asset_type": "index"}, {"timeframe": "invalid"}, {"q": "x" * 201},
])
def test_history_rejects_invalid_filters(tmp_path, filters):
    with pytest.raises(ValueError):
        history().StrategyResults(tmp_path).list_history("sample", **filters)


def test_delete_only_selected_archive_and_evicts_only_its_memory(tmp_path, engine):
    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    saved = store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "A"}]}, started_at=1)
    store.save(key, {"as_of": "2026-10-23", "rows": [{"symbol": "B"}]}, started_at=2)
    store.read(key, "2026-10-22")
    store.read(key, "2026-10-23")
    strategy_cache.write_cache(tmp_path, "2026-10-22", {"sample": {"rows": [{"symbol": "LIVE"}]}})
    assert not store.delete("other", saved["record_id"])
    assert not module.StrategyResults(tmp_path / "foreign").delete("sample", saved["record_id"])
    assert not (tmp_path / "foreign").exists()
    assert store.delete("sample", saved["record_id"])
    assert not store.delete("sample", saved["record_id"])
    assert store.read(key, "2026-10-22") is None
    assert store.summaries([key], "2026-10-22") == {}
    assert store.memberships([key], "2026-10-22", {"A"}) == {"A": []}
    assert [r["as_of"] for r in store.list_history("sample")["records"]] == ["2026-10-23"]
    assert any(k[0] == store.scope and k[1] == key.record_id("2026-10-23") for k in module._memory)
    assert not any(k[0] == store.scope and k[1] == saved["record_id"] for k in module._memory)
    assert strategy_cache.read_cache(tmp_path)["results"]["sample"]["rows"] == [{"symbol": "LIVE"}]


def test_deleted_id_recreated_by_another_process_cannot_reuse_cached_revision(tmp_path, engine):
    import sqlite3

    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "OLD"}]}, started_at=1)
    store.read(key, "2026-10-22")
    # An external process deletes the row without access to this process's cache.
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM strategy_results WHERE id=?", (key.record_id("2026-10-22"),))
    module.StrategyResults(tmp_path).save(
        key, {"as_of": "2026-10-22", "rows": [{"symbol": "NEW"}]}, started_at=2,
    )
    assert store.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "NEW"}]


def test_legacy_archive_without_cache_token_can_be_read_updated_and_deleted(tmp_path, engine):
    import json
    import sqlite3

    module = history()
    key = identity(module, engine, tmp_path)
    store = module.StrategyResults(tmp_path)
    saved = store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "LEGACY"}]}, started_at=1)
    saved.pop("_cache_token")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE strategy_results SET value=? WHERE id=?",
                     (json.dumps(saved), saved["record_id"]))
    assert store.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "LEGACY"}]
    store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "NEW"}]}, started_at=2)
    assert store.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "NEW"}]
    assert store.delete("sample", saved["record_id"])
    assert store.read(key, "2026-10-22") is None
