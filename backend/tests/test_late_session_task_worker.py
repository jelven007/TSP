"""Retry receipts and warnings share real fenced PostgreSQL transactions."""
# ruff: noqa: F811 -- reuse the isolated full-product fixtures.
from datetime import datetime, time
from types import SimpleNamespace

import pytest
from test_full_runtime import env, full_cluster, owned  # noqa: F401
from test_late_session_optional_params import signal

from app.cell.context import AccessError
from app.market_time import CN_TZ, cn_today
from app.multitenancy.alerts import Alerts
from app.multitenancy.monitor_rules import Rules
from app.strategy.late_session_strength import evaluate
from app.strategy.monitor_rules import normalize


def setup(owned, monkeypatch):
    Rules(owned.documents).save_one(normalize({
        "id": "late", "name": "尾盘任务", "type": "strategy", "scope": "all",
        "strategy_id": "ma_golden_cross",
    }))
    owned.clock["now"] = datetime.combine(cn_today(), time(14, 30, 15), CN_TZ).timestamp()
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    sig["id"] = "late"
    original = owned.worker.prepare
    def prepare(*args):
        account, context, payload, observed = original(*args)
        payload.update(late_task_at=datetime.fromtimestamp(owned.clock["now"], CN_TZ).isoformat(),
                       late_task_strategy_ids=["ma_golden_cross"],
                       late_task_definitions={"ma_golden_cross": [sig]},
                       late_task_scopes={"ma_golden_cross": "ma_golden_cross:probe"})
        return account, context, payload, observed
    monkeypatch.setattr(owned.worker, "prepare", prepare)
    return sig


def test_same_stale_sequence_retries_and_error_warning_does_not_repeat(owned, monkeypatch):
    sig = setup(owned, monkeypatch)
    calls = []
    failed = evaluate(None, sig, day=cn_today(), pool_symbols=None, prev_close={},
                      completed_through=datetime.fromtimestamp(owned.clock["now"], CN_TZ))
    def execute(payload, **kwargs):
        calls.append(payload)
        return {"events": [], "state": {"engine": {"late_session_tasks": {"receipt": {"as_of": str(cn_today())}}}},
                "results": {"ma_golden_cross": {
                    "as_of": str(cn_today()), "rows": [], "total": 0,
                    "section_statuses": {"buy": "waiting"},
                    "entry_data_quality": {"csgi_late": {"progress": failed.progress}},
                }}}
    owned.worker.executor = SimpleNamespace(execute=execute)
    snapshot = owned.snapshot()
    owned.worker.evaluate(owned.documents.context.tenant_id, snapshot)
    assert len(calls) == 1 and Alerts(owned.documents).count() == 1
    owned.clock["now"] += 20
    owned.worker.evaluate(owned.documents.context.tenant_id, snapshot)
    assert len(calls) == 2 and calls[-1]["late_task_only"]
    assert Alerts(owned.documents).count() == 1
    assert Alerts(owned.other).count() == 0


def test_compute_exception_alert_and_retry_state_are_guarded(owned, monkeypatch):
    setup(owned, monkeypatch)
    owned.worker.executor = SimpleNamespace(execute=lambda *args, **kwargs:
        (_ for _ in ()).throw(ValueError("计算服务暂时不可用")))
    with pytest.raises(ValueError):
        owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert Alerts(owned.documents).count() == 1
    saved = owned.documents.get("monitor_runtime", "default")
    assert saved["engine"]["late_session_tasks"]
    from app.multitenancy import runtime
    monkeypatch.setattr(runtime, "valid_claim",
                        lambda *args: (_ for _ in ()).throw(AccessError(409, "expired")))
    with pytest.raises(ValueError):
        owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert owned.documents.get("monitor_runtime", "default") == saved
    assert Alerts(owned.documents).count() == 1


def test_real_schema_seven_prepares_generic_minute_stage_times(owned, tmp_path):
    from app.cell.admin import migrate
    from app.multitenancy.strategies import definition_payload
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine

    cluster = owned.env
    for kind, role in zip(("control", "data"), cluster["roles"], strict=True):
        migrate(cluster[kind], kind, role, "full", target_version=7)
    settings = owned.worker.db.settings
    owned.worker.db.settings = settings.model_copy(update={"schema_version": 7})
    source = ('META = {"id": "generic_late", "name": "测试", "asset_types": ["stock"], '
              '"timeframes": ["1m"], "execution_backend": "minute_filter"}\n'
              'ENTRY_SIGNALS = ["csgi_late"]\n'
              'def filter_minute_history(df, params):\n    return df\n')
    (tmp_path / "generic_late.py").write_text(source)
    parsed = StrategyEngine([tmp_path])
    owned.documents.put("strategy_sources", "generic_late", {
        "source": "custom", "code": source, "definition": definition_payload(parsed.get("generic_late")),
    })
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    sig.update(id="late", name="尾盘走强", kind="entry", timeframe="intraday", conditions=[])
    custom_signals.save_one(owned.documents, sig)
    Rules(owned.documents).save_one(normalize({
        "id": "late", "name": "尾盘任务", "type": "strategy", "scope": "all", "strategy_id": "generic_late",
    }))
    owned.clock["now"] = datetime.combine(cn_today(), time(14, 30), CN_TZ).timestamp()
    try:
        _, _, payload, _ = owned.worker.prepare(owned.documents.context.tenant_id, owned.snapshot())
        assert payload["late_task_strategy_ids"] == ["generic_late"]
        assert payload["late_task_definitions"]["generic_late"][0]["id"] == "late"
        assert datetime.fromisoformat(payload["late_task_at"]).time() == time(14, 30, 15)
        assert payload["late_task_scopes"]["generic_late"].startswith("generic_late:")
    finally:
        owned.worker.db.settings = settings
