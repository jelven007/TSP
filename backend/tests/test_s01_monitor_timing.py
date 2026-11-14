"""Real FullWorker preparation must wake an enabled S01 monitor at both stages."""
from datetime import datetime, time

import pytest
from test_full_daily_stock_pools import upgrade as upgrade
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_runtime import owned as owned
from test_s01_entry_timing import timed
from test_s01_optional_triggers import optional
from test_s01_strategy import BUILTINS

from app.market_time import CN_TZ, cn_today
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
from app.strategy import monitor_rules
from app.strategy.engine import StrategyEngine


@pytest.mark.skipif(not hasattr(TenantStrategyEngine, "_saved_pools"),
                    reason="requires the deployed schema-seven daily-pool adapter")
def test_enabled_optional_s01_monitor_wakes_at_environment_and_confirmation(owned):
    strategy = optional(StrategyEngine([BUILTINS]))
    sid = "custom_timing_s01"
    strategy.meta["id"] = sid
    assert "result_sections" not in strategy.meta
    owned.documents.put("strategy_sources", sid, {
        "source": "custom", "code": "", "definition": definition_payload(strategy)})
    signal = timed()
    owned.documents.put("custom_signals", signal["id"], signal)
    strategy.exit_signals = []
    source = owned.documents.get("strategy_sources", sid)
    source["definition"]["exit_signals"] = []
    owned.documents.put("strategy_sources", sid, source)
    rule = monitor_rules.normalize({
        "id": "timing_monitor", "name": "S01时钟", "type": "strategy", "strategy_id": sid,
        "enabled": True, "asset_type": "stock", "scope": "all", "symbols": [],
        "conditions": [], "direction": "both", "notify_events": ["buy_signal"],
    })
    Rules(owned.documents).save_one(rule)
    for now, expected in [(time(14, 44, 59), time(14, 45)), (time(14, 45), time(14, 46))]:
        owned.clock["now"] = datetime.combine(cn_today(), now, CN_TZ).timestamp()
        _account, _context, payload, _fingerprint = owned.worker.prepare(
            owned.documents.context.tenant_id, owned.snapshot())
        assert payload["next_evaluation_at"] == datetime.combine(cn_today(), expected, CN_TZ).isoformat()
