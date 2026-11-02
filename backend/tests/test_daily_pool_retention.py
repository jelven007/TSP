"""A successful newer pool atomically replaces that strategy/day's old pool."""
from dataclasses import replace
from datetime import timedelta

import pytest
from test_daily_stock_pools import DAY, key, payload

from app.services.daily_stock_pools import DailyStockPools
from app.services.strategy_results import StrategyResults


def test_nine_success_keeps_one_pool_and_other_strategy_dates(tmp_path):
    store = DailyStockPools(tmp_path)
    eight = store.save(key(), payload(["600000.SH"], generation_slot="08:00"), started_at=10)
    other_day = store.save(key(), payload(["600008.SH"], as_of=str(DAY - timedelta(days=3))), started_at=11)
    other_key = replace(key(), strategy_id="other")
    other = store.save(other_key, payload(["600009.SH"], strategy_id="other"), started_at=12)
    nine = store.save(key(), payload([], generation_slot="09:00"), started_at=20)
    assert store.read("sample", DAY, record_id=eight["id"]) is None
    assert store.read("sample", DAY)["id"] == nine["id"]
    assert store.read("sample", DAY)["total"] == 0
    assert store.read("sample", DAY - timedelta(days=3))["id"] == other_day["id"]
    assert store.read("other", DAY)["id"] == other["id"]
    with StrategyResults(tmp_path)._local() as conn:
        assert conn.execute("SELECT count(*) FROM daily_stock_pools WHERE strategy_id='sample' AND day=?",
                            (str(DAY),)).fetchone()[0] == 1


def test_old_late_completion_cannot_restore_eight_after_nine(tmp_path):
    store = DailyStockPools(tmp_path)
    eight_payload = payload(["600000.SH"], generation_slot="08:00")
    eight = store.save(key(), eight_payload, started_at=10)
    nine = store.save(key(), payload(["600001.SH"], generation_slot="09:00"), started_at=20)
    assert store.save(key(), eight_payload, started_at=15)["id"] == nine["id"]
    assert store.read("sample", DAY, record_id=eight["id"]) is None
    assert store.read("sample", DAY)["symbols"] == ["600001.SH"]


def test_failed_save_retains_eight_and_replacement_rollback_is_atomic(tmp_path):
    store = DailyStockPools(tmp_path)
    eight = store.save(key(), payload(["600000.SH"], generation_slot="08:00"), started_at=10)
    with pytest.raises(ValueError, match="数量"):
        store.save(key(), payload([], total=1, generation_slot="09:00"), started_at=20)
    with pytest.raises(ValueError, match="rollback"), StrategyResults(tmp_path)._local(write=True) as conn:
        store.save_in_transaction(conn, key(), payload([], generation_slot="09:00"), started_at=20)
        raise ValueError("rollback")
    assert store.read("sample", DAY)["id"] == eight["id"]
