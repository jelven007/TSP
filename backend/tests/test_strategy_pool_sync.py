"""Two browser clients share one authoritative pool, without changing monitor rules."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy
from app.cell.context import AccessError
from app.multitenancy.access import access_error


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.add_exception_handler(AccessError, access_error)
    app.include_router(strategy.router)
    def missing(_sid):
        raise ValueError("unknown strategy")
    monkeypatch.setattr(strategy, "_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(strategy, "_get_engine", lambda _: SimpleNamespace(
        get=missing, list_strategies=lambda **_: [{"id": "s01"}, {"id": "s02"}, {"id": "custom_existing"}]))
    return TestClient(app)


def put(client, ids, revision, migrate=False):
    return client.put("/api/strategies/pool", json={"ids": ids, "revision": revision, "migrate": migrate})


def test_two_clients_read_shared_pool_and_explicit_clear_survives_old_browser(client):
    initial = client.get("/api/strategies/pool")
    assert initial.status_code == 200
    assert initial.json() == {"ids": [], "revision": 0}
    saved = put(client, ["s01", "s02"], 0)
    assert saved.status_code == 200
    second = TestClient(client.app)
    assert second.get("/api/strategies/pool").json() == saved.json()
    cleared = put(second, [], saved.json()["revision"])
    assert cleared.status_code == 200
    assert put(client, ["s01"], 0, migrate=True).json() == cleared.json()
    assert client.get("/api/strategies/pool").json()["ids"] == []


def test_nonempty_legacy_lists_merge_until_explicit_edit_without_reordering_existing(client):
    first = put(client, ["s02"], 0, migrate=True)
    assert first.status_code == 200
    merged = put(client, ["s01", "s02", "custom_existing"], 0, migrate=True)
    assert merged.json()["ids"] == ["s02", "s01", "custom_existing"]
    edited = put(client, ["s01"], merged.json()["revision"])
    assert put(client, ["s02"], 0, migrate=True).json() == edited.json()


def test_empty_first_browser_does_not_freeze_migration_and_monitor_is_a_default(client, tmp_path):
    from app.strategy import monitor_rules

    monitor_rules.save_one(tmp_path, monitor_rules.normalize({
        "id": "active", "name": "active", "type": "strategy", "enabled": True,
        "strategy_id": "s02", "scope": "all",
    }))
    before = monitor_rules.load_all(tmp_path)
    assert client.get("/api/strategies/pool").json()["ids"] == ["s02"]
    result = put(client, ["s01"], 0, migrate=True)
    assert result.json()["ids"] == ["s02", "s01"]
    assert monitor_rules.load_all(tmp_path) == before


def test_parallel_edits_refuse_lost_updates(client):
    assert put(client, ["s01"], 0).status_code == 200
    with ThreadPoolExecutor(2) as threads:
        responses = list(threads.map(lambda ids: put(client, ids, 1), [["s02"], []]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    success = next(response.json() for response in responses if response.status_code == 200)
    assert client.get("/api/strategies/pool").json() == success


@pytest.mark.parametrize("ids", [["unknown"], ["../escape"], ["s01", "s01"], [None]])
def test_invalid_pool_does_not_overwrite_saved_value(client, ids):
    assert put(client, ["s01"], 0).status_code == 200
    assert put(client, ids, 1).status_code in (400, 404, 422)
    assert client.get("/api/strategies/pool").json()["ids"] == ["s01"]
