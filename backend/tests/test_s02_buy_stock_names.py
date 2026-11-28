"""Frozen selection names survive authoritative minute inputs and historical views."""
import gzip
import json
from copy import deepcopy

import pytest
from test_strategy_input_manifest import SYMBOLS, build, minutes, request

from app.services import strategy_input_binding, strategy_results


def test_bound_target_snapshot_carries_frozen_names_without_changing_minutes(monkeypatch):
    monkeypatch.setattr(strategy_input_binding, "algorithm_sha256", lambda: "b" * 64)
    req = request()
    for row, name in zip(req["selection"]["rows"], ("冻结名称甲", "冻结名称乙"), strict=True):
        row["name"] = name
    manifest = build(minutes(), value=req)
    before = deepcopy(manifest)
    context = strategy_input_binding.frozen_context(manifest)
    assert context.current["name"].to_list() == ["冻结名称甲", "冻结名称乙"]
    assert context.current["close"].to_list() == [10., 10.]
    assert set(context.history.columns) == {"symbol", "datetime", "close"}
    assert context.history.height == 80
    assert manifest == before
    from app.strategy.engine import StrategyEngine
    # The existing result join must now carry names into minute-only hits.
    engine = StrategyEngine([])
    rows = engine._join_basic_columns(context.current.drop("name"), context.current)
    assert set(rows["name"]) == {"冻结名称甲", "冻结名称乙"}


@pytest.mark.parametrize("existing", [None, ""])
def test_day_view_recovers_missing_buy_names_from_its_own_frozen_selection(tmp_path, existing):
    sid, day = "custom_mvxxcp88", "2026-11-27"
    store = strategy_results.StrategyResults(tmp_path)
    identity = strategy_results.ResultIdentity(sid, "s02", "v1", "{}", "{}", "null", "stock", "1m")
    buy = [{"symbol": SYMBOLS[0], "name": existing, "close": 9.51, "score": 24.48},
           {"symbol": SYMBOLS[1], "name": "已冻结买入名称", "close": 7.95, "score": 17.43},
           {"symbol": "UNKNOWN", "close": 7.83, "score": 7.47}]
    result = {"as_of": day, "rows": deepcopy(buy), "total": 3, "section_mode": "signals",
              "section_statuses": {"buy": "success"},
              "sections": {"sell": [], "select": [], "buy": deepcopy(buy)},
              "selection_snapshot": {"strategy_id": sid, "as_of": day, "source_date": "2026-11-26",
                  "rows": [{"symbol": SYMBOLS[0], "name": "冻结名称甲"},
                           {"symbol": SYMBOLS[1], "name": "预选名称乙"}]}}
    saved = store.save(identity, result, started_at=1)
    strategy_results.clear_memory()
    payload = json.loads(gzip.decompress(store.day_view(sid, day)[1]))
    expected = [{**buy[0], "name": "冻结名称甲"}, buy[1], buy[2]]
    assert payload["result"]["sections"]["buy"] == expected
    assert payload["result"]["rows"] == expected
    assert payload["result"]["total"] == 3
    assert store.detail(sid, saved["record_id"])["result"]["sections"]["buy"] == buy


def test_day_view_does_not_borrow_names_from_another_date(tmp_path):
    sid, day = "custom_mvxxcp88", "2026-11-27"
    store = strategy_results.StrategyResults(tmp_path)
    identity = strategy_results.ResultIdentity(sid, "s02", "v1", "{}", "{}", "null", "stock", "1m")
    row = {"symbol": SYMBOLS[0], "close": 9.51}
    store.save(identity, {"as_of": day, "rows": [row], "total": 1, "section_mode": "signals",
        "sections": {"sell": [], "select": [], "buy": [row]},
        "selection_snapshot": {"as_of": "2026-11-26", "rows": [
            {"symbol": SYMBOLS[0], "name": "另一日名称"}]}}, started_at=1)
    payload = json.loads(gzip.decompress(store.day_view(sid, day)[1]))
    assert payload["result"]["sections"]["buy"] == [row]
