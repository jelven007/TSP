"""Buy receipts publish an independent next-session signal roster atomically."""
import gzip
import json

import pytest

from app.services.strategy_results import ResultIdentity, StrategyResults


def identity():
    return ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")


def bought(rows):
    return {"as_of": "2026-10-30", "section_mode": "signals", "rows": rows, "total": len(rows),
            "sections": {"sell": [], "select": [], "buy": rows},
            "section_statuses": {"buy": "success"}}


@pytest.mark.parametrize("rows", [[], [{"symbol": "600001.SH", "close": 10.}]])
def test_buy_immediately_publishes_next_sell_without_inventing_buy_or_selection(tmp_path, rows):
    from app.services.strategy_day_views import read
    from app.services.strategy_monitor_tasks import sell_roster
    store = StrategyResults(tmp_path)
    saved = store.save(identity(), bought(rows), started_at=1)
    roster = sell_roster(tmp_path, "custom_test", "2026-11-02")
    assert roster["source_date"] == "2026-10-30"
    assert roster["source_record_id"] == saved["record_id"]
    assert [r["symbol"] for r in roster["rows"]] == [r["symbol"] for r in rows]
    view = json.loads(gzip.decompress(read(store, "custom_test", "2026-11-02")[1]))["result"]
    assert view["sections"]["sell"] == roster["rows"]
    assert view["section_statuses"]["buy"] == "waiting"
    assert view["section_statuses"]["select"] == "waiting"
    assert "selection_snapshot" not in view
    assert all("qty" not in row and "available_qty" not in row for row in roster["rows"])
    store.save(identity(), bought(rows), started_at=2)
    assert sell_roster(tmp_path, "custom_test", "2026-11-02") == roster


def test_waiting_buy_cannot_publish_false_empty_roster(tmp_path):
    from app.services.strategy_monitor_tasks import sell_roster
    StrategyResults(tmp_path).save(identity(), {
        **bought([]), "section_errors": {"buy": "waiting"}, "section_statuses": {"buy": "waiting"},
    }, started_at=1)
    assert sell_roster(tmp_path, "custom_test", "2026-11-02") is None


def test_deleting_source_receipt_removes_its_forward_copy(tmp_path):
    from app.services.strategy_monitor_tasks import sell_roster
    store = StrategyResults(tmp_path)
    saved = store.save(identity(), bought([{"symbol": "600001.SH"}]), started_at=1)
    store.delete("custom_test", saved["record_id"])
    assert sell_roster(tmp_path, "custom_test", "2026-11-02") is None
    assert store.day_view("custom_test", "2026-11-02") is None


def test_roster_keeps_frozen_next_selection_and_both_writes_roll_back(tmp_path, monkeypatch):
    from app.services import strategy_day_views as views
    from app.services.daily_selections import DailySelections
    from app.services.strategy_monitor_tasks import sell_roster
    store = StrategyResults(tmp_path)
    selection = {"as_of": "2026-11-02", "source_date": "2026-10-30", "rows": [{"symbol": "OTHER"}]}
    DailySelections(tmp_path).save(identity(), selection, started_at=1)
    original = views._put
    def fail(store, conn, namespace, key, value):
        original(store, conn, namespace, key, value)
        if namespace == "strategy_sell_rosters":
            raise RuntimeError("forward write failed")
    monkeypatch.setattr(views, "_put", fail)
    with pytest.raises(RuntimeError, match="forward"):
        store.save(identity(), bought([{"symbol": "600001.SH"}]), started_at=2)
    assert store.read(identity(), "2026-10-30") is None
    assert sell_roster(tmp_path, "custom_test", "2026-11-02") is None
    monkeypatch.setattr(views, "_put", original)
    store.save(identity(), bought([{"symbol": "600001.SH"}]), started_at=2)
    view = json.loads(gzip.decompress(views.read(store, "custom_test", "2026-11-02")[1]))["result"]
    assert [r["symbol"] for r in view["sections"]["sell"]] == ["600001.SH"]
    assert view["sections"]["select"] == selection["rows"]
