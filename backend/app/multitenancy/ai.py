"""Tenant AI execution, durable confirmations, and stream authorization."""
# ruff: noqa: RUF001 -- localized business messages.
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import date, timedelta

from app.cell.context import AccessError
from app.cell.passwords import token_hash
from app.market_time import cn_today
from app.multitenancy.access import verify_live_access
from app.multitenancy.documents import TenantDocuments, private_storage
from app.multitenancy.research import ResearchJobs
from app.services.ndjson_heartbeat import with_heartbeat
from app.services.task_state import SUCCESS_TASK_STATUSES, TERMINAL_TASK_STATUSES


def report_options(request):
    documents = private_storage(request)
    return {"documents": documents} if documents is not None else {}


async def connected_result(request, operation):
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.5)
            if not task.done() and await request.is_disconnected():
                raise AccessError(499, "请求已取消")
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def authorized_lines(request, events, *, heartbeat=None):
    """Keep the original NDJSON protocol; close the producer on revocation."""
    stream = (heartbeat or with_heartbeat)(events)
    try:
        async for line in stream:
            await asyncio.to_thread(verify_live_access, request)
            yield line + "\n"
    except AccessError as exc:
        yield json.dumps({"type": "error", "message": exc.code}, ensure_ascii=False) + "\n"
        yield '{"type":"done"}\n'
    finally:
        await stream.aclose()


class SQLPendingRegistry:
    """Confirmations belong to one user, permission version, and session."""

    def __init__(self, documents: TenantDocuments, account: dict, session: str, timeout_s=120.0):
        self.documents, self.timeout_s = documents, timeout_s
        self.owner = {"user_id": account["id"], "auth_version": account["auth_version"],
                      "session": token_hash(session)}

    async def register(self, call_id, name, args):
        from app.custom.assistant.actions import PendingAction

        def save():
            from psycopg.types.json import Jsonb

            now = time.time()
            value = {**self.owner, "name": name, "args": args, "decision": "pending",
                     "expires_at": now + self.timeout_s}
            with self.documents.db.business_transaction(self.documents.context, write=True) as conn:
                # Completed cards have no further purpose. Remove them only
                # when creating a new card, after their decision was consumed.
                conn.execute(
                    """DELETE FROM business.documents WHERE namespace='assistant_actions'
                       AND (value->>'expires_at')::numeric < %s""", (now,),
                )
                count = conn.execute(
                    """SELECT count(*) AS n FROM business.documents
                       WHERE namespace='assistant_actions' AND value->>'decision'='pending'"""
                ).fetchone()["n"]
                if count >= 20:
                    raise AccessError(429, "待确认操作过多，请先完成已有操作")
                conn.execute(
                    """INSERT INTO business.documents(tenant_id,namespace,id,value)
                       VALUES(%s,'assistant_actions',%s,%s)""",
                    (self.documents.context.tenant_id, call_id, Jsonb(value)),
                )
            return PendingAction(call_id=call_id, name=name, args=dict(args))

        return await asyncio.to_thread(save)

    async def resolve(self, call_id, approve):
        def decide():
            from psycopg.types.json import Jsonb

            with self.documents.db.business_transaction(self.documents.context, write=True) as conn:
                row = conn.execute(
                    """SELECT value FROM business.documents
                       WHERE namespace='assistant_actions' AND id=%s FOR UPDATE""", (call_id,),
                ).fetchone()
                value = row["value"] if row else None
                if (not value or any(value.get(key) != expected for key, expected in self.owner.items())
                        or value["decision"] != "pending" or value["expires_at"] <= time.time()):
                    return None
                value = {**value, "decision": "approved" if approve else "denied"}
                conn.execute(
                    """UPDATE business.documents SET value=%s,revision=revision+1,updated_at=now()
                       WHERE namespace='assistant_actions' AND id=%s""", (Jsonb(value), call_id),
                )
                return value

        return await asyncio.to_thread(decide)

    async def await_decision(self, action):
        try:
            while True:
                value = await asyncio.to_thread(self.documents.get, "assistant_actions", action.call_id)
                if not value or value["expires_at"] <= time.time():
                    return "expired"
                if value["decision"] != "pending":
                    return value["decision"]
                await asyncio.sleep(.25)
        finally:
            # Disconnects must also retire a pending card; a later click cannot
            # resurrect an action belonging to an abandoned chat.
            await asyncio.to_thread(self.documents.delete, "assistant_actions", action.call_id)


async def run_tool_backtest(
    engine, *, strategy_id, params=None, start=None, end=None, asset_type="stock", verify_access=None,
):
    from app.backtest.strategy import StrategyBacktestConfig
    from app.services.tool_catalog import _BACKTEST_STATS_KEYS, _DEFAULT_BACKTEST_DAYS

    if asset_type not in {"stock", "etf"}:
        raise AccessError(400, "资产类型错误")
    engine.get(strategy_id)
    end_date = date.fromisoformat(end) if end else cn_today()
    start_date = date.fromisoformat(start) if start else end_date - timedelta(days=_DEFAULT_BACKTEST_DAYS)
    if start_date > end_date:
        raise AccessError(400, "开始日期不能晚于结束日期")
    cfg = StrategyBacktestConfig(strategy_id=strategy_id, symbols=None, params=params, start=start_date,
                                 end=end_date, asset_type=asset_type)
    jobs = ResearchJobs(engine.documents)
    submission = asyncio.create_task(asyncio.to_thread(
        jobs.submit, engine.task("backtest", config=asdict(cfg)), force=True, source="ai",
    ))
    try:
        row = await asyncio.shield(submission)
    except asyncio.CancelledError:
        row = await submission
        await asyncio.to_thread(jobs.cancel, row["id"])
        raise
    finished = False
    try:
        deadline = time.monotonic() + 7300
        while time.monotonic() < deadline:
            if verify_access is not None:
                await asyncio.to_thread(verify_access)
            current = await asyncio.to_thread(jobs.get, row["id"])
            if current is None:
                raise AccessError(404, "研究任务不存在")
            if current["state"] in TERMINAL_TASK_STATUSES:
                finished = True
                if current["state"] not in SUCCESS_TASK_STATUSES:
                    raise AccessError(400, current["error"] or "任务已取消或中断")
                stats = current["result"].get("stats") or {}
                return {"strategy_id": strategy_id, "start": str(start_date), "end": str(end_date),
                        "stats": {key: stats[key] for key in _BACKTEST_STATS_KEYS if key in stats}}
            await asyncio.sleep(.5)
        raise AccessError(504, "研究任务等待超时")
    finally:
        if not finished:
            await asyncio.to_thread(jobs.cancel, row["id"])
