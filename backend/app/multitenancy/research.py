"""Durable full-engine jobs, fenced leases, and tenant-owned research artifacts."""
# ruff: noqa: RUF001 -- localized business messages.
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import polars as pl
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.cell.context import ROLE_PERMISSIONS, AccessError, Role, TenantContext
from app.multitenancy.documents import TenantDocuments, json_value, private_storage
from app.services.task_state import (
    ACTIVE_TASK_STATUSES,
    SUCCESS_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
)

if TYPE_CHECKING:
    from app.multitenancy.identity import AccountIdentity

LEASE_SECONDS = 30
MAX_JOBS = 200
MAX_EVENTS = 256


def signature(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        json_value(value), sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def read_frame(data: bytes, *, max_rows=1_000_000, max_uncompressed=128 * 1024 * 1024) -> pl.DataFrame:
    import pyarrow.parquet as pq

    if not data or len(data) > 64 * 1024 * 1024:
        raise ValueError("research artifact exceeds its size limit")
    parquet = pq.ParquetFile(io.BytesIO(data))
    metadata = parquet.metadata
    if (metadata.num_rows > max_rows or metadata.num_columns > 256
            or sum(metadata.row_group(index).total_byte_size for index in range(metadata.num_row_groups))
            > max_uncompressed):
        raise ValueError("research artifact exceeds its expanded size limit")
    return pl.read_parquet(io.BytesIO(data))


@contextmanager
def authorize(identity: AccountIdentity, context: TenantContext, version: int) -> Iterator[TenantContext]:
    """Lock permission versions across a claim/commit, never across computation."""
    with identity.db.control.connection() as conn, conn.transaction():
        user = conn.execute(
            "SELECT * FROM control.users WHERE id=%s FOR SHARE", (context.user_id,),
        ).fetchone()
        if (not user or not user["active"] or user["account_state"] != "active"
                or user["auth_version"] != version):
            raise AccessError(403, "任务账户权限已变更")
        account = identity._public(conn, user)
        if "business:write" not in account["permissions"]:
            raise AccessError(403, "无权执行研究任务")
        tenant = conn.execute(
            "SELECT * FROM control.tenants WHERE id=%s FOR SHARE", (context.tenant_id,),
        ).fetchone()
        membership = conn.execute(
            "SELECT role FROM control.memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE",
            (context.tenant_id, context.user_id),
        ).fetchone()
        if (not tenant or tenant["status"] != "active" or tenant["cell_id"] != context.cell_id
                or tenant["policy_version"] != context.policy_version
                or tenant["routing_epoch"] != context.routing_epoch
                or (not membership and account["role"] != "admin")):
            raise AccessError(403, "任务租户权限已变更")
        role = Role.OWNER if account["role"] == "admin" else Role(membership["role"])
        scoped = replace(context, role=role, permissions=ROLE_PERMISSIONS[role])
        scoped.require("strategies:write")
        if context.token_id:
            token = conn.execute(
                """SELECT * FROM control.api_tokens WHERE id=%s AND tenant_id=%s AND user_id=%s
                   AND revoked_at IS NULL AND expires_at>now() FOR SHARE""",
                (context.token_id, context.tenant_id, context.user_id),
            ).fetchone()
            if (not token or token["policy_version"] != context.policy_version
                    or token["routing_epoch"] != context.routing_epoch
                    or token["auth_version"] != version or not token["full_scopes"]):
                raise AccessError(403, "任务凭据已失效")
            scoped = replace(scoped, permissions=scoped.permissions & frozenset(token["permissions"]))
        yield scoped


class ResearchJobs:
    def __init__(self, documents: TenantDocuments):
        self.documents = documents
        self.db, self.context = documents.db, documents.context

    def get(self, job_id: str) -> dict | None:
        with self.db.business_transaction(self.context) as conn:
            return conn.execute("SELECT * FROM business.research_jobs WHERE id=%s", (job_id,)).fetchone()

    def submit(self, payload: dict, *, key: str | None = None, force=False, source="manual", claim=None,
               expected_version=None, expected_schedule=None, prerequisite_error=None) -> dict:
        from psycopg.types.json import Jsonb

        from app.multitenancy.identity import AccountIdentity

        payload = json_value(payload)
        if payload.get("tenant_id") != self.context.tenant_id:
            raise AccessError(403, "任务租户不一致")
        kind = payload["kind"]
        sig = key or signature(payload)
        snapshot = dict(payload)
        snapshot["config"] = {key: value for key, value in payload.get("config", {}).items() if key != "run_id"}
        snapshot_digest = signature(snapshot)
        identity = AccountIdentity(self.db)
        with self.db.control.connection() as conn:
            user = conn.execute("SELECT auth_version FROM control.users WHERE id=%s",
                                (self.context.user_id,)).fetchone()
        version = user["auth_version"] if expected_version is None else expected_version
        with (authorize(identity, self.context, version) as ctx,
              self.db.business_transaction(ctx, write=True) as conn):
            if claim:
                from app.multitenancy.runtime import valid_claim
                valid_claim(conn, claim)
            if expected_schedule:
                namespace, revision = expected_schedule
                saved = conn.execute(
                    "SELECT revision FROM business.documents WHERE namespace=%s AND id='default'", (namespace,),
                ).fetchone()
                if not saved or saved["revision"] != revision:
                    raise AccessError(409, "定时任务配置已更新")
            if payload.get("source_claim"):
                existing = conn.execute(
                    "SELECT * FROM business.research_jobs WHERE input->>'source_claim'=%s",
                    (payload["source_claim"],),
                ).fetchone()
                if existing:
                    return existing
            if not force:
                existing = conn.execute(
                    """SELECT * FROM business.research_jobs WHERE signature=%s AND kind=%s
                       AND metadata->>'snapshot'=%s AND state=ANY(%s)
                       ORDER BY created_at DESC LIMIT 1""",
                    (sig, kind, snapshot_digest, sorted(ACTIVE_TASK_STATUSES | SUCCESS_TASK_STATUSES)),
                ).fetchone()
                if existing:
                    return existing
            # Terminal results are retained until explicitly removed; never
            # delete an active job to make room for another user's request.
            if conn.execute("SELECT count(*) AS n FROM business.research_jobs").fetchone()["n"] >= MAX_JOBS:
                raise AccessError(429, "研究任务已达上限，请删除不再需要的历史任务")
            job_id = uuid.uuid4().hex
            now = datetime.now(UTC).isoformat()
            metadata = {
                "source": source, "snapshot": snapshot_digest, "policy_version": ctx.policy_version,
                "routing_epoch": ctx.routing_epoch, "token_id": ctx.token_id,
                "created_at": now, "started_at": None, "finished_at": None,
            }
            if kind in {"backtest", "optimize", "walkforward"}:
                metadata["research"] = {
                    "schema_version": 1, "job_id": job_id, "input_snapshot": snapshot_digest,
                    "public_release_id": payload.get("public_release_id"),
                }
            initial = "skipped_prerequisite" if prerequisite_error else "queued"
            event = {"id": 1, "timestamp": now, "type": initial,
                     "payload": {"status": initial, "source": source, "message": prerequisite_error}}
            return conn.execute(
                """INSERT INTO business.research_jobs
                   (tenant_id,id,user_id,kind,input,auth_version,signature,metadata,events,event_seq,state,error)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s) RETURNING *""",
                (ctx.tenant_id, job_id, ctx.user_id, kind, Jsonb(payload), version,
                 sig, Jsonb(metadata), Jsonb([event]), initial, prerequisite_error),
            ).fetchone()

    @staticmethod
    def event(conn, row: dict, event_type: str, payload: dict):
        from psycopg.types.json import Jsonb

        payload = json_value(payload)
        if len(json.dumps(payload).encode()) > 16 * 1024:
            raise AccessError(413, "任务进度记录过大")
        event_id = row["event_seq"] + 1
        event = {"id": event_id, "timestamp": datetime.now(UTC).isoformat(),
                 "type": event_type, "payload": payload}
        events = [*row["events"], event][-MAX_EVENTS:]
        conn.execute(
            "UPDATE business.research_jobs SET events=%s,event_seq=%s,updated_at=now() WHERE id=%s",
            (Jsonb(events), event_id, row["id"]),
        )
        row.update(events=events, event_seq=event_id)

    def cancel(self, job_id: str) -> bool:
        with self.db.business_transaction(self.context, write=True) as conn:
            row = conn.execute("SELECT * FROM business.research_jobs WHERE id=%s FOR UPDATE",
                               (job_id,)).fetchone()
            if not row or row["state"] not in ACTIVE_TASK_STATUSES:
                return False
            status = "cancelled" if row["state"] == "queued" else "cancelling"
            conn.execute(
                "UPDATE business.research_jobs SET state=%s,updated_at=now() WHERE id=%s",
                (status, job_id),
            )
            self.event(conn, row, status, {"status": status})
            return True

    def cancel_key(self, key: str, kind: str) -> bool:
        with self.db.business_transaction(self.context) as conn:
            row = conn.execute(
                """SELECT id FROM business.research_jobs WHERE signature=%s AND kind=%s
                   AND state=ANY(%s) ORDER BY created_at DESC LIMIT 1""",
                (key, kind, sorted(ACTIVE_TASK_STATUSES)),
            ).fetchone()
        return self.cancel(row["id"]) if row else False

    def delete(self, job_id: str):
        with self.db.business_transaction(self.context, write=True) as conn:
            row = conn.execute("SELECT state FROM business.research_jobs WHERE id=%s", (job_id,)).fetchone()
            if not row:
                raise AccessError(404, "研究任务不存在")
            if row["state"] in ACTIVE_TASK_STATUSES:
                raise AccessError(409, "请先取消正在执行的任务")
            conn.execute("DELETE FROM business.research_jobs WHERE id=%s", (job_id,))

    def artifact(self, job_id: str, name: str) -> bytes:
        with self.db.business_transaction(self.context) as conn:
            row = conn.execute(
                "SELECT content FROM business.research_artifacts WHERE job_id=%s AND name=%s",
                (job_id, name),
            ).fetchone()
        if not row:
            raise AccessError(404, "研究产物不存在")
        return bytes(row["content"])

    def frame(self, job_id: str, name: str) -> pl.DataFrame:
        return read_frame(self.artifact(job_id, name))


class _Cancellation:
    def __init__(self, worker, jobs, row, owner):
        self.worker, self.jobs, self.row, self.owner = worker, jobs, row, owner
        self.checked = 0.0
        self.cancelled = False

    def is_set(self):
        if self.worker.stop.is_set():
            return True
        if self.cancelled or time.monotonic() - self.checked < 1:
            return self.cancelled
        self.checked = time.monotonic()
        try:
            with (authorize(self.worker.identity, self.jobs.context, self.row["auth_version"]) as ctx,
                  self.jobs.db.business_transaction(ctx, write=True) as conn):
                current = conn.execute(
                    "SELECT state,lease_owner,lease_until FROM business.research_jobs WHERE id=%s",
                    (self.row["id"],),
                ).fetchone()
                self.cancelled = (not current or current["state"] != "running"
                                  or current["lease_owner"] != self.owner
                                  or not current["lease_until"] or current["lease_until"] <= datetime.now(UTC))
                if not self.cancelled:
                    conn.execute(
                        """UPDATE business.research_jobs
                           SET lease_until=now()+make_interval(secs=>%s) WHERE id=%s""",
                        (LEASE_SECONDS, self.row["id"]),
                    )
        except AccessError:
            self.cancelled = True
        return self.cancelled


class ResearchWorker:
    """Bounded scheduler. SQL rows survive process restarts and SSE disconnects."""
    def __init__(self, db, executor, *, concurrency=4):
        from app.cell.scheduler import Scheduler
        from app.multitenancy.identity import AccountIdentity

        self.db, self.executor = db, executor
        self.identity = AccountIdentity(db)
        self.scheduler = Scheduler(db)
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="research")
        self.capacity = concurrency
        self.futures = set()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="research-dispatch")

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _context(self, tenant_id):
        # Scheduling uses an internal binding context. Authorization is checked
        # separately for the row's actual submitter before execution and commit.
        with self.db.control.connection() as conn:
            row = conn.execute(
                """SELECT id,policy_version,routing_epoch FROM control.tenants
                   WHERE id=%s AND cell_id=%s AND status='active'""",
                (tenant_id, self.db.settings.id),
            ).fetchone()
        if row is None:
            return None
        return TenantContext(row["id"], "research-scheduler", self.db.settings.id, Role.OWNER,
                             ROLE_PERMISSIONS[Role.OWNER], row["policy_version"], row["routing_epoch"],
                             uuid.uuid4().hex)

    def _next_due(self, context):
        from app.cell.scheduler import Scheduler

        try:
            with self.db.business_transaction(context) as conn:
                return Scheduler.next_due(conn, "research", 0)
        except AccessError:
            return datetime.now(UTC) + timedelta(seconds=60)

    def _run_claim(self, context, row, scheduler_claim):
        try:
            self.run(context, row)
        finally:
            self.scheduler.finish(scheduler_claim, self._next_due(context))

    def _loop(self):
        import logging

        from app.multitenancy.health import WorkerHealth

        logger = logging.getLogger(__name__)
        health = WorkerHealth(self.db, "research")
        while not self.stop.is_set():
            for future in list(self.futures):
                if future.done():
                    self.futures.remove(future)
                    if future.exception():
                        logger.error("Research execution failed: %s", future.exception())
            try:
                available = self.capacity - len(self.futures)
                for scheduler_claim in self.scheduler.claim("research", available) if available else ():
                    if self.stop.is_set():
                        break
                    context = self._context(scheduler_claim["tenant_id"])
                    if context is None:
                        self.scheduler.finish(
                            scheduler_claim, datetime.now(UTC) + timedelta(seconds=60),
                        )
                        continue
                    row = self.claim(context)
                    if row:
                        self.futures.add(
                            self.pool.submit(self._run_claim, context, row, scheduler_claim),
                        )
                    else:
                        self.scheduler.finish(scheduler_claim, self._next_due(context))
                health.record()
            except Exception:
                logger.exception("Research dispatch failed")
                with suppress(Exception):
                    health.record(False)
            self.stop.wait(.5)

    def claim(self, context):
        from psycopg.types.json import Jsonb

        jobs = ResearchJobs(TenantDocuments(self.db, context))
        with self.db.business_transaction(context, write=True) as conn:
            expired = conn.execute(
                """SELECT * FROM business.research_jobs WHERE state IN ('running','cancelling')
                   AND lease_until<now() FOR UPDATE"""
            ).fetchall()
            for row in expired:
                status = "cancelled" if row["state"] == "cancelling" else "interrupted"
                metadata = {**row["metadata"], "finished_at": datetime.now(UTC).isoformat()}
                conn.execute(
                    """UPDATE business.research_jobs SET state=%s,lease_owner=NULL,lease_until=NULL,
                       metadata=%s,error=%s,updated_at=now() WHERE id=%s""",
                    (status, Jsonb(metadata), "任务执行中断，请重新提交", row["id"]),
                )
                jobs.event(conn, row, status, {"status": status})
            if conn.execute(
                "SELECT 1 FROM business.research_jobs WHERE state IN ('running','cancelling') LIMIT 1",
            ).fetchone():
                return None
            row = conn.execute(
                """SELECT * FROM business.research_jobs WHERE state='queued'
                   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            owner = uuid.uuid4().hex
            metadata = {**row["metadata"], "started_at": datetime.now(UTC).isoformat()}
            conn.execute(
                """UPDATE business.research_jobs SET state='running',lease_owner=%s,
                   lease_until=now()+make_interval(secs=>%s),attempts=attempts+1,metadata=%s WHERE id=%s""",
                (owner, LEASE_SECONDS, Jsonb(metadata), row["id"]),
            )
            jobs.event(conn, row, "running", {"status": "running"})
            row.update(state="running", lease_owner=owner)
            return row

    def run(self, context, row):
        context = replace(context, user_id=row["user_id"], policy_version=row["metadata"]["policy_version"],
                          routing_epoch=row["metadata"]["routing_epoch"], token_id=row["metadata"].get("token_id"))
        jobs = ResearchJobs(TenantDocuments(self.db, context))
        owner = row["lease_owner"]
        cancel = _Cancellation(self, jobs, row, owner)
        result, error, retry = None, None, False
        try:
            with authorize(self.identity, context, row["auth_version"]):
                pass

            def progress(value):
                if cancel.is_set():
                    return
                with self.db.business_transaction(context, write=True) as conn:
                    current = conn.execute(
                        """SELECT * FROM business.research_jobs WHERE id=%s AND lease_owner=%s
                           AND lease_until>now() AND state='running' FOR UPDATE""", (row["id"], owner),
                    ).fetchone()
                    if current:
                        jobs.event(conn, current, "progress", value)

            result = self.executor.execute(row["input"], progress, cancel)
            if result.get("error"):
                raise ValueError(str(result["error"]))
        except AccessError as exc:
            retry = exc.status in {429, 503} and not cancel.is_set()
            error = str(exc)
        except Exception as exc:
            error = str(exc)[:2000]
        try:
            if cancel.is_set():
                error = "任务已取消或账户权限已变更"
            with authorize(self.identity, context, row["auth_version"]) as authorized:
                self.finish(jobs, authorized, row, result, error, retry=retry)
        except AccessError:
            # Discard computation after revocation. Internal context may only
            # record failure; it cannot persist any computed private result.
            internal = replace(context, role=Role.OWNER, permissions=ROLE_PERMISSIONS[Role.OWNER],
                               policy_version=context.policy_version, routing_epoch=context.routing_epoch)
            with suppress(AccessError):
                self.finish(jobs, internal, row, None, "任务已取消或账户权限已变更")
        except (ValueError, TypeError) as exc:
            self.finish(jobs, context, row, None, f"结果保存失败: {str(exc)[:500]}")

    def finish(self, jobs, context, row, result, error, *, retry=False):
        from psycopg.types.json import Jsonb

        with self.db.business_transaction(context, write=True) as conn:
            current = conn.execute(
                """SELECT * FROM business.research_jobs WHERE id=%s AND lease_owner=%s
                   AND lease_until>now() AND state IN ('running','cancelling') FOR UPDATE""",
                (row["id"], row["lease_owner"]),
            ).fetchone()
            if not current:
                return
            cancelled = current["state"] == "cancelling" or self.stop.is_set()
            status = ("cancelled" if cancelled else "queued" if retry else "failed" if error
                      else result.get("status", "succeeded"))
            if status not in TERMINAL_TASK_STATUSES | {"queued"}:
                raise ValueError("invalid compute terminal state")
            if status in SUCCESS_TASK_STATUSES:
                result = dict(result)
                if current["metadata"].get("research"):
                    result["research"] = {
                        **result.get("research", {}),
                        "provenance": current["metadata"]["research"],
                    }
                artifacts = result.pop("artifact_data", {})
                if sum(len(value) for value in artifacts.values()) > 96 * 1024 * 1024:
                    raise ValueError("research artifacts exceed total size limit")
                for name, encoded in artifacts.items():
                    if name not in {"factors", "correlation", "candidates", "folds"}:
                        raise ValueError("unknown research artifact")
                    data = base64.b64decode(encoded, validate=True)
                    conn.execute(
                        """INSERT INTO business.research_artifacts(tenant_id,job_id,name,content)
                           VALUES(%s,%s,%s,%s)""", (context.tenant_id, row["id"], name, data),
                    )
                result = json_value(result)
            else:
                result = None
            metadata = {**current["metadata"], "finished_at": datetime.now(UTC).isoformat()}
            conn.execute(
                """UPDATE business.research_jobs SET state=%s,result=%s,error=%s,metadata=%s,
                   lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE id=%s""",
                (status, Jsonb(result) if result is not None else None,
                 error if status == "failed" else None, Jsonb(metadata), row["id"]),
            )
            jobs.event(conn, current, status, {"status": status, "message": error})


def jobs_for(request: Request) -> ResearchJobs | None:
    documents = private_storage(request)
    return ResearchJobs(documents) if documents else None


def run_sync(request: Request, kind: str, config: dict) -> dict:
    from app.api.strategy import _get_engine

    jobs = jobs_for(request)
    row = jobs.submit(_get_engine(request).task(kind, config=config), force=True)
    deadline = time.monotonic() + 7300
    while time.monotonic() < deadline:
        current = jobs.get(row["id"])
        if current["state"] in SUCCESS_TASK_STATUSES:
            return current["result"]
        if current["state"] in TERMINAL_TASK_STATUSES:
            raise AccessError(400, current["error"] or "任务已取消或中断")
        time.sleep(.5)
    jobs.cancel(row["id"])
    raise AccessError(504, "研究任务等待超时")


def stream(request: Request, jobs: ResearchJobs, row: dict, key: str):
    from app.multitenancy.access import verify_live_access

    async def events():
        yield f"event: job\ndata: {json.dumps({'key': key, 'id': row['id']})}\n\n"
        cursor = 0
        heartbeat = time.monotonic()
        while not await request.is_disconnected():
            try:
                await asyncio.to_thread(verify_live_access, request)
            except AccessError as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
                return
            current = await asyncio.to_thread(jobs.get, row["id"])
            if not current:
                return
            for event in current["events"]:
                if event["id"] > cursor:
                    cursor = event["id"]
                    if event["type"] == "progress":
                        yield f"event: progress\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
            if current["state"] in TERMINAL_TASK_STATUSES:
                if current["state"] in SUCCESS_TASK_STATUSES:
                    yield f"event: done\ndata: {json.dumps(current['result'], ensure_ascii=False, default=str)}\n\n"
                else:
                    message = current["error"] or ("任务已取消" if current["state"] == "cancelled" else "任务已中断，请重新提交")
                    yield f"event: error\ndata: {json.dumps({'message': message}, ensure_ascii=False)}\n\n"
                return
            if time.monotonic() - heartbeat > 15:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(.5)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def manifest(row: dict) -> dict:
    metadata = row["metadata"]
    config = row["input"]["config"]
    return {
        "schema_version": 1, "run_id": row["id"], "kind": row["kind"],
        "status": row["state"], "source": metadata["source"],
        "request": config.get("request", {}), "data_fingerprint": config.get("data_fingerprint"),
        "run_signature": row["signature"], "artifacts": {name: f"{name}.parquet" for name in
            (row["result"] or {}).get("artifacts", [])},
        "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
        "started_at": metadata.get("started_at"), "finished_at": metadata.get("finished_at"),
        "error": row["error"],
    }


class SQLMiningStore:
    def __init__(self, jobs: ResearchJobs):
        self.jobs = jobs

    def get(self, run_id):
        row = self.jobs.get(run_id)
        return manifest(row) if row and row["kind"] == "mining" else None

    def list_runs(self, *, limit=50, statuses=None):
        with self.jobs.db.business_transaction(self.jobs.context) as conn:
            rows = conn.execute(
                """SELECT * FROM business.research_jobs WHERE kind='mining'
                   AND (%s::text[] IS NULL OR state=ANY(%s)) ORDER BY created_at DESC LIMIT %s""",
                (statuses, statuses, limit),
            ).fetchall()
        return [manifest(row) for row in rows]

    def find_by_signature(self, sig, *, statuses):
        return next((item for item in self.list_runs(limit=200, statuses=sorted(statuses))
                     if item["run_signature"] == sig), None)

    def read_summary(self, run_id):
        row = self.jobs.get(run_id)
        if not row or row["kind"] != "mining":
            raise KeyError(run_id)
        progress = next((item["payload"] for item in reversed(row["events"])
                         if item["type"] == "progress"), None)
        return {**(row["result"] or {}), "progress": progress}

    def read_events(self, run_id, *, after_id=0):
        row = self.jobs.get(run_id)
        if not row or row["kind"] != "mining":
            raise KeyError(run_id)
        return [event for event in row["events"] if event["id"] > after_id]

    def read_artifact(self, run_id, name):
        frame = self.jobs.frame(run_id, name)
        if name == "candidates":
            for row in frame.select("signature").to_dicts():
                values = self.jobs.documents.get("mining_backlinks", f"{run_id}:{row['signature']}", {})
                for field, value in values.items():
                    frame = frame.with_columns(
                        pl.when(pl.col("signature") == row["signature"]).then(pl.lit(value))
                        .otherwise(pl.col(field)).alias(field),
                    )
        return frame


class SQLMiningManager:
    def __init__(self, jobs, engine):
        self.jobs, self.engine = jobs, engine
        self.store = SQLMiningStore(jobs)

    def start(self, request, data_fingerprint, *, force=False, source="manual", run_id=None):
        from app.services.mining_jobs import compute_run_signature

        config = {"run_id": run_id or uuid.uuid4().hex, "request": request,
                  "data_fingerprint": data_fingerprint, "source": source}
        payload = self.engine.task("mining", config=config)
        sig = compute_run_signature(request, {**data_fingerprint, "private": signature(
            {key: payload[key] for key in ("sources", "overrides", "factors", "signals")},
        )})
        row = self.jobs.submit(payload, key=sig, force=force, source=source)
        return {**manifest(row), "reused": row["input"]["config"]["run_id"] != config["run_id"]}

    def cancel(self, run_id):
        if not self.store.get(run_id):
            raise KeyError(run_id)
        self.jobs.cancel(run_id)
        return self.store.get(run_id)
