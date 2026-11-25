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
        from app.services.custom_status_refresh import custom_stock_ids, save_refresh, state_digest
        from app.services.daily_pool_schedule import SLOTS, batch_pool, due_slot
        from app.services.daily_stock_pools import DailyStockPools
        from app.services.strategy_results import capture_identities
        from app.strategy.config import list_overrides
        from app.strategy.daily_pool import DEFINITION_VERSION
        from app.strategy.engine import StrategyEngine

        now = as_cn_datetime(now)
        slot = due_slot(now)
        if self.db.settings.schema_version < 7 or slot is None:
            return
        attempt_start = time.monotonic()
        status = None
        if repo is not None:
            from app.services.announced_stock_status import morning_status
            status = morning_status(repo, now.date(), observed_at=now, slot=slot)
        day = str(now.date())
        key = f"{day}:{slot}"
        account, context = self.runtime.account(documents.context.tenant_id)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            previous = unit.get("daily_stock_pool_jobs", key, {})
            if slot == "09:00":
                earlier_key = f"{day}:{SLOTS[0]}"
                earlier = unit.get("daily_stock_pool_jobs", earlier_key, {})
                if earlier.get("state") not in {"succeeded", "interrupted"}:
                    unit.put("daily_stock_pool_jobs", earlier_key, {**earlier, "state": "interrupted",
                             "reason": "已错过08:30批次，生成最近09:00批次"})
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
                                "definition_version": DEFINITION_VERSION,
                                **({"status": state_digest(status)} if status else {})})
            successful = (previous.get("state") == "succeeded"
                          and previous.get("definition_version") == DEFINITION_VERSION
                          and (not status or previous.get("signature") == marker))
            completed = (set(previous.get("completed", []))
                         if successful or previous.get("signature") == marker else set())
            pending = [sid for sid in ids if sid not in completed]
            if not pending:
                return
            task = engine.task("daily_stock_pools", strategy_ids=pending, as_of=day,
                               overrides_map=overrides, premarket=True,
                               observed_at=f"{day}T{slot}:00",
                               **({"confirmed_status": status} if status else {}),
                               **({"public_release_id": release} if release else {}))
            unit.put("daily_stock_pool_jobs", key, {
                "signature": marker, "definition_version": DEFINITION_VERSION,
                "completed": sorted(completed), "state": "running",
                **({"stock_status": status} if status else {}),
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
            if status:
                from datetime import timedelta
                if (due_slot(now + timedelta(seconds=time.monotonic() - attempt_start)) != slot
                        or state_digest(morning_status(repo, now.date(), observed_at=now, slot=slot))
                        != state_digest(status)):
                    raise AccessError(409, "本轮股票状态或批次已更新，将重试最新批次")
            custom_ids = set(custom_stock_ids(engine))
            for sid, pool in response["pools"].items():
                if sid not in pending or pool.get("as_of") != day or pool.get("strategy_id") != sid:
                    raise ValueError("股票池结果身份不匹配")
                generation = {"public_release_id": release, "signature": marker}
                pool = batch_pool(pool, now.date(), slot)
                if status and sid in custom_ids:
                    save_refresh(unit, identities[sid], pool, status, started_at=started_at,
                                 input_generation=generation)
                else:
                    DailyStockPools(unit).save(identities[sid], pool, started_at=started_at,
                                              input_generation=generation)
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
            if status:
                append_event(unit.connection, documents.context.tenant_id, "strategy_results_updated",
                             {"as_of": day, "section": "select", "strategy_ids": sorted(completed),
                              "stock_status_refreshed": True})

    def evening_status(self, documents, snapshot, repo, now, claim):
        """Refresh every private custom stock scope without enabling monitoring."""
        from datetime import timedelta

        from app.multitenancy.runtime import fingerprint
        from app.services.announced_stock_status import confirmed_status
        from app.services.custom_status_refresh import (
            POLICY,
            custom_stock_ids,
            save_refresh,
            state_digest,
        )
        from app.services.daily_selections import due_window
        from app.services.strategy_results import capture_identities
        from app.strategy.config import list_overrides
        from app.strategy.engine import StrategyEngine

        attempt_start = time.monotonic()
        window = due_window(now, monitored_only=True)
        if window is None or self.db.settings.schema_version < 7:
            return
        _, target = window
        key = f"{target}:evening_status"
        status = confirmed_status(repo, target, observed_at=now)
        account, context = self.runtime.account(documents.context.tenant_id)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            observed = fingerprint(unit.connection, include_state=False)
            builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "strategy" / "builtin"])
            engine = TenantStrategyEngine(unit, builtins, self.executor,
                administrator="settings:manage" in account["permissions"])
            ids = custom_stock_ids(engine)
            if not ids:
                return
            overrides = list_overrides(unit)
            identities = capture_identities(engine, unit, ids, overrides_map=overrides, timeframe="all")
            release = snapshot.get("public_release", {}).get("id")
            marker = signature({"policy": POLICY, "input": observed, "release": release,
                                "status": state_digest(status), "day": str(target)})
            previous = unit.get("daily_stock_pool_jobs", key, {})
            if previous.get("signature") == marker and previous.get("state") == "succeeded":
                return
            started_at = time.time_ns()
            job = {"signature": marker, "state": "running", "status": status,
                   "started_at": started_at, "attempts": previous.get("attempts", 0) + 1}
            unit.put("daily_stock_pool_jobs", key, job)
            task = engine.task("daily_stock_pools", strategy_ids=ids, as_of=str(target),
                overrides_map=overrides, confirmed_status=status,
                **({"public_release_id": release} if release else {}))
        try:
            response = self.executor.execute(task)
        except Exception as exc:
            with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
                valid_claim(unit.connection, claim)
                unit.put("daily_stock_pool_jobs", key, {**job, "state": "failed",
                         "errors": {sid: f"股票状态更新失败: {exc}" for sid in ids}})
            raise
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            commit_now = now + timedelta(seconds=time.monotonic() - attempt_start)
            if (fingerprint(unit.connection, include_state=False) != observed
                    or due_window(commit_now, monitored_only=True) != window):
                raise AccessError(409, "股票池配置已更新，将重试状态刷新")
            errors = dict(response.get("errors", {}))
            completed = []
            for sid, pool in response.get("pools", {}).items():
                if sid not in ids or pool.get("as_of") != str(target) or pool.get("strategy_id") != sid:
                    raise ValueError("股票状态更新结果身份不匹配")
                if sid in errors:
                    continue
                save_refresh(unit, identities[sid], pool, status, started_at=started_at,
                             input_generation={"public_release_id": release, "signature": marker})
                completed.append(sid)
            errors.update({sid: "股票状态更新缺少结果，将重试" for sid in set(ids) - set(completed) - set(errors)})
            unit.put("daily_stock_pool_jobs", key, {**job,
                     "state": "failed" if errors else "succeeded", "completed": completed,
                     "errors": errors, "finished_at": time.time_ns()})
            append_event(unit.connection, documents.context.tenant_id, "strategy_results_updated",
                         {"as_of": str(target), "section": "select", "strategy_ids": completed,
                          "stock_status_refreshed": True})

    def daily_selections(self, documents, snapshot, repo, now, claim):
        """按各自时窗执行已启用策略预选, 状态刷新及专用资格组沿独立入口处理。"""
        if repo is not None:
            self.evening_status(documents, snapshot, repo, now, claim)
        from app.services.selection_clock import S03_ID
        for sid in (None, S03_ID):
            self._daily_selections_group(documents, snapshot, repo, now, claim, strategy_id=sid)

    def _daily_selections_group(self, documents, snapshot, repo, now, claim, *, strategy_id=None):
        from app.services.daily_selections import DailySelections, due_window
        from app.services.s03_watchlist import WatchlistSyncError
        from app.services.strategy_monitor_tasks import selection_receipts
        try:
            self._run_daily_selections_group(documents, snapshot, repo, now, claim, strategy_id=strategy_id)
        except WatchlistSyncError as exc:
            window = due_window(now, monitored_only=True, strategy_id=strategy_id)
            if window is None:
                raise
            account, context = self.runtime.account(documents.context.tenant_id)
            with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
                valid_claim(unit.connection, claim)
                store = DailySelections(unit)
                key = str(window[1]) + ":s03"
                failed = {**store.job(key), "source_date": str(window[0]), "as_of": str(window[1]),
                          "state": "failed", "completed": [], "errors": {strategy_id: str(exc)},
                          "watchlist_sync": {"state": "failed", "error": str(exc)},
                          "finished_at": time.time_ns()}
                store.job(key, failed)
                selection_receipts(unit, failed, now)

    def _run_daily_selections_group(self, documents, snapshot, repo, now, claim, *, strategy_id=None):
        """在调度租约下冻结输入并派发盘后资格, 返回后复核身份和窗口再归档。

        公共定版未就绪、修复在途、计算失败与过期分别记回执。
        每策略缺结果或无效快照视为失败, 有效策略可独立保存; 业务事务内
        联动归档、作业、任务回执及通知, 不把补数或计算返回当作保存成功。
        """
        from datetime import timedelta

        from app.market_time import published_trading_day
        from app.multitenancy.runtime import fingerprint
        from app.services.daily_selections import (
            DEFINITION_VERSION,
            DailySelections,
            due_window,
            signal_strategy,
            valid_selection,
        )
        from app.services.selection_clock import S03_ID, window_for
        from app.services.strategy_monitor_tasks import (
            monitored_ids,
            pending_selection_repairs,
            selection_receipts,
            selection_repairs,
            task_receipt,
        )
        from app.services.strategy_results import capture_identities
        from app.strategy.config import list_overrides
        from app.strategy.engine import StrategyEngine

        attempt_start = time.monotonic()
        start, end = window_for(strategy_id)
        expired = now.time().replace(tzinfo=None) > end and published_trading_day(now.date()) is True
        window = due_window(now.replace(hour=start.hour, minute=0, second=0, microsecond=0)
                            if expired else now, monitored_only=True, strategy_id=strategy_id)
        if window is None:
            return
        source, target = window
        account, context = self.runtime.account(documents.context.tenant_id)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "strategy" / "builtin"])
            engine = TenantStrategyEngine(unit, builtins, self.executor,
                administrator="settings:manage" in account["permissions"])
            store = DailySelections(unit)
            active = monitored_ids(unit)
            ids = [sid for sid, strategy in engine._strategies.items()
                   if sid in active and signal_strategy(strategy) and not strategy.meta.get("research_only")
                   and (sid == S03_ID) == (strategy_id == S03_ID)]
            from app.services.strategy_monitor_state import finished
            pending = [sid for sid in ids if store.read(sid, target) is None
                       and (not expired or not finished(
                           task_receipt(unit, sid, source).get("tasks", {}).get("select", {})))]
            if not pending:
                if strategy_id == S03_ID and S03_ID in ids and not expired:
                    from app.services.s03_watchlist import sync_archived_selection
                    selected = store.read(S03_ID, target)
                    receipt = sync_archived_selection(unit, selected, now)
                    if receipt:
                        key = str(target) + ":s03"
                        recovered = {**store.job(key), "source_date": str(source), "as_of": str(target),
                                     "state": "succeeded", "completed": [S03_ID], "errors": {},
                                     "selections": {S03_ID: selected}, "watchlist_sync": receipt}
                        store.job(key, recovered)
                        selection_receipts(unit, recovered, now)
                        append_event(unit.connection, documents.context.tenant_id, "strategy_results_updated",
                                     {"as_of": str(target), "section": "select", "strategy_ids": [S03_ID],
                                      "watchlist_changed": True})
                return
            key = str(target) + (":s03" if strategy_id == S03_ID else "")
            previous = store.job(key)
            job = {**previous, "source_date": str(source), "as_of": str(target),
                   "attempts": previous.get("attempts", 0) + 1, "started_at": time.time_ns()}
            generations = {sid: unit.get("strategy_monitoring", sid)["generation"] for sid in pending}
            if expired:
                expired_job = {**job, "state": "expired",
                    "errors": {sid: "盘后选股时段已结束, 尚无成功归档" for sid in pending}}
                store.job(key, expired_job)
                selection_receipts(unit, expired_job, now)
                return
            all_pending = set(pending)
            repairs = {sid: value for sid, value in (previous.get("repairs") or {}).items()
                       if sid in all_pending}
            blocked = pending_selection_repairs(self.market, repairs)
            pending = [sid for sid in pending if sid not in blocked]
            blocked_errors = {sid: (previous.get("errors") or {}).get(sid, "等待输入修复完成")
                              for sid in blocked}
            job["repairs"] = repairs
            if not pending:
                store.job(key, {**previous, "repairs": repairs})
                return
            observed = fingerprint(unit.connection, include_state=False)
            overrides = list_overrides(unit)
            identities = capture_identities(engine, unit, pending, overrides_map=overrides, timeframe="all")
            release = snapshot.get("public_release", {}).get("id")
            marker = signature({"input": observed, "source_date": str(source), "target": key,
                                "release": release, "definition_version": DEFINITION_VERSION})
            job = {**job, "signature": marker, "state": "running"}
            if strategy_id == S03_ID:
                from app.services.s03_selection import freeze_source
                from app.services.selection_clock import S02_ID
                try:
                    job["source_selection"] = previous.get("source_selection") or freeze_source(
                        store.read(S02_ID, target), source, target)
                except ValueError as exc:
                    waiting = {**job, "state": "waiting_data", "errors": {S03_ID: str(exc)}}
                    store.job(key, waiting)
                    selection_receipts(unit, waiting, now)
                    return
            store.job(key, job)
            task = engine.task("daily_selections", strategy_ids=pending, source_date=str(source),
                               as_of=str(target), overrides_map=overrides,
                               source_selection=job.get("source_selection"),
                               **({"public_release_id": release} if release else {}))
        if str(source) not in snapshot.get("settlement_days", []):
            errors = {sid: f"等待{source}盘后完整数据" for sid in pending}
            repairs = {**repairs, **selection_repairs(
                self.market, repo, context.tenant_id, generations, job, errors,
                engine=engine, storage=documents, overrides=overrides)}
            with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
                valid_claim(unit.connection, claim)
                if fingerprint(unit.connection, include_state=False) != observed:
                    raise AccessError(409, "预选配置已更新")
                waiting = {**job, "state": "waiting_data", "errors": {**blocked_errors, **errors}, "repairs": repairs}
                DailySelections(unit).job(key, waiting)
                selection_receipts(unit, waiting, now)
            return
        try:
            response = self.executor.execute(task)
        except Exception as exc:
            errors = {sid: str(exc) for sid in pending}
            repairs = {**repairs, **selection_repairs(
                self.market, repo, context.tenant_id, generations, job, errors,
                engine=engine, storage=documents, overrides=overrides)}
            with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
                valid_claim(unit.connection, claim)
                failed = {**job, "state": "failed", "finished_at": time.time_ns(),
                    "errors": {**blocked_errors, **errors}, "repairs": repairs}
                DailySelections(unit).job(key, failed)
                selection_receipts(unit, failed, now)
            return
        missing = set(pending) - set(response.get("selections", {})) - set(response.get("errors", {}))
        response["errors"] = {**response.get("errors", {}),
                             **{sid: "盘后预选结果缺失，将自动重试" for sid in missing}}
        for sid, selected in list(response.get("selections", {}).items()):
            if sid in pending and (sid in response["errors"] or not valid_selection(
                    selected, target, strategy_id=sid)):
                response["errors"].setdefault(sid, "盘后预选证据异常, 需要修复重试")
                del response["selections"][sid]
        repairs = {**repairs, **selection_repairs(
            self.market, repo, context.tenant_id, generations, job, response["errors"],
            engine=engine, storage=documents, overrides=overrides)}
        rejected = None
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            valid_claim(unit.connection, claim)
            store = DailySelections(unit)
            commit_now = getattr(self, "selection_now", lambda: now + timedelta(
                seconds=time.monotonic() - attempt_start))()
            if (fingerprint(unit.connection, include_state=False) != observed
                    or due_window(commit_now, monitored_only=True, strategy_id=strategy_id) != window):
                rejected = AccessError(409, "预选配置或任务窗口已更新，旧计算已丢弃")
                store.job(key, {**job, "state": "failed", "finished_at": time.time_ns(),
                               "errors": {sid: str(rejected) for sid in pending}})
            else:
                completed = set(ids) - all_pending
                watchlist_sync = None
                for sid, selected in response.get("selections", {}).items():
                    if sid not in pending or selected.get("as_of") != str(target) or selected.get("source_date") != str(source):
                        raise ValueError("盘后预选结果日期或策略不匹配")
                    store.save(identities[sid], selected, started_at=job["started_at"],
                               input_generation={"public_release_id": release, "signature": marker})
                    if sid == S03_ID:
                        from app.services.s03_watchlist import sync_archived_selection
                        watchlist_sync = sync_archived_selection(unit, store.read(sid, target), commit_now)
                    completed.add(sid)
                errors = {**blocked_errors, **response.get("errors", {}),
                          **{sid: "盘后预选结果缺失，将自动重试" for sid in set(pending) - completed
                             - set(response.get("errors", {}))}}
                finished = {**job, "state": "failed" if errors else "succeeded",
                               "completed": sorted(completed), "errors": errors, "finished_at": time.time_ns(),
                               "repairs": repairs, "selections": response.get("selections", {})}
                if watchlist_sync:
                    finished["watchlist_sync"] = watchlist_sync
                store.job(key, finished)
                selection_receipts(unit, finished, commit_now)
                append_event(unit.connection, documents.context.tenant_id, "strategy_results_updated",
                             {"as_of": str(target), "section": "select", "strategy_ids": sorted(completed),
                              **({"watchlist_changed": True} if watchlist_sync else {})})
        if rejected:
            raise rejected

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
            from app.services.strategy_monitor_tasks import retire_legacy
            await asyncio.to_thread(retire_legacy, documents)
            await asyncio.to_thread(self.daily_selections, documents, snapshot, repo, now, claim)
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
        await asyncio.to_thread(schedules.scheduler.resume_scheduled)
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
