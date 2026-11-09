import gzip
import json
from dataclasses import replace

import pytest

from app.services import strategy_results


@pytest.fixture
def store(tmp_path):
    return strategy_results.StrategyResults(tmp_path)


@pytest.fixture
def identity():
    return strategy_results.ResultIdentity("sample", "样例", "v1", "{}", "{}", "null", "stock", "1m")


def signal(day="2026-11-05", **extra):
    return {"as_of": day, "total": 1, "rows": [{"symbol": "BUY"}], "section_mode": "signals",
            "sections": {"sell": [], "select": [{"symbol": "SELECT"}], "buy": [{"symbol": "BUY"}]},
            **extra}


def read(store, day="2026-11-05"):
    value = store.day_view("sample", day)
    return json.loads(gzip.decompress(value[1])) if value else None


def test_day_view_survives_cold_cache_and_preserves_full_saved_fields(store, identity):
    saved = store.save(identity, signal(), started_at=1)
    strategy_results.clear_memory()
    payload = read(store)
    assert payload["summary"] == saved["summary"]
    assert payload["result"]["sections"] == saved["result"]["sections"]
    assert payload["result"]["record_id"] == saved["record_id"]
    assert payload["result"]["strategy_version"] == "v1"
    assert store.day_view("other", "2026-11-05") is None
    assert store.day_view("sample", "2026-11-04") is None


def test_empty_scan_is_not_hidden_by_later_selection_only(store, identity):
    store.save(identity, signal(rows=[], total=0, sections={"sell": [], "select": [], "buy": []}), started_at=1)
    frozen = replace(identity, daily_record=True)
    store.save(frozen, signal(selection_only=True), started_at=2)
    assert read(store)["result"]["rows"] == []
    assert read(store)["result"].get("selection_only") is not True


def test_late_selection_is_materialized_without_mutating_scan(store, identity):
    original = store.save(identity, signal(), started_at=1)
    snapshot = {"source_date": "2026-11-04", "rows": [{"symbol": "LATE"}]}
    store.save(replace(identity, daily_record=True),
               signal(selection_only=True, selection_snapshot=snapshot), started_at=2)
    assert read(store)["result"]["sections"]["select"] == snapshot["rows"]
    assert store.detail("sample", original["record_id"])["result"]["sections"]["select"] == [{"symbol": "SELECT"}]


def test_delete_republishes_previous_version_and_deleting_strategy_removes_views(store, identity):
    first = store.save(identity, signal(), started_at=1)
    second = store.save(replace(identity, strategy_version="v2"), signal(), started_at=2)
    assert read(store)["summary"]["strategy_version"] == "v2"
    assert store.delete("sample", second["record_id"])
    assert read(store)["result"]["record_id"] == first["record_id"]
    with store.deleting_strategy("sample"):
        pass
    assert read(store) is None
    with pytest.raises(Exception, match=r"删除|重建"):
        store.save(identity, signal(), started_at=3)


def test_failed_view_publication_rolls_back_archive(store, identity, monkeypatch):
    from app.services import strategy_day_views
    original = store.save(identity, signal(), started_at=1)
    before = store.day_view("sample", "2026-11-05")
    def fail(*args):
        raise OSError("view write failed")
    monkeypatch.setattr(strategy_day_views, "publish", fail)
    with pytest.raises(OSError, match="view write failed"):
        store.save(identity, signal(rows=[]), started_at=2)
    assert store.detail("sample", original["record_id"]) == original
    assert store.day_view("sample", "2026-11-05") == before
