"""Live eligibility projections retain original evidence and complete own scopes."""
import gzip
import json
from datetime import date, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.services.daily_selections import DailySelections
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.engine import StrategyEngine

CN = ZoneInfo("Asia/Shanghai")
SOURCE = date(2026, 10, 30)
DAY = date(2026, 11, 2)


def identity():
    return ResultIdentity("custom_test", "Test", "v1", "{}", "{}", "null", "stock", "1m")


def status():
    return {"target_date": str(DAY), "observed_at": "2026-10-30T21:00:00+08:00",
            "suspended": ["600001.SH"], "resumed": ["600002.SH", "000001.SZ", "600004.SH"],
            "risk_warning": ["600003.SH", "600004.SH"], "risk_removed": ["600005.SH"],
            "evidence_digest": "test"}


def test_ordinary_postclose_window_is_21_to_22():
    from app.services.selection_clock import window_for
    assert window_for("custom_test") == (time(21), time(22))


def test_confirmed_status_overrides_directory_without_numeric_or_outside_scope(tmp_path):
    from app.services import custom_status_refresh as refresh
    source = tmp_path / "test.py"
    source.write_text('''
import polars as pl
META = {"id": "custom_test", "name": "Test", "asset_types": ["stock"], "limit": 1}
BASIC_FILTER = {"target_mode": "selected", "boards": ["沪主板"],
                "exclude_st": True, "exclude_suspended": True, "price_max": 1}
def filter(df, params):
    return pl.lit(False)
''')
    engine = StrategyEngine([tmp_path])
    directory = pl.DataFrame([
        {"symbol": symbol, "name": name, "is_risk_warning": risk}
        for symbol, name, risk in [
            ("600000.SH", "普通", False), ("600001.SH", "停牌", False),
            ("600002.SH", "复牌", False), ("600003.SH", "普通", False),
            ("600004.SH", "ST复牌", True), ("600005.SH", "ST旧名", True),
            ("000001.SZ", "范围外复牌", False),
        ]
    ])
    repo = SimpleNamespace(get_instruments_asset=lambda _: directory)
    pool = refresh.compute_pools(engine, repo, DAY, ["custom_test"], {}, status())["pools"]["custom_test"]
    assert pool["symbols"] == ["600000.SH", "600002.SH", "600005.SH"]
    assert pool["total"] == 3
    assert pool["status_refresh"]["excluded_symbols"] == [
        "600001.SH", "600003.SH", "600004.SH"]


def test_pruning_is_durable_changes_view_token_and_never_readds_or_rewrites_evidence(tmp_path):
    from app.services import custom_status_refresh as refresh
    original = {"as_of": str(DAY), "source_date": str(SOURCE),
                "rows": [{"symbol": symbol} for symbol in ["600000.SH", "600001.SH", "600003.SH"]],
                "generated_at": "2026-10-30T21:01:00+08:00"}
    selected = DailySelections(tmp_path).save(identity(), original, started_at=1)
    store = StrategyResults(tmp_path)
    before = store.day_view("custom_test", DAY)
    refresh.save_refresh(tmp_path, identity(), {
        "strategy_id": "custom_test", "as_of": str(DAY), "symbols": ["600000.SH"], "total": 1,
        "status_refresh": status()}, status(), started_at=2)
    after = store.day_view("custom_test", DAY)
    assert before[0] != after[0]
    assert json.loads(gzip.decompress(after[1]))["result"]["sections"]["select"] == [
        {"symbol": "600000.SH"}]
    assert DailySelections(tmp_path).read("custom_test", DAY)["rows"] == [{"symbol": "600000.SH"}]
    assert store.read(identity(), str(DAY))["result"]["sections"]["select"] == [{"symbol": "600000.SH"}]
    assert store.detail("custom_test", selected["record_id"]) == selected
    resumed = {**status(), "suspended": [], "risk_warning": [],
               "resumed": ["600001.SH"], "risk_removed": ["600003.SH"],
               "observed_at": "2026-11-02T08:30:00+08:00"}
    refresh.save_refresh(tmp_path, identity(), {
        "strategy_id": "custom_test", "as_of": str(DAY),
        "symbols": ["600000.SH", "600001.SH", "600003.SH"], "total": 3}, resumed, started_at=3)
    assert DailySelections(tmp_path).read("custom_test", DAY)["rows"] == [{"symbol": "600000.SH"}]
    assert store.detail("custom_test", selected["record_id"]) == selected
    other = date(2026, 11, 3)
    DailySelections(tmp_path).save(identity(), {**original, "as_of": str(other),
                                               "source_date": str(DAY)}, started_at=4)
    assert len(DailySelections(tmp_path).read("custom_test", other)["rows"]) == 3


def test_full_scheduler_has_independent_status_refresh_before_selection():
    from app.multitenancy.scheduled_worker import TenantSchedules
    assert callable(getattr(TenantSchedules, "evening_status", None)), (
        "Full has no per-custom-strategy next-session status task")


@pytest.mark.parametrize("as_of, observed", [
    ("2026-10-30", "2026-11-02T08:30:00+08:00"),
    ("2026-11-03", "2026-11-02T08:30:00+08:00"),
])
def test_status_date_mismatch_or_historical_mutation_is_rejected(tmp_path, as_of, observed):
    from app.services.custom_status_refresh import save_refresh
    with pytest.raises(ValueError):
        save_refresh(tmp_path, identity(), {"strategy_id": "custom_test", "as_of": as_of,
                     "symbols": [], "total": 0}, {**status(), "observed_at": observed}, started_at=1)
