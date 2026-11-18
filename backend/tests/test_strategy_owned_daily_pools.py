"""Each strategy consumes its own full scope snapshot before signal computation."""
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.daily_stock_pools import DailyStockPools, generate_scheduled_local
from app.services.strategy_results import ResultIdentity, execution_snapshot
from app.strategy.engine import StrategyDataContext, StrategyEngine

DAY = date(2026, 11, 11)
SYMBOLS = ["600001.SH", "600002.SH", "000001.SZ", "000002.SZ"]


def source(sid, board, *, minute=True):
    callback = (
        'def filter_minute_history(df, params):\n'
        '    return df.filter(pl.lit(df["symbol"].n_unique() == 2)).unique("symbol")\n'
        if minute else
        'def filter(df, params):\n'
        '    return pl.lit(df["symbol"].n_unique() == 2)\n'
    )
    return (
        'import polars as pl\n'
        f'META = {{"id": "{sid}", "name": "{sid}", "asset_types": ["stock"], '
        f'"timeframes": ["{"1m" if minute else "1d"}"], "limit": 0}}\n'
        + ('EXECUTION_BACKEND = "minute_filter"\n' if minute else '')
        + f'BASIC_FILTER = {{"enabled": False, "target_mode": "selected", "boards": ["{board}"]}}\n'
        + 'EXIT_SIGNALS = ["signal_test_exit"]\n'
        + callback
    )


def make_engine(tmp_path, *, minute=True):
    for sid, board in (("custom_mvxxcs01", "沪主板"), ("custom_mvxxcp88", "深主板")):
        (tmp_path / f"{sid}.py").write_text(source(sid, board, minute=minute))
    return StrategyEngine([tmp_path])


def context(*, minute=True):
    current = pl.DataFrame({
        "symbol": SYMBOLS, "date": [DAY] * 4, "close": [10.] * 4,
        "signal_test_exit": [False, False, True, False],
    })
    history = current.with_columns(pl.lit(datetime(2026, 11, 11, 14, 30)).alias("datetime"))
    return StrategyDataContext("stock", "1m" if minute else "1d", DAY,
                               current=current, history=history)


def pool(sid, symbols, **changes):
    return {"strategy_id": sid, "as_of": str(DAY), "symbols": symbols,
            "total": len(symbols), "definition_version": "scope_only_v3", **changes}


def identity(sid):
    return ResultIdentity(sid, sid, "v1", "{}", "{}", "null", "stock", "1m")


@pytest.mark.parametrize("minute", [True, False])
def test_scope_is_applied_before_strategy_signal_code(tmp_path, minute):
    engine = make_engine(tmp_path, minute=minute)
    shared = context(minute=minute)
    results = engine.run_all(shared, parallel=False)
    assert {row["symbol"] for row in results["custom_mvxxcs01"].rows} == set(SYMBOLS[:2])
    # One of the Shenzhen stocks has an exit signal, so only the other enters.
    assert {row["symbol"] for row in results["custom_mvxxcp88"].rows} == {SYMBOLS[3]}
    assert results["custom_mvxxcs01"].exit_signal_hits == []
    assert shared.current.height == shared.history.height == 4


def test_saved_pool_is_consumed_without_rebuilding_from_current_quotes(tmp_path):
    engine = make_engine(tmp_path)
    saved = pool("custom_mvxxcs01", SYMBOLS[:2])
    engine._daily_pool_loader = lambda sid, day: saved if sid == "custom_mvxxcs01" else None
    result = engine.run("custom_mvxxcs01", context())
    assert result.daily_stock_pool == saved
    assert {row["symbol"] for row in result.rows} == set(SYMBOLS[:2])


def test_empty_saved_pool_never_falls_back_to_full_market(tmp_path):
    engine = make_engine(tmp_path)
    saved = pool("custom_mvxxcs01", [])
    engine._daily_pool_loader = lambda *_: saved
    result = engine.run("custom_mvxxcs01", context())
    assert result.daily_stock_pool == saved
    assert result.rows == [] and result.exit_signal_hits == []


@pytest.mark.parametrize("changes", [
    {"strategy_id": "custom_mvxxcp88"}, {"as_of": "2026-11-10"}, {"total": 999},
])
def test_wrong_snapshot_identity_date_or_count_is_rejected(tmp_path, changes):
    engine = make_engine(tmp_path)
    engine._daily_pool_loader = lambda *_: pool("custom_mvxxcs01", SYMBOLS[:2], **changes)
    with pytest.raises(ValueError, match="股票池"):
        engine.run("custom_mvxxcs01", context())


def test_all_members_are_read_and_bound_to_local_execution_snapshot(tmp_path):
    engine = make_engine(tmp_path)
    symbols = [f"60{i:04d}.SH" for i in range(701)]
    store = DailyStockPools(tmp_path)
    saved = store.save(identity("custom_mvxxcs01"), pool("custom_mvxxcs01", symbols), started_at=10)
    store.save(identity("custom_mvxxcp88"), pool("custom_mvxxcp88", SYMBOLS[2:]), started_at=10)
    frozen = execution_snapshot(engine, tmp_path)
    result = frozen.run("custom_mvxxcs01", context())
    assert result.daily_stock_pool["id"] == saved["id"]
    assert result.daily_stock_pool["symbols"] == sorted(symbols)
    assert len(result.daily_stock_pool["symbols"]) == 701


def test_completed_batch_adds_new_strategy_without_regenerating_existing_pool(tmp_path):
    (tmp_path / "one.py").write_text(source("custom_mvxxcp88", "沪主板"))
    engine = StrategyEngine([tmp_path])
    service = SimpleNamespace(build_daily_pool_context=lambda *a, **k: context())
    now = datetime(2026, 11, 11, 9, 0)
    assert generate_scheduled_local(engine, service, tmp_path, now=now)["saved"] == ["custom_mvxxcp88"]
    before = DailyStockPools(tmp_path).read("custom_mvxxcp88", DAY)
    (tmp_path / "two.py").write_text(source("custom_mvxxcs01", "深主板"))
    engine.reload()
    assert generate_scheduled_local(engine, service, tmp_path, now=now)["saved"] == ["custom_mvxxcs01"]
    assert DailyStockPools(tmp_path).read("custom_mvxxcp88", DAY)["id"] == before["id"]
    assert DailyStockPools(tmp_path).read("custom_mvxxcs01", DAY)["symbols"] == SYMBOLS[2:]
    assert generate_scheduled_local(engine, service, tmp_path, now=now)["saved"] == []


def test_matrix_scope_isolated_without_mutating_shared_market():
    from app.backtest.matrix import build_market_data_matrix
    from app.strategy import daily_pool

    shared = context(minute=False)
    panel = shared.history.with_columns(
        pl.col("close").alias("open"), pl.col("close").alias("high"),
        pl.col("close").alias("low"), pl.lit(100.).alias("volume"),
    )
    market = build_market_data_matrix(panel, field_columns={"close"})
    scoped = daily_pool.scope_context(replace(shared, market=market), SYMBOLS[:2])
    assert scoped.market.symbols == tuple(SYMBOLS[:2])
    assert scoped.market.close.shape == (1, 2)
    assert scoped.market.valid_bars.shape == (1, 2)
    assert market.close.shape == (1, 4) and market.symbols == tuple(sorted(SYMBOLS))
    assert scoped.history["symbol"].to_list() == SYMBOLS[:2]


def test_archiving_consumed_pool_preserves_snapshot_identity(tmp_path):
    store = DailyStockPools(tmp_path)
    saved = store.save(identity("custom_mvxxcs01"),
                       pool("custom_mvxxcs01", SYMBOLS[:2]), started_at=10)
    repeated = store.save(identity("custom_mvxxcs01"), saved, started_at=20)
    assert repeated["id"] == saved["id"]
    assert store.snapshots(["custom_mvxxcs01"], DAY)["custom_mvxxcs01"]["id"] == saved["id"]


def test_holding_exit_survives_empty_entry_pool(tmp_path):
    from app.strategy import custom_signals

    engine = make_engine(tmp_path)
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, {
        "id": "morning", "name": "早盘清仓", "kind": "exit", "timeframe": "intraday",
        "template": "morning_liquidation", "conditions": [], "enabled": True,
        "params": {"time": "10:30:00", "amplitude_pct": 6},
    })
    shared = context()
    prior = shared.current.with_columns(pl.lit(date(2026, 11, 10)).alias("date"))
    shared = replace(shared, daily_history=prior, observed_at=datetime(2026, 11, 11, 14, 30),
                     positions=[{"symbol": SYMBOLS[2], "entry_date": "2026-11-10",
                                 "qty": 100, "available_qty": 100, "account_id": "one", "cycle_id": "lot"}])
    engine._daily_pool_loader = lambda *_: pool("custom_mvxxcs01", [])
    result = engine.run("custom_mvxxcs01", shared, overrides={"exit_signals": ["csgi_morning"]})
    assert result.rows == []
    assert [hit["symbol"] for hit in result.exit_signal_hits] == [SYMBOLS[2]]
    assert result.exit_signal_hits[0]["reason"] == "deadline"


@pytest.mark.parametrize("members", [[], [SYMBOLS[1]]])
def test_composite_result_respects_its_own_pool_and_child_scope(tmp_path, members):
    from test_composite_strategy import _composite_code

    make_engine(tmp_path, minute=False)
    (tmp_path / "blend.py").write_text(_composite_code(
        "blend", [("custom_mvxxcs01", 1), ("custom_mvxxcp88", 1)]))
    engine = StrategyEngine([tmp_path])
    engine._daily_pool_loader = lambda sid, day: pool(sid, members) if sid == "blend" else None
    result = engine.run("blend", context(minute=False))
    assert {row["symbol"] for row in result.rows} == set(members)
    assert result.daily_stock_pool["symbols"] == members


def test_compute_consumes_task_pool_instead_of_rebuilding(tmp_path, monkeypatch):
    from app.config import settings
    from app.multitenancy.compute_worker import execute
    from app.services.screener import ScreenerService

    shared = context()
    saved = pool("custom_mvxxcs01", [])
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path:
                        iter(()) if str(path) == "/market" else original(path))
    monkeypatch.setattr("app.tickflow.repository.DataStore", lambda *a, **kw:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr("app.tickflow.repository.KlineRepository", lambda _: None)
    monkeypatch.setattr(ScreenerService, "build_strategy_context", lambda *a, **kw: shared)
    response = execute({
        "kind": "strategy", "as_of": str(DAY), "timeframe": "1m",
        "strategy_ids": ["custom_mvxxcs01"],
        "sources": [{"id": "custom_mvxxcs01", "source": "custom",
                     "code": source("custom_mvxxcs01", "沪主板")}],
        "daily_pool_snapshots": {"custom_mvxxcs01": saved},
    }, tmp_path)
    assert response["results"]["custom_mvxxcs01"]["daily_stock_pool"] == saved
    assert response["results"]["custom_mvxxcs01"]["rows"] == []


def test_monitor_consumes_frozen_strategy_pool(tmp_path):
    from app.multitenancy.monitor_compute import evaluate_snapshot

    engine = make_engine(tmp_path)
    saved = pool("custom_mvxxcs01", [])
    evaluate_snapshot({
        "snapshot": {"status": {"is_trading_hours": False}}, "rules": [],
        "daily_pool_snapshots": {"custom_mvxxcs01": saved},
    }, None, engine, tmp_path)
    result = engine.run("custom_mvxxcs01", context())
    assert result.daily_stock_pool == saved and result.rows == []
