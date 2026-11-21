"""A credential-free sandbox consumes the worker's frozen task windows."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.market_time import CN_TZ
from app.services.strategy_monitor_tasks import run_tasks, set_monitoring, strategy_windows
from app.strategy.engine import StrategyDataContext

SID = "custom_test"
WINDOWS = {
    "buy": [{"start": "14:30:00", "end": "14:55:00", "signal": {
        "id": "late", "template": "late_session_strength",
        "params": {"time": "14:30:00", "end_time": "14:55:00",
                   "evaluation_delay_minutes": .5, "pool_up_min_pct": 60},
    }}],
    "sell": [{"start": "09:30:00", "end": "11:30:00", "signal": {
        "id": "morning", "template": "morning_liquidation",
        "params": {"start_time": "09:30:00", "time": "10:50:00",
                   "fallback_time": "11:30:00", "rise_pct": 6., "fall_pct": 6.},
    }}],
}


def test_worker_windows_survive_sandbox_without_private_lifecycle(tmp_path):
    strategy = SimpleNamespace(source="custom", meta={"id": SID})
    engine = SimpleNamespace(_monitor_task_loader=lambda sid: WINDOWS if sid == SID else None)
    assert not (tmp_path / "user_data/strategy_monitoring").exists()
    assert strategy_windows(engine, strategy, tmp_path, {}) == WINDOWS


@pytest.mark.parametrize("frozen", [None, {"buy": [], "sell": []}])
def test_absent_worker_windows_cannot_revive_local_enabled_monitor(tmp_path, frozen):
    set_monitoring(tmp_path, SID, "s02", True)
    strategy = SimpleNamespace(source="custom", meta={"id": SID})
    engine = SimpleNamespace(_monitor_task_loader=lambda _: frozen)
    assert strategy_windows(engine, strategy, tmp_path, {}) == {"buy": [], "sell": []}


def test_late_sandbox_task_expires_and_preserves_selection_without_fake_buys(tmp_path):
    strategy = SimpleNamespace(source="custom", meta={"id": SID})
    selected = [{"symbol": "600001.SH", "reference_date": "2026-11-19"}]
    engine = SimpleNamespace(
        _monitor_task_loader=lambda _: WINDOWS,
        _monitor_receipt_loader=lambda _: {"tasks": {
            kind: {"state": "unconfigured", "terminal": False, "windows": []}
            for kind in ("buy", "sell")}},
        _monitor_result_loader=lambda _: {
            "as_of": "2026-11-20", "rows": [], "total": 0,
            "sections": {"select": selected, "buy": [], "sell": []},
            "section_statuses": {"select": "success", "buy": "unconfigured", "sell": "unconfigured"},
            "section_errors": {"buy": "入场触发器未配置有效时段"},
            "entry_filter_error": "入场触发器未配置有效时段",
        },
    )
    context = StrategyDataContext("stock", "1m", datetime(2026, 11, 20).date(),
                                  observed_at=datetime(2026, 11, 20, 15, 25, tzinfo=CN_TZ))
    windows = strategy_windows(engine, strategy, tmp_path, {})
    result = run_tasks(engine, strategy, context, {}, windows, {"rows": []})
    assert result.section_statuses["buy"] == "expired"
    assert result.section_statuses["sell"] == "expired"
    assert all(result.monitor_tasks[kind]["terminal"] for kind in ("buy", "sell"))
    assert result.sections["select"] == selected
    assert result.sections["buy"] == []
    assert result.entry_signal_hits == []
    assert "未配置" not in (result.entry_filter_error or "")
    assert "未配置" not in result.section_errors.get("buy", "")
