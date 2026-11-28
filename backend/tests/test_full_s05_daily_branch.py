"""Tenant-private integer branch persistence and rollback."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixture reuse.
import gzip
import json

import pytest
from test_full_identity import env, full_cluster
from test_full_schedules import own
from test_s05_daily_branch import DAY, selection

from app.services.daily_selections import DailySelections
from app.services.s05_selection import NUMERIC_POLICY, POLICY, WEIGHTS
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.s02_173004552 import SID


def test_integer_branch_is_private_atomic_and_stored_in_next_day_selection(env):
    _, documents = own(env)
    _, other = own(env)
    snapshot = selection(4)
    snapshot.update(scoring_policy=POLICY, scoring_weights=WEIGHTS)
    identity = ResultIdentity(SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    with pytest.raises(ValueError), documents.transaction() as unit:
        DailySelections(unit).save(identity, snapshot, started_at=1)
        raise ValueError("rollback")
    assert DailySelections(documents).read(SID, DAY, policy=NUMERIC_POLICY) is None
    assert DailySelections(other).read(SID, DAY, policy=NUMERIC_POLICY) is None
    saved = DailySelections(documents).save(identity, snapshot, started_at=2)
    archived = StrategyResults(documents).detail(SID, saved["record_id"])
    assert type(archived["result"]["selection_snapshot"]["branch_code"]) is int
    assert archived["result"]["selection_snapshot"]["branch_code"] == 4
    view = json.loads(gzip.decompress(StrategyResults(documents).day_view(SID, str(DAY))[1]))["result"]
    assert view["selection_snapshot"]["as_of"] == "2026-11-27"
    assert view["selection_snapshot"]["source_date"] == "2026-11-26"
    assert view["monitor_tasks"]["buy"]["executed"] is False
    assert "sell" not in view["monitor_tasks"]


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_common_branch_archive_and_enabled_monitor_schedule_are_tenant_private(env, code, monkeypatch):
    from datetime import datetime

    from test_s05_common_daily_branch import common_selection, common_strategy, engine, signals

    from app.market_time import CN_TZ
    from app.services.routed_strategy_monitor import monitoring_summary
    from app.services.s05_selection import COMMON_BRANCH_POLICY
    from app.services.strategy_monitor_tasks import set_monitoring
    from app.strategy.custom_signals import private_signal_registry

    _, documents = own(env)
    _, other = own(env)
    archived = datetime(2026, 11, 26, 22, 16, tzinfo=CN_TZ)
    monkeypatch.setattr("app.services.strategy_results.time.time", archived.timestamp)
    snapshot = {**common_selection(code), "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
                "generated_at": archived.isoformat()}
    identity = ResultIdentity(SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    with pytest.raises(ValueError), documents.transaction() as unit:
        DailySelections(unit).save(identity, snapshot, started_at=1)
        raise ValueError("rollback")
    assert DailySelections(documents).read(SID, DAY, policy=COMMON_BRANCH_POLICY) is None
    DailySelections(documents).save(identity, snapshot, started_at=2)
    assert DailySelections(other).read(SID, DAY, policy=COMMON_BRANCH_POLICY) is None
    set_monitoring(documents, SID, "s05", True)
    runtime, strategy = engine(), common_strategy()
    runtime._strategies = {SID: strategy}
    runtime.get = lambda *args: strategy
    runtime.has = lambda sid: sid == SID
    with private_signal_registry(signals()):
        row = monitoring_summary(runtime, documents, now=datetime(2026, 11, 27, 10, tzinfo=CN_TZ))["strategies"][0]
    assert row["enabled"] and row["branch_code"] == code
    assert row["tasks"]["sell"]["windows"] == []
    assert row["tasks"]["sell"]["state"] == "waiting"
    assert DailySelections(documents).read(SID, DAY)["branch_code"] == code
    assert DailySelections(documents).read(SID, DAY, live_entry=True)["branch_code"] == code
