"""自选证券的多策略关系、旧文件迁移及API可见性。"""
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy as strategy_api
from app.api import watchlist as watchlist_api
from app.config import settings
from app.services import watchlist


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(watchlist_api, "_service", lambda _: watchlist)
    return watchlist


def test_relations_survive_reimport_reorder_and_group_changes(store):
    _, first = store.create_group("核心")
    _, second = store.create_group("观察")
    store.add("000001.SZ", "备注", first["id"])
    before = store.list_symbols()
    rev = store.revision()
    rows = store.set_strategies("000001.SZ", ["daily", "minute", "daily"])
    assert rows[0] == {**before[0], "strategy_ids": ["daily", "minute"]}
    assert store.revision() == rev + 1
    store.add_batch(["600000.SH", "000001.SZ"], group_ids=[second["id"]])
    store.move_to_top("000001.SZ")
    store.clear_group(first["id"])
    store.create_group("新组", symbols=["000001.SZ", "300750.SZ"])
    rows = store.list_symbols()
    shared = next(row for row in rows if row["symbol"] == "000001.SZ")
    assert shared["strategy_ids"] == ["daily", "minute"]
    assert second["id"] in shared["group_ids"]
    store.remove("000001.SZ")
    store.add("000001.SZ")
    assert store.list_symbols()[0]["strategy_ids"] == []
    store.set_strategies("000001.SZ", ["daily"])
    store.clear()
    store.add("000001.SZ")
    assert store.list_symbols()[0]["strategy_ids"] == []


def test_missing_symbol_and_empty_relations(store):
    store.add("600000.SH")
    rev = store.revision()
    with pytest.raises(KeyError):
        store.set_strategies("000001.SZ", ["daily"])
    assert store.revision() == rev
    store.set_strategies("600000.SH", ["daily"])
    assert store.set_strategies("600000.SH", [])[0]["strategy_ids"] == []


def test_old_schema_read_only_and_backup_preserved(store):
    path = store._path()
    pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"], "added_at": ["first", "second"],
        "note": ["首条", "次条"], "group_ids": [["a"], ["b"]],
    }).write_parquet(path)
    original = path.read_bytes()
    assert store.list_symbols() == [{
        "symbol": "600000.SH", "added_at": "first", "note": "首条",
        "group_ids": ["a", "b"], "strategy_ids": [],
    }]
    assert path.read_bytes() == original
    backup = path.with_suffix(".parquet.bak")
    backup.write_bytes(b"previous-group-migration")
    store.set_strategies("600000.SH", ["daily"])
    assert backup.read_bytes() == b"previous-group-migration"
    backup.unlink()
    path.write_bytes(original)
    store.set_strategies("600000.SH", ["minute"])
    assert backup.read_bytes() == original


def test_api_validates_visible_strategies_and_preserves_failed_update(store, monkeypatch):
    app = FastAPI()
    app.include_router(watchlist_api.router)
    app.state.repo = SimpleNamespace(get_name_map=lambda _: {})

    def get(strategy_id):
        if strategy_id == "foreign":
            raise ValueError("unknown strategy")
        return SimpleNamespace(meta={"research_only": strategy_id == "draft"})

    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: SimpleNamespace(get=get))
    store.add("600000.SH")
    with TestClient(app) as client:
        response = client.put("/api/watchlist/600000.SH/strategies", json={"strategy_ids": ["daily", "minute"]})
        assert response.status_code == 200
        assert response.json()["symbols"][0]["strategy_ids"] == ["daily", "minute"]
        rev = store.revision()
        for invalid in ["foreign", "draft"]:
            assert client.put("/api/watchlist/600000.SH/strategies",
                              json={"strategy_ids": [invalid]}).status_code == 404
        assert store.revision() == rev
        assert client.put("/api/watchlist/000001.SZ/strategies", json={"strategy_ids": []}).status_code == 404
        assert client.put("/api/watchlist/600000.SH/strategies", json={"strategy_ids": []}).json()[
            "symbols"][0]["strategy_ids"] == []
