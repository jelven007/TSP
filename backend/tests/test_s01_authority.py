"""S01 freezes both stages from one authority while retaining available-minute scoring."""
import math
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import strategy_input_binding as binding
from app.services import strategy_input_manifest as manifest
from app.services.strategy_input_authority import InputStore

SID = "custom_mvxxcs01"
DAY = "2026-11-25"
PREVIOUS = "2026-11-24"
BAR = datetime.fromisoformat(f"{DAY}T14:45:00")
SYMBOLS = ["600001.SH", "600002.SH"]
SIGNAL = {"id": "late_session_strength_s01", "template": "late_session_strength", "params": {
    "time": "14:45:00", "evaluation_delay_minutes": 1, "pool_up_min_pct": 65,
    "change_min_pct": 2, "change_max_pct": 3, "amplitude_max_pct": 5.5, "amount_min_100m": 1,
    "ma5_1m_count": None, "ma5_5m_count": None, "turnover_min_pct": None, "volume_ratio_min": None,
}}


def request():
    return {"strategy_id": SID, "as_of": DAY, "reference_date": PREVIOUS,
            "bar_at": BAR.isoformat(), "definition_sha256": "a" * 64,
            "algorithm_sha256": "b" * 64, "signal": SIGNAL,
            "retry_until": f"{DAY}T14:55:00+08:00", "pool": {"symbols": SYMBOLS},
            "selection": {"as_of": DAY, "source_date": PREVIOUS,
                          "rows": [{"symbol": s, "reference_close": 10.} for s in SYMBOLS]}}


def market_rows(symbols):
    return pl.DataFrame([
        {"symbol": s, "datetime": stamp, "open": 10., "high": 10.3, "low": 10.,
         "close": price, "amount": amount}
        for s in symbols for stamp, price, amount in (
            (BAR.replace(hour=9, minute=31), 10., 40e6),
            (BAR, 10.25 if s == SYMBOLS[0] else 10., 60e6),
            (BAR + timedelta(minutes=1), 10.25, 999e6))
    ])


def build(frame, phase, **kwargs):
    return manifest.build_manifest(
        request(), phase=phase, minutes=frame, references=dict.fromkeys(SYMBOLS, 10.),
        reference_errors={}, coverage={}, source_evidence={}, published_at=BAR.isoformat(), **kwargs)


def test_s01_partial_first_release_does_not_freeze_breadth():
    first = build(market_rows(SYMBOLS[:1]), "breadth", timing={"collector_completed_at": BAR.timestamp()})
    assert first["state"] == "waiting_input"
    assert first["coverage"][SYMBOLS[1]]["state"] == "pending"
    assert build(market_rows(SYMBOLS), "breadth")["state"] == "ready"


def test_s01_cumulative_stocks_keep_sparse_prefix_and_score_inputs():
    frame = market_rows(SYMBOLS)
    last = frame.filter((pl.col("symbol") == SYMBOLS[0]) & (pl.col("datetime") == BAR))
    value = build(pl.concat([frame, last.with_columns(pl.lit(61e6).alias("amount"))]), "stocks")
    assert value["state"] == "ready"
    rows = [r for r in value["input"]["minutes"] if r["symbol"] == SYMBOLS[0]]
    assert [r["datetime"] for r in rows] == [BAR.replace(hour=9, minute=31).isoformat(), BAR.isoformat()]
    assert sum(row["amount"] for row in rows) == 101e6
    assert all(row["open"] == 10. for row in rows)


def test_s01_worker_requests_both_phases_and_records_missing_mirror(monkeypatch):
    from app.strategy import basic_filter

    monkeypatch.setenv("TSP_S02_INPUT_AUTHORITY", "receiver")
    monkeypatch.setattr(binding, "cn_now", lambda: BAR.replace(tzinfo=CN_TZ))
    monkeypatch.setattr(binding, "algorithm_sha256", lambda: "b" * 64)
    monkeypatch.setattr(basic_filter, "previous_session", lambda day: day - timedelta(days=1))
    req = request()
    payload = {"strategy_task_ids": [SID], "strategy_task_plans": {SID: {"actions": [
        {"kind": "buy", "phase": p, "signal_id": SIGNAL["id"], "retry_until": req["retry_until"]}
        for p in ("breadth", "stocks")]}}, "daily_identities": {SID: dict.fromkeys((
            "strategy_version", "params_json", "overrides_json", "asset_type", "timeframe",
            "runtime_identity"), "same")}, "daily_pool_snapshots": {SID: req["pool"]},
        "selection_snapshots": {SID: req["selection"]},
        "strategy_task_windows": {SID: {"buy": [{"signal": SIGNAL, "start": "14:45:00"}]}}}
    calls = []

    def remote(value, phase):
        calls.append((value["strategy_id"], phase))
        return {"state": "waiting_input"}

    binding.prepare_inputs(payload, SimpleNamespace(s02_input=remote))
    assert calls == [(SID, "breadth"), (SID, "stocks")]
    assert all(s["state"] == "waiting_input" for s in payload["s02_input_status"][SID].values())


def test_s01_cannot_bind_another_signal_or_strategy():
    value = request()
    assert manifest.normalize_request(value)["strategy_id"] == SID
    with pytest.raises(ValueError, match="信号"):
        manifest.normalize_request({**value, "signal": {**SIGNAL, "id": "late_session_strength"}})


def test_s01_package_runs_actual_score_and_breadth_on_publisher_and_receiver(tmp_path, monkeypatch):
    from app.multitenancy import compute_worker, monitor_compute
    from app.services.strategy_input_mirror import install, package
    from app.services.strategy_input_source import verify_source
    from app.services.strategy_monitor_tasks import run_tasks, task_windows
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore

    now = (BAR + timedelta(minutes=1, seconds=5)).replace(tzinfo=CN_TZ)
    monkeypatch.setattr("app.market_time.cn_now", lambda: now)
    monkeypatch.setattr(binding, "cn_now", lambda: now)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: now)
    monkeypatch.setattr(binding, "algorithm_sha256", lambda: "b" * 64)
    monkeypatch.setattr(DataStore, "__init__", lambda *_a, **_kw: pytest.fail("opened local market"))
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("TSP_PACK_SCRIPT", str(root / "scripts/release/package_transfer.py"))
    home = tmp_path / "strategies"
    home.mkdir()
    (home / "s01.py").write_bytes((root / "examples/strategies/s01_signal_sections.py").read_bytes())
    engine = StrategyEngine([home])
    engine._user_data_dir = lambda: tmp_path
    sig = {**SIGNAL, "name": "s01", "kind": "entry", "timeframe": "intraday", "enabled": True,
           "conditions": [], "params": {**SIGNAL["params"], "end_time": "15:00:00"}}
    custom_signals.save_one(tmp_path, sig)
    overrides = {"entry_signals": ["csgi_" + sig["id"]], "exit_signals": []}
    saved = request()
    saved["signal"] = sig
    raw = [{"symbol": s, "date": PREVIOUS, "open": 10., "high": 10.1, "low": 9.9, "close": 10.}
           for s in SYMBOLS]
    factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64})
    evidence, errors = verify_source(saved, pl.DataFrame(raw), factors)
    evidence.update(reference_daily=raw, reference_errors=errors)
    origin, receiver = InputStore(tmp_path / "origin"), InputStore(tmp_path / "mirror")
    frames = {}
    for phase in ("breadth", "stocks"):
        value = manifest.build_manifest(saved, phase=phase, minutes=market_rows(SYMBOLS),
                                        references=dict.fromkeys(SYMBOLS, 10.), reference_errors=errors,
                                        coverage={}, source_evidence=evidence, published_at=now.isoformat())
        lead = origin.seal(value)
        key = manifest.common_key(saved)
        peer = install(receiver, package(origin, key, phase), key, phase)
        assert lead["input_sha256"] == peer["input_sha256"]
        frames[phase] = lead, peer

    for index in (0, 1):
        stock = frames["stocks"][index]
        context = binding.frozen_context(stock)
        context.signal_task_states = {}
        windows = task_windows(engine, engine.get(SID), overrides)
        result = run_tasks(engine, engine.get(SID), context, overrides, windows, None)
        step = result.monitor_tasks["buy"]["signals"][SIGNAL["id"]]["stocks"]
        assert step["state"] == "success", step["errors"]
        assert step["input_sha256"] == stock["input_sha256"]
        assert [row["symbol"] for row in step["rows"]] == [SYMBOLS[0]]
        assert step["rows"][0]["amount"] == 100e6
        assert step["rows"][0]["score"] == pytest.approx(-.03 + .001 * math.log(100e6))
        assert not result.monitor_tasks["buy"]["published"]
        breadth = frames["breadth"][index]
        task = {
            "kind": "monitor_breadth", "monitor_phase": "breadth", "s02_inputs_required": True,
            "strategy_task_ids": [SID], "state": {"engine": {}}, "sell_rosters": {}, "rules": [],
            "strategy_task_plans": {SID: {"actions": [{
                "kind": "buy", "signal_id": sig["id"], "phase": "breadth",
                "scheduled_at": now.isoformat(), "retry_until": saved["retry_until"]}]}},
            "strategy_task_windows": {SID: windows},
            "strategy_task_receipts": {SID: {}}, "strategy_task_results": {},
            "daily_pool_snapshots": {SID: saved["pool"]}, "daily_identities": {SID: {
                "asset_type": "stock", "timeframe": "1m", "runtime_identity": SID}},
            "selection_snapshots": {SID: saved["selection"]}, "late_task_scopes": {SID: SID},
            "strategy_input_manifests": {SID: {"breadth": breadth}},
        }
        step = (compute_worker.execute_request(task, tmp_path)["results"][SID]
                ["monitor_tasks"]["buy"]["signals"][sig["id"]]["breadth"])
        assert step["state"] == "rejected" and step["pool_up_pct"] == 50
        assert step["input_sha256"] == breadth["input_sha256"]
