"""Current/next-session status projections over immutable selection evidence."""
from __future__ import annotations

import time
from datetime import date

import polars as pl

from app.services.strategy_results import StrategyResults
from app.strategy.version import digest

NAMESPACE = "custom_strategy_status"
POLICY = "confirmed_session_status_v1"


def custom_stock_ids(engine):
    return [sid for sid, strategy in engine._strategies.items()
            if getattr(strategy, "source", None) in ("custom", "ai")
            and not strategy.meta.get("research_only")
            and "stock" in strategy.meta.get("asset_types", ["stock"])]


def state_digest(status):
    return digest({key: status[key] for key in (
        "target_date", "suspended", "resumed", "risk_warning", "risk_removed",
        "scheduled_for", "cutoff", "finished_at", "coverage_complete", "pending_count", "errors")
        if key in status})


def compute_pools(engine, repo, target, ids, overrides, status):
    from app.price_limits import parse_listing_date
    from app.strategy.daily_pool import build_pool
    from app.strategy.engine import StrategyDataContext
    from app.strategy.suspension import COLUMN

    if status["target_date"] != str(target):
        raise ValueError("状态证据目标日不匹配")
    getter = getattr(repo, "get_instruments_asset", None)
    directory = getter("stock") if callable(getter) else None
    if directory is None or directory.is_empty():
        raise ValueError("状态更新缺少完整证券目录")
    if "listing_date" in directory.columns:
        directory = directory.filter(pl.Series([
            (listed := parse_listing_date(value)) is None or listed <= target
            for value in directory["listing_date"]]))
    directory = directory.select(column for column in directory.columns if column not in {
        "date", "is_suspended", COLUMN, "_suspension_date", "_suspension_status",
        "volume", "close", "raw_close"})
    symbol = pl.col("symbol")
    old_risk = (pl.col("is_risk_warning") if "is_risk_warning" in directory.columns
                else pl.col("name").str.contains(r"(?i)\*?ST").fill_null(False)
                if "name" in directory.columns else pl.lit(None, dtype=pl.Boolean))
    directory = directory.with_columns(
        pl.lit(target).alias("date"),
        pl.when(symbol.is_in(status["risk_warning"])).then(True)
        .when(symbol.is_in(status["risk_removed"])).then(False).otherwise(old_risk).alias("is_risk_warning"),
        pl.when(symbol.is_in(status["suspended"])).then(True)
        .when(symbol.is_in(status["resumed"])).then(False)
        .otherwise(pl.lit(None, dtype=pl.Boolean)).alias(COLUMN))
    context = StrategyDataContext("stock", "1d", target, current=directory)
    blocked = set(status["suspended"]) | set(status["risk_warning"])
    pools, errors = {}, {}
    for sid in ids:
        try:
            # Scope restoration is a directory operation, never a quote/formula filter.
            pool = build_pool(engine, sid, context, overrides=overrides.get(sid), premarket=True)
            symbols = sorted(set(pool["symbols"]) - blocked)
            pools[sid] = {**pool, "symbols": symbols, "total": len(symbols),
                          "status_refresh": {**status, "policy": POLICY,
                                             "excluded_symbols": sorted(blocked)}}
        except (ValueError, OSError) as exc:
            errors[sid] = str(exc)
    return {"pools": pools, "errors": errors}


def _read(store, conn, strategy_id, day):
    from app.services.strategy_day_views import _get, _key
    return _get(store, conn, NAMESPACE, _key(strategy_id, day, "stock", "1m")) or {}


def selection(store, strategy_id, snapshot, *, conn=None):
    if not snapshot or not snapshot.get("as_of") or not isinstance(snapshot.get("rows"), list):
        return snapshot
    if conn is None:
        transaction = store.storage._transaction if store.tenant else store._local
        with transaction() as opened:
            if opened is None:
                return snapshot
            return selection(store, strategy_id, snapshot, conn=opened)
    update = _read(store, conn, strategy_id, snapshot["as_of"])
    if not update:
        return snapshot
    excluded = set(update["removed_selection_symbols"])
    return {**snapshot, "rows": [row for row in snapshot["rows"] if row["symbol"] not in excluded],
            "status_refresh": update}


def result(store, strategy_id, value, *, conn=None):
    selected = selection(store, strategy_id, value.get("selection_snapshot"), conn=conn)
    if selected is None:
        return value
    return {**value, "selection_snapshot": selected,
            "sections": {**value["sections"], "select": selected["rows"]}}


def save_refresh(storage, identity, pool, status, *, started_at, input_generation=None):
    from app.services.daily_stock_pools import DailyStockPools
    from app.services.strategy_day_views import _key, _put, publish

    store = StrategyResults(storage)
    target = str(pool["as_of"])
    if status["target_date"] != target or date.fromisoformat(target) < date.fromisoformat(status["observed_at"][:10]):
        raise ValueError("实时股票状态只能刷新当日或下一交易日")
    transaction = store.storage._transaction if store.tenant else store._local
    with transaction(write=True) as conn:
        store._check_generation(conn, identity.strategy_id, identity.storage_generation)
        old = _read(store, conn, identity.strategy_id, target)
        if old.get("started_at", 0) > started_at:
            return old
        previous_pool = DailyStockPools(storage).snapshots([identity.strategy_id], target).get(identity.strategy_id, {})
        removed = set(old.get("removed_selection_symbols", [])) | set(status["suspended"]) | set(status["risk_warning"])
        update = {**status, "policy": POLICY, "started_at": started_at,
                  "removed_selection_symbols": sorted(removed),
                  "added_pool_symbols": sorted(set(pool["symbols"]) - set(previous_pool.get("symbols", []))),
                  "removed_pool_symbols": sorted(set(previous_pool.get("symbols", [])) - set(pool["symbols"])),
                  "pool_total": pool["total"], "input_generation": input_generation}
        DailyStockPools(storage).save_in_transaction(
            conn, identity, pool, started_at=started_at, input_generation=input_generation)
        _put(store, conn, NAMESPACE, _key(identity.strategy_id, target, "stock", "1m"), update)
        for timeframe in ("1d", "1m"):
            publish(store, conn, identity.strategy_id, target, timeframe=timeframe)
        return update


def generate_evening_local(engine, service, storage, now):
    from app.services.announced_stock_status import confirmed_status
    from app.services.daily_selections import due_window
    from app.services.daily_stock_pools import DailyStockPools
    from app.services.strategy_results import capture_identities, execution_snapshot
    from app.strategy.config import list_overrides

    window = due_window(now, monitored_only=True)
    if window is None:
        return {"saved": [], "errors": {}}
    _, target = window
    live_engine = engine
    engine = execution_snapshot(engine, storage)
    ids = custom_stock_ids(engine)
    if not ids:
        return {"saved": [], "errors": {}}
    overrides = list_overrides(storage)
    identities = capture_identities(engine, storage, ids, overrides_map=overrides, timeframe="all")
    status = confirmed_status(service.repo, target, observed_at=now)
    marker = digest({"policy": POLICY, "status": state_digest(status),
                     "identities": {sid: item.fingerprint for sid, item in identities.items()}})
    store = DailyStockPools(storage)
    key = f"{target}:evening_status"
    old = store.job(key)
    if old.get("signature") == marker and old.get("state") == "succeeded":
        return {"saved": [], "errors": {}}
    response = compute_pools(engine, service.repo, target, ids, overrides, status)
    started_at = time.time_ns()
    saved = []
    for sid, pool in response["pools"].items():
        latest = capture_identities(execution_snapshot(live_engine, storage), storage, [sid],
                                    overrides_map=list_overrides(storage), timeframe="all")[sid]
        if latest.fingerprint != identities[sid].fingerprint:
            response["errors"][sid] = "股票池配置已更新, 将重试状态刷新"
            continue
        save_refresh(storage, identities[sid], pool, status, started_at=started_at)
        saved.append(sid)
    store.job(key, {"signature": marker, "state": "failed" if response["errors"] else "succeeded",
                   "completed": saved, "errors": response["errors"], "status": status})
    return {"saved": saved, "errors": response["errors"]}
