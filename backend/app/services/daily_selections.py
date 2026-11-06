"""Durable post-close eligibility, separate from next-session minute entries."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import date
from datetime import time as clock_time

from app.cell.context import AccessError
from app.market_time import as_cn_datetime, cn_now, published_trading_days
from app.services.strategy_results import ResultIdentity, StrategyResults, _encode

DEFINITION_VERSION = "postclose_selection_v1"
_scheduled_lock = threading.Lock()


def signal_strategy(strategy):
    return (strategy.execution_backend == "minute_filter"
            and strategy.meta.get("result_sections") == "signals"
            and not strategy.meta.get("minute_execution"))


def due_window(now=None):
    """Recover only the latest post-close window, before its target session opens."""
    now = as_cn_datetime(now or cn_now())
    days = sorted(day for year in (now.year - 1, now.year, now.year + 1)
                  for day in (published_trading_days(year) or []))
    sources = [day for day in days if day < now.date()
               or (day == now.date() and now.time().replace(tzinfo=None) >= clock_time(16))]
    if not sources:
        return None
    source = sources[-1]
    target = next((day for day in days if day > source), None)
    if target is None or (now.date() >= target
                          and now.time().replace(tzinfo=None) >= clock_time(9, 30)):
        return None
    return source, target


def selection_identity(strategy_id):
    return ResultIdentity(strategy_id, "", "", "{}", "{}", "null", "stock", "1m", daily_record=True)


class DailySelections:
    def __init__(self, storage):
        self.results = StrategyResults(storage)

    def read(self, strategy_id, day):
        saved = self.results.detail(strategy_id, selection_identity(strategy_id).record_id(str(day)))
        return (saved or {}).get("result", {}).get("selection_snapshot")

    def save(self, identity, snapshot, *, started_at, input_generation=None):
        from app.strategy.basic_filter import previous_session
        target = date.fromisoformat(str(snapshot["as_of"]))
        if str(previous_session(target)) != str(snapshot["source_date"]):
            raise ValueError("预选源日期必须是目标日的精确前交易日")
        frozen = {**snapshot, "identity": identity.fingerprint,
                  "definition_version": DEFINITION_VERSION}
        reference = frozen.pop("price_suggestion_reference", None)
        return self.results.save(replace(identity, daily_record=True), {
            "as_of": str(target), "strategy_id": identity.strategy_id,
            "rows": [], "total": 0, "elapsed_ms": 0, "section_mode": "signals",
            "selection_only": True, "selection_snapshot": frozen,
            "price_suggestion_reference": reference,
            "sections": {"sell": [], "select": snapshot["rows"], "buy": []},
            "section_errors": {"buy": "仅已生成盘后选名单, 目标交易日买入信号尚未计算"},
        }, started_at=started_at, input_generation=input_generation)

    def job(self, key, value=None):
        if self.results.tenant:
            storage = self.results.storage
            return (storage.get("daily_selection_jobs", key, {}) if value is None
                    else storage.put("daily_selection_jobs", key, value))
        with self.results._local(write=value is not None) as conn:
            if conn is None:
                return {}
            if value is not None:
                conn.execute("CREATE TABLE IF NOT EXISTS daily_selection_jobs(key TEXT PRIMARY KEY,value TEXT)")
                conn.execute("INSERT INTO daily_selection_jobs VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _encode(value)))
                return value
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='daily_selection_jobs'").fetchone():
                return {}
            row = conn.execute("SELECT value FROM daily_selection_jobs WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}


def compute_selections(engine, service, source, target, ids, overrides, *, now=None):
    from app.services.daily_pool_schedule import scheduled_for
    from app.strategy.engine import _sanitize
    from app.strategy.signal_price_suggestions import price_reference
    from app.strategy.signal_sections import select_rows
    context = service.build_selection_context(engine, source, target, ids, overrides_map=overrides)
    reference = price_reference(context)
    results, errors = {}, {}
    for sid in ids:
        try:
            effective = {**overrides.get(sid, {}), "basic_filter": {
                **overrides.get(sid, {}).get("basic_filter", {}),
                "data_reference": "previous_trade_day",
            }}
            rows = _sanitize(select_rows(engine, engine.get(sid), context, overrides=effective))
            results[sid] = {"as_of": str(target), "source_date": str(source), "rows": rows,
                            "price_suggestion_reference": reference,
                            "generated_at": as_cn_datetime(now or cn_now()).isoformat(),
                            "scheduled_for": scheduled_for(source, "16:00")}
        except ValueError as exc:
            errors[sid] = str(exc)
    return {"selections": results, "errors": errors}


def generate_scheduled_local(engine, service, storage, *, now=None):
    from app.services.strategy_results import capture_identities, execution_snapshot
    from app.strategy.config import list_overrides
    from app.strategy.version import digest
    now = as_cn_datetime(now or cn_now())
    window = due_window(now)
    if window is None or not _scheduled_lock.acquire(blocking=False):
        return {"saved": [], "errors": {}}
    try:
        source, target = window
        store = DailySelections(storage)
        live_engine = engine
        engine = execution_snapshot(engine, storage)
        overrides = list_overrides(storage)
        ids = [sid for sid, strategy in engine._strategies.items() if signal_strategy(strategy)
               and not strategy.meta.get("research_only")]
        pending = [sid for sid in ids if store.read(sid, target) is None]
        if not pending:
            return {"saved": [], "errors": {}}
        identities = capture_identities(engine, storage, pending, overrides_map=overrides, timeframe="1m")
        marker = digest({sid: identity.fingerprint for sid, identity in identities.items()})
        job_key = str(target)
        old = store.job(job_key)
        started_at = time.time_ns()
        job = {**old, "source_date": str(source), "as_of": str(target), "signature": marker,
               "state": "running", "started_at": started_at, "attempts": old.get("attempts", 0) + 1}
        store.job(job_key, job)
        try:
            ready = json.loads((service.repo.store.data_dir / "settlement_ready.json").read_text())
            if ready.get("day") != str(source):
                raise ValueError(f"等待{source}盘后完整数据")
            response = compute_selections(engine, service, source, target, pending, overrides, now=now)
        except (OSError, ValueError) as exc:
            store.job(job_key, {**job, "state": "waiting_data", "errors": {
                sid: f"等待{source}盘后完整数据" for sid in pending}, "finished_at": time.time_ns()})
            return {"saved": [], "errors": {sid: str(exc) for sid in pending}}
        except Exception:
            errors = {sid: "盘后预选计算暂不可用, 将自动重试" for sid in pending}
            store.job(job_key, {**job, "state": "failed", "errors": errors,
                               "finished_at": time.time_ns()})
            return {"saved": [], "errors": errors}
        saved, errors = [], dict(response["errors"])
        for sid, selected in response["selections"].items():
            try:
                # Do not archive results computed against a changed user configuration.
                latest = capture_identities(execution_snapshot(live_engine, storage), storage, [sid],
                    overrides_map=list_overrides(storage), timeframe="1m")[sid]
                if latest.fingerprint != identities[sid].fingerprint:
                    raise ValueError("预选配置已更新, 将自动重试")
                store.save(identities[sid], selected, started_at=started_at)
                saved.append(sid)
            except (AccessError, KeyError, ValueError, RuntimeError) as exc:
                errors[sid] = str(exc)
        store.job(job_key, {**job, "state": "failed" if errors else "succeeded",
                           "completed": sorted(set(ids) - set(pending) | set(saved)),
                           "errors": errors, "finished_at": time.time_ns()})
        return {"saved": saved, "errors": errors}
    finally:
        _scheduled_lock.release()
