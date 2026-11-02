"""Tenant daily review and weekly mining, with persistent claims and fencing."""
# ruff: noqa: RUF001 -- localized business messages.
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.cell.database import Databases
from app.cell.scheduler import Scheduler
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import append_event
from app.multitenancy.executor import ComputeExecutor
from app.multitenancy.health import WorkerHealth
from app.multitenancy.market import PublicMarket, decode_frame
from app.multitenancy.quote_bridge import PublicDepthBridge
from app.multitenancy.research import ResearchJobs, authorize, signature
from app.multitenancy.runtime import FullWorker, valid_claim
from app.multitenancy.strategies import TenantStrategyEngine
from app.services import market_recap_reports
from app.services.mining_schedule import (
    _prerequisite_error,
    beijing_now,
    build_data_fingerprint,
    build_default_request,
    schedule_claim,
)

logger = logging.getLogger(__name__)


class TenantSchedules:
    def __init__(self, db, market, executor=None, review_stream=None):
        self.db, self.market = db, market
        self.executor = executor or ComputeExecutor()
        self.runtime = FullWorker(db, None, market, self.executor)
        self.identity = self.runtime.identity
        self.scheduler = Scheduler(db)
        self.health = WorkerHealth(db, "scheduled")
        self.ai_guard = review_stream is None
        if review_stream is None:
            from app.services.market_recap import recap_market_stream
            review_stream = recap_market_stream
        self.review_stream = review_stream

    @contextmanager
    def authorized(self, documents, schedule, namespace, revision, claim):
        context = replace(documents.context, user_id=schedule["user_id"],
                          policy_version=schedule["policy_version"], routing_epoch=schedule["routing_epoch"])
        with (authorize(self.identity, context, schedule["auth_version"]),
              documents.transaction() as unit):
            valid_claim(unit.connection, claim)
            row = unit.record(namespace, "default")
            if not row or row["revision"] != revision:
                raise AccessError(409, "定时任务配置已更新")
            yield unit

    def mining(self, documents, row, repo, now, claim):
        from app.factors.registry import private_factor_registry
        from app.strategy.engine import StrategyEngine

        schedule, revision = row["value"], row["revision"]
        if (not schedule.get("mining_schedule_enabled") or now.weekday() > 4
                or now.weekday() < schedule["mining_schedule_weekday"]):
            return
        with self.authorized(documents, schedule, "mining_schedule", revision, claim):
            pass
        context = replace(documents.context, user_id=schedule["user_id"])
        owned = TenantDocuments(self.db, context)
        builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "strategy" / "builtin"])
        engine = TenantStrategyEngine(owned, builtins, self.executor)
        marker = repo.store.data_dir / ".public_release.json"
        if marker.exists():
            engine.public_release_id = json.loads(marker.read_text())["id"]
        request = build_default_request(repo, schedule["mining_budget_profile"])
        with private_factor_registry(owned.values("custom_factors")):
            fingerprint = build_data_fingerprint(repo, type("MiningState", (), {"strategy_engine": engine})(), request)
        source_claim = schedule_claim(now.date())
        fingerprint.update(source="scheduled", source_claim=source_claim)
        payload = engine.task("mining", source_claim=source_claim, config={
            "run_id": source_claim, "request": request, "data_fingerprint": fingerprint, "source": "scheduled",
        })
        jobs = ResearchJobs(owned)
        error = _prerequisite_error(repo, request)
        jobs.submit(payload, key=signature({"source_claim": source_claim}), source="scheduled", claim=claim,
                    expected_version=schedule["auth_version"], expected_schedule=("mining_schedule", revision),
                    prerequisite_error=error)

    async def review(self, documents, row, repo, snapshot, now, claim):
        schedule, revision = row["value"], row["revision"]
        if not schedule.get("enabled") or (now.hour, now.minute) < (schedule["hour"], schedule["minute"]):
            return
        if self.ai_guard:
            from app.services.ai_provider import ai_configured
            if not ai_configured():
                return
        key = "review:" + now.date().isoformat()
        with self.authorized(documents, schedule, "review_schedule", revision, claim) as unit:
            existing = unit.get("scheduled_jobs", key, {})
            if existing.get("state") == "succeeded" or existing.get("attempts", 0) >= 3:
                return
            unit.put("scheduled_jobs", key, {"state": "running", "started_at": now.isoformat(),
                                           "attempts": existing.get("attempts", 0) + 1})
            append_event(unit.connection, documents.context.tenant_id, "review_progress", {"type": "retry"})
        from app.services.quote_service import QuoteService

        quotes = QuoteService()
        quotes.set_repo(repo)
        for asset, encoded in snapshot["frames"].items():
            repo.install_public_snapshot(asset, decode_frame(encoded))
        quotes._index_quotes_cache = decode_frame(snapshot["indices"])
        depth = PublicDepthBridge(repo)
        depth.update(snapshot.get("depth"))
        content, meta, done = [], {}, False
        progress_at = 0
        pending = []

        def progress(event):
            with self.authorized(documents, schedule, "review_schedule", revision, claim) as unit:
                append_event(unit.connection, documents.context.tenant_id, "review_progress", event)

        # No SQL transaction or permission lock stays open during model calls.
        async with asyncio.timeout(300):
            async for raw in self.review_stream(repo, quotes, depth):
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "error":
                    raise ValueError(event.get("message") or "复盘生成失败")
                if kind == "meta":
                    meta = event
                    await asyncio.to_thread(progress, event)
                elif kind == "delta" and event.get("content"):
                    content.append(event["content"])
                    pending.append(event["content"])
                    if time.monotonic() - progress_at > 1:
                        await asyncio.to_thread(progress, {"type": "delta", "content": "".join(pending)})
                        pending, progress_at = [], time.monotonic()
                elif kind == "done":
                    done = True
        if not done or not content:
            raise ValueError("复盘内容不完整")
        if pending:
            await asyncio.to_thread(progress, {"type": "delta", "content": "".join(pending)})
        with self.authorized(documents, schedule, "review_schedule", revision, claim) as unit:
            report = market_recap_reports.save_report({
                "as_of": meta.get("as_of") or now.date().isoformat(), "focus": "",
                "content": "".join(content), "summary": meta.get("summary", ""),
                "emotion_score": meta.get("emotion_score"), "emotion_label": meta.get("emotion_label", ""),
            }, documents=unit)
            old = unit.get("scheduled_jobs", key, {})
            unit.put("scheduled_jobs", key, {**old, "state": "succeeded", "report_id": report["id"],
                                           "finished_at": now.isoformat()})
            if unit.get("notification_preferences", "default", {}).get("review_push_mode") == "auto":
                unit.put("notification_requests", report["id"], {
                    "state": "queued", "report_id": report["id"], "content": report["content"],
                    "as_of": report["as_of"], "user_id": schedule["user_id"],
                    "auth_version": schedule["auth_version"],
                })
            append_event(unit.connection, documents.context.tenant_id, "review_progress",
                         {"type": "done", "archived": True})

    def daily_pools(self, documents, snapshot, repo, now, claim):
        """Two independent premarket snapshots, independent of current quotes/settlement."""
        from app.market_time import as_cn_datetime
        from app.multitenancy.runtime import fingerprint
        from app.services.daily_pool_schedule import batch_pool, due_slot
        from app.services.daily_stock_pools import DailyStockPools
        from app.services.strategy_results import capture_identities
        from app.strategy.config import list_overrides
        from app.strategy.daily_pool import DEFINITION_VERSION
        from app.strategy.engine import StrategyEngine

        now = as_cn_datetime(now)
        slot = due_slot(now)
        if self.db.settings.schema_version < 7 or slot is None:
            return
        day = str(now.date())
        key = f"{day}:{slot}"
        account, context = self.runtime.account(documents.context.tenant_id)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            previous = unit.get("daily_stock_pool_jobs", key, {})
            if previous.get("state") == "succeeded":
                return
            if slot == "09:00":
                earlier_key = f"{day}:08:00"
                earlier = unit.get("daily_stock_pool_jobs", earlier_key, {})
                if earlier.get("state") not in {"succeeded", "interrupted"}:
                    unit.put("daily_stock_pool_jobs", earlier_key, {**earlier, "state": "interrupted",
                             "reason": "已错过08:00批次，生成最近09:00批次"})
            observed = fingerprint(unit.connection, include_state=False)
            builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "strategy" / "builtin"])
            engine = TenantStrategyEngine(unit, builtins, self.executor,
                administrator="settings:manage" in account["permissions"])
            ids = [sid for sid, strategy in engine._strategies.items()
                   if not strategy.meta.get("research_only")
                   and "stock" in strategy.meta.get("asset_types", ["stock"])]
            overrides = list_overrides(unit)
            identities = capture_identities(engine, unit, ids, overrides_map=overrides, timeframe="all")
            release = snapshot.get("public_release", {}).get("id")
            marker = signature({"input": observed, "release": release, "day": day,
                                "slot": slot,
                                "definition_version": DEFINITION_VERSION})
            completed = set(previous.get("completed", [])) if previous.get("signature") == marker else set()
            pending = [sid for sid in ids if sid not in completed]
            task = engine.task("daily_stock_pools", strategy_ids=pending, as_of=day,
                               overrides_map=overrides, premarket=True,
                               **({"public_release_id": release} if release else {}))
            unit.put("daily_stock_pool_jobs", key, {
                "signature": marker, "completed": sorted(completed), "state": "running",
                "attempts": previous.get("attempts", 0) + 1, "started_at": time.time_ns(),
            })
        started_at = time.time_ns()
        try:
            response = self.executor.execute(task) if pending else {"pools": {}, "errors": {}}
        except Exception:
            with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
                valid_claim(unit.connection, claim)
                old = unit.get("daily_stock_pool_jobs", key, {})
                unit.put("daily_stock_pool_jobs", key, {**old, "state": "failed",
                         "errors": {sid: "股票池计算暂不可用，将自动重试" for sid in pending}})
            raise
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            if fingerprint(unit.connection, include_state=False) != observed:
                raise AccessError(409, "股票池配置已更新，将自动重新生成")
            for sid, pool in response["pools"].items():
                DailyStockPools(unit).save(identities[sid], batch_pool(pool, now.date(), slot), started_at=started_at,
                    input_generation={"public_release_id": release, "signature": marker})
                completed.add(sid)
            errors = response.get("errors", {})
            missing = set(pending) - completed - set(errors)
            errors = {**errors, **{sid: "股票池结果缺失，将自动重试" for sid in missing}}
            unit.put("daily_stock_pool_jobs", key, {
                **unit.get("daily_stock_pool_jobs", key, {}),
                "signature": marker, "completed": sorted(completed),
                "state": "failed" if errors else "succeeded",
                "errors": errors, "finished_at": time.time_ns(),
            })

    async def process(self, claim, snapshot, repo, *, now=None):
        now = beijing_now(now)
        from app.services.daily_pool_schedule import next_check
        due = next_check()
        try:
            _account, context = await asyncio.to_thread(self.runtime.account, claim["tenant_id"])
        except AccessError:
            await asyncio.to_thread(self.scheduler.finish, claim, due)
            raise
        documents = TenantDocuments(self.db, context)
        async def renew():
            while True:
                await asyncio.sleep(20)
                with self.db.data.connection() as conn:
                    valid = conn.execute(
                        "SELECT scheduler.renew_full(%s,%s,%s) AS valid",
                        (claim["tenant_id"], claim["kind"], claim["owner"]),
                    ).fetchone()["valid"]
                if not valid:
                    raise AccessError(409, "定时任务租约已过期")
                # Renewing a lease must not hide a stalled execution batch from
                # the container supervisor; only the outer loop proves progress.
                await asyncio.to_thread(self.health.record, progress=False)

        async def work():
            if self.db.settings.schema_version >= 7:
                await asyncio.to_thread(self.daily_pools, documents, snapshot, repo, now, claim)
            if now.date().isoformat() not in snapshot.get("settlement_days", []):
                return
            mining = await asyncio.to_thread(documents.record, "mining_schedule", "default")
            review = await asyncio.to_thread(documents.record, "review_schedule", "default")
            if mining:
                await asyncio.to_thread(self.mining, documents, mining, repo, now, claim)
            if review:
                try:
                    await self.review(documents, review, repo, snapshot, now, claim)
                except Exception as exc:
                    with self.authorized(documents, review["value"], "review_schedule", review["revision"], claim) as unit:
                        key = "review:" + now.date().isoformat()
                        old = unit.get("scheduled_jobs", key, {})
                        unit.put("scheduled_jobs", key, {**old, "state": "failed", "error": "复盘生成失败"})
                        append_event(unit.connection, context.tenant_id, "review_progress",
                                     {"type": "error", "message": "定时复盘生成失败，将自动重试"})
                    raise exc

        task, renewal = asyncio.create_task(work()), asyncio.create_task(renew())
        try:
            finished, _ = await asyncio.wait({task, renewal}, return_when=asyncio.FIRST_COMPLETED)
            for item in finished:
                await item
            with documents.transaction(write=False) as unit:
                review = unit.get("review_schedule", "default", {})
                mining = unit.get("mining_schedule", "default", {})
                if (self.db.settings.schema_version < 7
                        and not review.get("enabled") and not mining.get("mining_schedule_enabled")):
                    due = None
        finally:
            task.cancel()
            renewal.cancel()
            await asyncio.gather(task, renewal, return_exceptions=True)
            await asyncio.to_thread(self.scheduler.finish, claim, due)


async def run(db, market, stopped):
    from app.config import settings
    from app.tickflow.repository import DataStore, KlineRepository

    schedules = TenantSchedules(db, market)
    try:
        while not stopped.is_set():
            claims = await asyncio.to_thread(
                schedules.scheduler.claim, "scheduled", db.settings.worker_concurrency,
            )
            await asyncio.to_thread(schedules.health.record)
            if not claims:
                await asyncio.sleep(.5)
                continue
            try:
                snapshot = await asyncio.to_thread(market.snapshot)
                current = settings.data_dir.resolve()
                store = DataStore(current, read_only=True)
                try:
                    results = await asyncio.gather(
                        *(schedules.process(claim, snapshot, KlineRepository(store)) for claim in claims),
                        return_exceptions=True,
                    )
                    for result in results:
                        if isinstance(result, BaseException):
                            logger.warning("tenant schedule deferred: %s", type(result).__name__)
                finally:
                    store.db.close()
            except Exception:
                logger.exception("tenant scheduling dependencies unavailable")
                for claim in claims:
                    from app.services.daily_pool_schedule import next_check
                    schedules.scheduler.finish(claim, next_check())
    finally:
        schedules.runtime.close()


def main():
    logging.basicConfig(level=logging.INFO)
    db = Databases(CellSettings())
    db.open()
    market, stopped = PublicMarket(db.settings), asyncio.Event()
    async def start():
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except NotImplementedError:
                # Windows ProactorEventLoop has no add_signal_handler.
                signal.signal(signum, lambda *_: stopped.set())
        await run(db, market, stopped)
    try:
        asyncio.run(start())
    finally:
        market.close()
        db.close()


if __name__ == "__main__":
    main()
