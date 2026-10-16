"""Run phase-two step-two mixed acceptance on disposable PostgreSQL.

This is a local capacity and isolation acceptance harness. HTTP runs through an
in-process ASGI transport, while identity, tenant state, scheduler work,
research execution, event delivery, and RLS use real PostgreSQL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import psutil
import psycopg
from fastapi import FastAPI
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.api import events as event_api
from app.api import paper as paper_api
from app.api import strategy as strategy_api
from app.api import watchlist as watchlist_api
from app.cell.context import AccessError
from app.multitenancy import access
from app.multitenancy import notifications as notification_api
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import EventHub, append_event
from app.multitenancy.research import ResearchJobs, ResearchWorker
from app.multitenancy.stream_auth import StreamTicketCodec
from app.strategy.engine import StrategyEngine
from scripts.accept_phase2_step1 import (
    _cluster,
    _contexts,
    _git_state,
    _latency,
    _login,
    _public_markers,
    _seed,
)

BASE_URL = "https://phase2-step2.test"


def _database_sizes(root_dsn: str, dbnames: tuple[str, ...]) -> dict[str, int]:
    with psycopg.connect(root_dsn, autocommit=True, row_factory=dict_row) as conn:
        return {
            name: conn.execute(
                "SELECT pg_database_size(%s) AS bytes", (name,),
            ).fetchone()["bytes"]
            for name in dbnames
        }


class ResourceSampler:
    """Sample only metrics the local harness can measure without pretending."""

    def __init__(self, root_dsn: str, dbnames: tuple[str, ...]):
        self.root_dsn = root_dsn
        self.dbnames = dbnames
        self.data_dsn = make_conninfo(root_dsn, dbname=dbnames[1])
        self.process = psutil.Process()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="phase2-step2-resource-sampler",
        )
        self.samples = 0
        self.errors = 0
        self.peak_rss = 0
        self.peak_cpu_percent = 0.0
        self.peak_threads = 0
        self.peak_db_connections = 0
        self.peak_db_active = 0
        self.peak_db_waiting = 0
        self.peak_research_queued = 0
        self.peak_research_running = 0
        self.peak_research_due = 0
        self.started_cpu = self.process.cpu_times()
        self.started_rss = self.process.memory_info().rss
        self.process.cpu_percent(None)

    def start(self) -> None:
        self._sample_process()
        self.thread.start()

    def _sample_process(self) -> None:
        with self.process.oneshot():
            self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
            self.peak_threads = max(self.peak_threads, self.process.num_threads())
        self.peak_cpu_percent = max(
            self.peak_cpu_percent,
            self.process.cpu_percent(None),
        )

    def _run(self) -> None:
        try:
            with (
                psycopg.connect(
                    self.root_dsn,
                    autocommit=True,
                    row_factory=dict_row,
                ) as root,
                psycopg.connect(
                    self.data_dsn,
                    autocommit=True,
                    row_factory=dict_row,
                ) as data,
            ):
                while not self.stop.wait(0.1):
                    try:
                        self._sample_process()
                        activity = root.execute(
                            """SELECT count(*) AS total,
                                      count(*) FILTER (WHERE state='active') AS active,
                                      count(*) FILTER (
                                          WHERE state='active' AND wait_event_type IS NOT NULL
                                      ) AS waiting
                               FROM pg_stat_activity WHERE datname=ANY(%s)""",
                            (list(self.dbnames),),
                        ).fetchone()
                        research = data.execute(
                            """SELECT
                                  count(*) FILTER (WHERE state='queued') AS queued,
                                  count(*) FILTER (WHERE state='running') AS running
                               FROM business.research_jobs"""
                        ).fetchone()
                        due = data.execute(
                            """SELECT count(*) AS n FROM scheduler.work
                               WHERE kind='research' AND due_at IS NOT NULL"""
                        ).fetchone()["n"]
                        self.peak_db_connections = max(
                            self.peak_db_connections,
                            activity["total"],
                        )
                        self.peak_db_active = max(
                            self.peak_db_active,
                            activity["active"],
                        )
                        self.peak_db_waiting = max(
                            self.peak_db_waiting,
                            activity["waiting"],
                        )
                        self.peak_research_queued = max(
                            self.peak_research_queued,
                            research["queued"],
                        )
                        self.peak_research_running = max(
                            self.peak_research_running,
                            research["running"],
                        )
                        self.peak_research_due = max(
                            self.peak_research_due,
                            due,
                        )
                        self.samples += 1
                    except Exception:
                        self.errors += 1
        except Exception:
            self.errors += 1

    def close(self) -> dict:
        self.stop.set()
        self.thread.join(timeout=5)
        self._sample_process()
        ended_cpu = self.process.cpu_times()
        return {
            "samples": self.samples,
            "sampling_errors": self.errors,
            "harness_process": {
                "rss_start_bytes": self.started_rss,
                "rss_peak_bytes": self.peak_rss,
                "rss_end_bytes": self.process.memory_info().rss,
                "cpu_user_seconds": round(
                    ended_cpu.user - self.started_cpu.user,
                    3,
                ),
                "cpu_system_seconds": round(
                    ended_cpu.system - self.started_cpu.system,
                    3,
                ),
                "peak_cpu_percent": round(self.peak_cpu_percent, 1),
                "peak_threads": self.peak_threads,
            },
            "postgresql": {
                "peak_connections_to_acceptance_databases": self.peak_db_connections,
                "peak_active_connections": self.peak_db_active,
                "peak_waiting_active_connections": self.peak_db_waiting,
            },
            "research": {
                "peak_queued": self.peak_research_queued,
                "peak_running": self.peak_research_running,
                "peak_due_tenants": self.peak_research_due,
            },
        }


def _seed_private_data(
    cluster,
    principals: list[dict],
    *,
    ledger_rows: int,
    nav_rows: int,
) -> dict:
    started = time.perf_counter()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    base_day = date(2025, 12, 1)
    with cluster.data.transaction():
        for principal in principals:
            index = principal["index"]
            tenant_id = principal["tenant_id"]
            account = {
                "id": "default",
                "name": f"容量账户 {index:03d}",
                "initial_cash": 1_000_000.0,
                "cash": 1_000_000.0,
                "commission_pct": 0.00025,
                "stamp_tax_pct": 0.001,
                "slippage_bps": 5.0,
                "queue_limit_orders": False,
                "status": "active",
                "created_at": base_time.isoformat(),
            }
            cluster.data.execute(
                """INSERT INTO business.full_paper_accounts
                   (tenant_id,id,value,positions) VALUES(%s,'default',%s,%s)""",
                (tenant_id, Jsonb(account), Jsonb({})),
            )
            orders = []
            fills = []
            for ordinal in range(1, ledger_rows + 1):
                order_id = f"ord-{index:03d}-{ordinal:05d}"
                created_at = (
                    base_time + timedelta(seconds=index * ledger_rows + ordinal)
                ).isoformat()
                symbol = f"{600000 + ordinal % 20:06d}.SH"
                side = "buy" if ordinal % 2 else "sell"
                order = {
                    "id": order_id,
                    "symbol": symbol,
                    "asset_type": "stock",
                    "side": side,
                    "qty": 100,
                    "order_type": "market",
                    "status": "filled",
                    "postponed": 0,
                    "source": "acceptance",
                    "created_at": created_at,
                }
                fill = {
                    "id": f"fill-{index:03d}-{ordinal:05d}",
                    "order_id": order_id,
                    "owner": index,
                    "symbol": symbol,
                    "asset_type": "stock",
                    "side": side,
                    "qty": 100,
                    "price": round(10 + ordinal / 1000, 3),
                    "fees": 5.0,
                    "kind": "fill",
                    "timestamp": created_at,
                    "date": created_at[:10],
                }
                orders.append(
                    (tenant_id, "default", order_id, Jsonb(order)),
                )
                fills.append(
                    (
                        tenant_id,
                        "default",
                        "fill:" + order_id,
                        ordinal,
                        Jsonb(fill),
                    ),
                )
            with cluster.data.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO business.full_paper_orders
                       (tenant_id,account_id,id,value) VALUES(%s,%s,%s,%s)""",
                    orders,
                )
                cursor.executemany(
                    """INSERT INTO business.full_paper_fills
                       (tenant_id,account_id,id,ordinal,value)
                       VALUES(%s,%s,%s,%s,%s)""",
                    fills,
                )
            nav = []
            for offset in range(nav_rows):
                day = base_day + timedelta(days=offset)
                value = {
                    "date": day.isoformat(),
                    "cash": 1_000_000.0,
                    "mv": 0.0,
                    "nav": 1_000_000.0 + offset,
                    "owner": index,
                }
                nav.append(
                    (tenant_id, "default", day, Jsonb(value)),
                )
            with cluster.data.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO business.full_paper_nav
                       (tenant_id,account_id,day,value) VALUES(%s,%s,%s,%s)""",
                    nav,
                )
            context = principal["context"]
            cluster.data.execute(
                """INSERT INTO business.notification_channels
                   (tenant_id,id,name,kind,destination,enabled,user_id,
                    auth_version,policy_version,routing_epoch)
                   VALUES(%s,%s,%s,'email',%s,false,%s,%s,%s,%s)""",
                (
                    tenant_id,
                    f"disabled-{index:03d}",
                    f"禁用验收渠道 {index:03d}",
                    principal["email"],
                    principal["user_id"],
                    principal["account"]["auth_version"],
                    context.policy_version,
                    context.routing_epoch,
                ),
            )
    return {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "accounts": len(principals),
        "orders": len(principals) * ledger_rows,
        "fills": len(principals) * ledger_rows,
        "nav_rows": len(principals) * nav_rows,
        "disabled_notification_channels": len(principals),
    }


def _dataset_counts(cluster) -> dict:
    tables = (
        "full_paper_accounts",
        "full_paper_orders",
        "full_paper_fills",
        "full_paper_nav",
        "notification_channels",
        "research_jobs",
    )
    counts = {
        table: cluster.data.execute(
            f"SELECT count(*) AS n FROM business.{table}"
        ).fetchone()["n"]
        for table in tables
    }
    counts["scheduler_research_rows"] = cluster.data.execute(
        "SELECT count(*) AS n FROM scheduler.work WHERE kind='research'"
    ).fetchone()["n"]
    return counts


def _app(cluster, root: Path) -> tuple[FastAPI, StreamTicketCodec]:
    app = FastAPI()
    app.state.service_role = "api"
    app.state.account_identity = cluster.identity
    app.state.quote_service = SimpleNamespace(ensure_fresh=lambda: None)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=root),
        get_name_map=lambda symbols: {},
        resolve_asset_type=lambda symbol: "stock",
    )
    app.state.compute_executor = object()
    app.state.strategy_engine = StrategyEngine(
        [Path(strategy_api.__file__).resolve().parents[1] / "strategy" / "builtin"]
    )
    codec = StreamTicketCodec(cluster.settings.stream_ticket_secret)
    app.state.stream_tickets = codec
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(watchlist_api.router)
    app.include_router(strategy_api.router)
    app.include_router(paper_api.router)
    app.include_router(notification_api.router)
    app.include_router(event_api.router)
    return app, codec


async def _http_round(
    client: httpx.AsyncClient,
    codec: StreamTicketCodec,
    principals: list[dict],
    round_index: int,
    ledger_rows: int,
    nav_rows: int,
    *,
    profile: str,
    request_interval: float = 0,
    stagger_seconds: float = 0,
) -> dict:
    records: list[dict] = []
    active = 0
    peak_active = 0
    started_count = 0
    active_lock = asyncio.Lock()
    gate = asyncio.Event()
    all_active = asyncio.Event()

    async def request(method: str, path: str, headers: dict, operation: str, **kwargs):
        started = time.perf_counter()
        try:
            response = await client.request(method, path, headers=headers, **kwargs)
        except Exception as exc:
            records.append(
                {
                    "operation": operation,
                    "status": 0,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "error": type(exc).__name__,
                }
            )
            raise
        records.append(
            {
                "operation": operation,
                "status": response.status_code,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        )
        if request_interval:
            await asyncio.sleep(request_interval)
        return response

    async def one(principal):
        nonlocal active, peak_active, started_count
        await gate.wait()
        async with active_lock:
            active += 1
            started_count += 1
            peak_active = max(peak_active, active)
            if started_count == len(principals):
                all_active.set()
        await all_active.wait()
        if stagger_seconds:
            await asyncio.sleep(
                stagger_seconds * principal["index"] / len(principals)
            )
        started = time.perf_counter()
        try:
            headers = {
                "Cookie": f"tf_session={principal['token']}",
                "Origin": BASE_URL,
            }
            symbols = [f"{600000 + offset:06d}.SH" for offset in range(20)]
            note = f"owner-{principal['index']:03d}-round-{round_index + 1}"
            batch = await request(
                "POST",
                "/api/watchlist/batch",
                headers,
                "watchlist_write",
                json={"symbols": symbols, "note": note},
            )
            listed = await request(
                "GET",
                "/api/watchlist",
                headers,
                "watchlist_read",
            )
            strategies = await request(
                "GET",
                "/api/strategies",
                headers,
                "strategy_catalog",
            )
            orders = await request(
                "GET",
                "/api/paper/orders?limit=25",
                headers,
                "paper_orders_page_1",
            )
            order_cursor = orders.json().get("next_cursor") if orders.status_code == 200 else None
            older_orders = await request(
                "GET",
                "/api/paper/orders?limit=25&cursor=" + str(order_cursor),
                headers,
                "paper_orders_page_2",
            )
            trades = await request(
                "GET",
                "/api/paper/trades?limit=25",
                headers,
                "paper_trades_page_1",
            )
            trade_cursor = trades.json().get("next_cursor") if trades.status_code == 200 else None
            older_trades = await request(
                "GET",
                "/api/paper/trades?limit=25&cursor=" + str(trade_cursor),
                headers,
                "paper_trades_page_2",
            )
            nav = await request(
                "GET",
                "/api/paper/nav?limit=30",
                headers,
                "paper_nav_page_1",
            )
            nav_cursor = nav.json().get("next_cursor") if nav.status_code == 200 else None
            older_nav = await request(
                "GET",
                "/api/paper/nav?limit=30&cursor=" + str(nav_cursor),
                headers,
                "paper_nav_page_2",
            )
            channels = await request(
                "GET",
                "/api/monitor-rules/notifications/channels",
                headers,
                "notification_channels",
            )
            preferences = await request(
                "PUT",
                "/api/monitor-rules/notifications/preferences",
                headers,
                "notification_preferences_write",
                json={
                    "default_channels": [],
                    "review_channels": [],
                    "review_push_mode": "manual",
                },
            )
            ticket = await request(
                "POST",
                "/api/events/ticket",
                headers,
                "event_ticket",
            )
            responses = (
                batch,
                listed,
                strategies,
                orders,
                older_orders,
                trades,
                older_trades,
                nav,
                older_nav,
                channels,
                preferences,
                ticket,
            )
            if any(response.status_code != 200 for response in responses):
                raise AssertionError(
                    f"user {principal['index']} statuses "
                    f"{[response.status_code for response in responses]}"
                )
            watchlist = listed.json()["symbols"]
            if (
                len(watchlist) != 20
                or {row["note"] for row in watchlist} != {note}
            ):
                raise AssertionError(
                    f"user {principal['index']} read another tenant's watchlist"
                )
            if not strategies.json()["strategies"]:
                raise AssertionError("strategy catalog is unexpectedly empty")

            expected_prefix = f"ord-{principal['index']:03d}-"
            first_orders = orders.json()
            second_orders = older_orders.json()
            if (
                first_orders["total"] != ledger_rows
                or not first_orders["next_cursor"]
                or not second_orders["next_cursor"]
                or any(
                    not row["id"].startswith(expected_prefix)
                    for row in first_orders["orders"] + second_orders["orders"]
                )
                or {
                    row["id"] for row in first_orders["orders"]
                }
                & {
                    row["id"] for row in second_orders["orders"]
                }
            ):
                raise AssertionError(
                    f"user {principal['index']} paper order page is invalid"
                )
            first_trades = trades.json()
            second_trades = older_trades.json()
            if (
                first_trades["total"] != ledger_rows
                or first_trades["execution_total"] != ledger_rows
                or not first_trades["next_cursor"]
                or not second_trades["next_cursor"]
                or any(
                    row["owner"] != principal["index"]
                    for row in first_trades["fills"] + second_trades["fills"]
                )
            ):
                raise AssertionError(
                    f"user {principal['index']} paper trade page is invalid"
                )
            first_nav = nav.json()
            second_nav = older_nav.json()
            if (
                first_nav["total"] != nav_rows
                or not first_nav["next_cursor"]
                or not second_nav["next_cursor"]
                or any(
                    row["owner"] != principal["index"]
                    for row in first_nav["nav"] + second_nav["nav"]
                )
            ):
                raise AssertionError(
                    f"user {principal['index']} paper NAV page is invalid"
                )
            channel_rows = channels.json()["channels"]
            if (
                len(channel_rows) != 1
                or channel_rows[0]["id"] != f"disabled-{principal['index']:03d}"
                or channel_rows[0]["enabled"]
            ):
                raise AssertionError(
                    f"user {principal['index']} notification isolation failed"
                )
            claims = codec.consume(ticket.json()["ticket"])
            if claims is None or claims.tenant_id != principal["tenant_id"]:
                raise AssertionError("stream ticket lost its tenant binding")
            if codec.consume(ticket.json()["ticket"]) is not None:
                raise AssertionError("stream ticket replay was accepted")
            return {
                "ok": True,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        finally:
            async with active_lock:
                active -= 1

    tasks = [asyncio.create_task(one(principal)) for principal in principals]
    await asyncio.sleep(0)
    started = time.perf_counter()
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed_ms = (time.perf_counter() - started) * 1000
    errors = [
        f"{type(result).__name__}: {result}"
        for result in results
        if isinstance(result, BaseException)
    ]
    latencies = [
        result["latency_ms"]
        for result in results
        if not isinstance(result, BaseException)
    ]
    request_latencies = [record["latency_ms"] for record in records]
    operation_latency = {
        operation: _latency(
            [
                record["latency_ms"]
                for record in records
                if record["operation"] == operation
            ]
        )
        for operation in sorted({record["operation"] for record in records})
    }
    return {
        "profile": profile,
        "round": round_index + 1,
        "users": len(principals),
        "peak_active_workflows": peak_active,
        "successes": len(latencies),
        "failures": len(errors),
        "errors": errors[:10],
        "elapsed_ms": round(elapsed_ms, 3),
        "workflow_latency": _latency(latencies),
        "request_latency": _latency(request_latencies),
        "operation_latency": operation_latency,
        "requests": len(records),
        "http_5xx": sum(record["status"] >= 500 for record in records),
        "http_4xx": sum(400 <= record["status"] < 500 for record in records),
        "transport_errors": sum(record["status"] == 0 for record in records),
    }


async def _http_workflows(
    app: FastAPI,
    codec: StreamTicketCodec,
    principals: list[dict],
    *,
    rounds: int,
    ledger_rows: int,
    nav_rows: int,
    profile: str,
    request_interval: float = 0,
    stagger_seconds: float = 0,
) -> list[dict]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
        timeout=30,
    ) as client:
        return [
            await _http_round(
                client,
                codec,
                principals,
                index,
                ledger_rows,
                nav_rows,
                profile=profile,
                request_interval=request_interval,
                stagger_seconds=stagger_seconds,
            )
            for index in range(rounds)
        ]


def _private_events(cluster, contexts: list) -> dict:
    hub = EventHub(cluster.db)
    subscriptions = [
        hub.subscribe(TenantDocuments(cluster.db, context))
        for context in contexts
    ]
    hub.start()
    try:
        deadline = time.time() + 10
        while not hub.listener_connected and time.time() < deadline:
            time.sleep(0.02)
        if not hub.listener_connected:
            raise AssertionError("private event LISTEN did not connect")
        started = time.perf_counter()
        for index, context in enumerate(contexts):
            with cluster.db.business_transaction(context, write=True) as conn:
                append_event(
                    conn,
                    context.tenant_id,
                    "strategy_results_updated",
                    {"owner": index, "source": "phase2-step2"},
                )
        received: dict[int, list[dict]] = {}
        deadline = time.time() + 20
        while len(received) < len(subscriptions) and time.time() < deadline:
            for index, subscription in enumerate(subscriptions):
                rows = subscription.pop()
                if rows:
                    received.setdefault(index, []).extend(rows)
            if len(received) < len(subscriptions):
                time.sleep(0.02)
        for index in range(len(subscriptions)):
            rows = received.get(index, [])
            if (
                len(rows) != 1
                or rows[0]["payload"]
                != {"owner": index, "source": "phase2-step2"}
            ):
                raise AssertionError(
                    f"private event isolation failed for user {index}"
                )
        return {
            "subscribers": len(subscriptions),
            "successes": len(received),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "hub": hub.status(),
        }
    finally:
        hub.close()


class AcceptanceCompute:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def execute(self, task, progress, cancel):
        started = time.time()
        if cancel.is_set():
            raise RuntimeError("research task cancelled before execution")
        progress({"phase": "acceptance", "completed": 1, "total": 1})
        time.sleep(self.delay_seconds)
        if cancel.is_set():
            raise RuntimeError("research task cancelled during execution")
        finished = time.time()
        with self.lock:
            self.calls.append(
                {
                    "tenant_id": task["tenant_id"],
                    "started_at": started,
                    "finished_at": finished,
                }
            )
        return {
            "status": "succeeded",
            "stats": {"total_return": 0.01},
            "trades": [],
        }


def _submit_research(cluster, principals: list[dict]) -> tuple[list[dict], dict]:
    started = time.perf_counter()
    submitted = []
    for principal in principals:
        jobs = ResearchJobs(
            TenantDocuments(cluster.db, principal["context"])
        )
        row = jobs.submit(
            {
                "kind": "backtest",
                "tenant_id": principal["tenant_id"],
                "config": {"period": "2026-10-16", "acceptance": True},
                "sources": [],
                "overrides": {},
                "factors": [],
                "signals": [],
            },
            key=f"phase2-step2-{principal['index']:03d}",
            force=True,
            source="acceptance",
        )
        submitted.append(
            {
                "tenant_id": principal["tenant_id"],
                "job_id": row["id"],
            }
        )
    return submitted, {
        "submitted": len(submitted),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _await_research(
    cluster,
    submitted: list[dict],
    compute: AcceptanceCompute,
    started_at: float,
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    tenant_ids = [item["tenant_id"] for item in submitted]
    while time.monotonic() < deadline:
        states = cluster.data.execute(
            """SELECT state,count(*) AS n FROM business.research_jobs
               WHERE tenant_id=ANY(%s) GROUP BY state""",
            (tenant_ids,),
        ).fetchall()
        by_state = {row["state"]: row["n"] for row in states}
        if by_state.get("succeeded") == len(submitted):
            break
        if sum(
            by_state.get(state, 0)
            for state in (
                "failed",
                "cancelled",
                "interrupted",
                "skipped_prerequisite",
            )
        ):
            raise AssertionError(f"research jobs reached failure states: {by_state}")
        time.sleep(0.05)
    else:
        raise TimeoutError("research acceptance queue did not drain")

    rows = cluster.data.execute(
        """SELECT tenant_id,state,attempts,metadata FROM business.research_jobs
           WHERE tenant_id=ANY(%s)""",
        (tenant_ids,),
    ).fetchall()
    if (
        len(rows) != len(submitted)
        or any(row["state"] != "succeeded" or row["attempts"] != 1 for row in rows)
    ):
        raise AssertionError("research completion or attempt count is invalid")
    call_tenants = {call["tenant_id"] for call in compute.calls}
    if call_tenants != set(tenant_ids) or len(compute.calls) != len(submitted):
        raise AssertionError("research scheduler did not execute every tenant exactly once")
    queue_ms = []
    execution_ms = []
    completion_ms = []
    for row in rows:
        created = datetime.fromisoformat(row["metadata"]["created_at"]).timestamp()
        started = datetime.fromisoformat(row["metadata"]["started_at"]).timestamp()
        finished = datetime.fromisoformat(row["metadata"]["finished_at"]).timestamp()
        queue_ms.append((started - created) * 1000)
        execution_ms.append((finished - started) * 1000)
        completion_ms.append((finished - started_at) * 1000)
    due = cluster.data.execute(
        """SELECT count(*) AS n FROM scheduler.work
           WHERE kind='research' AND tenant_id=ANY(%s) AND due_at IS NOT NULL""",
        (tenant_ids,),
    ).fetchone()["n"]
    slots = cluster.data.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE tenant_id IS NOT NULL) AS occupied
           FROM scheduler.slots WHERE kind='research'"""
    ).fetchone()
    return {
        "completed": len(rows),
        "elapsed_ms": round((time.time() - started_at) * 1000, 3),
        "queue_latency": _latency(queue_ms),
        "execution_latency": _latency(execution_ms),
        "completion_latency": _latency(completion_ms),
        "attempts": {"minimum": 1, "maximum": 1},
        "remaining_due_tenants": due,
        "slots": slots,
    }


def _cross_tenant_isolation(cluster, principals: list[dict]) -> dict:
    started = time.perf_counter()
    checks = 0
    for index, principal in enumerate(principals):
        other = (index + 1) % len(principals)
        foreign_id = f"ord-{other:03d}-00001"
        with cluster.db.business_transaction(principal["context"]) as conn:
            row = conn.execute(
                "SELECT id FROM business.full_paper_orders WHERE id=%s",
                (foreign_id,),
            ).fetchone()
        if row is not None:
            raise AssertionError(
                f"tenant {index} read tenant {other}'s paper order"
            )
        checks += 1
    return {
        "checks": checks,
        "leaks": 0,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _http_summary(rounds: list[dict]) -> dict:
    requests = sum(item["requests"] for item in rounds)
    failures = sum(item["failures"] for item in rounds)
    http_5xx = sum(item["http_5xx"] for item in rounds)
    http_4xx = sum(item["http_4xx"] for item in rounds)
    transport_errors = sum(item["transport_errors"] for item in rounds)
    return {
        "rounds": len(rounds),
        "workflows": sum(item["users"] for item in rounds),
        "successful_workflows": sum(item["successes"] for item in rounds),
        "failed_workflows": failures,
        "requests": requests,
        "http_5xx": http_5xx,
        "http_4xx": http_4xx,
        "transport_errors": transport_errors,
        "effective_request_error_rate": round(
            (http_5xx + http_4xx + transport_errors) / requests,
            6,
        )
        if requests
        else 0,
        "peak_active_workflows": max(
            (item["peak_active_workflows"] for item in rounds),
            default=0,
        ),
        "worst_round_request_p95_ms": max(
            (item["request_latency"]["p95_ms"] for item in rounds),
            default=0,
        ),
        "worst_round_request_p99_ms": max(
            (item["request_latency"]["p99_ms"] for item in rounds),
            default=0,
        ),
    }


def run(
    root_dsn: str,
    users: int,
    rounds: int,
    pool_size: int,
    ledger_rows: int,
    nav_rows: int,
    research_concurrency: int,
    research_delay: float,
    steady_request_interval: float,
    output: Path,
) -> dict:
    repository = Path(__file__).resolve().parents[2]
    started = time.time()
    report = {
        "status": "failed",
        "scope": "local_disposable_postgresql_100_user_mixed_acceptance",
        "limits": [
            "not a production or candidate-host deployment",
            "HTTP uses an in-process ASGI transport; Nginx and container network capacity are not measured",
            "event subscribers exercise the PostgreSQL event hub, not 100 external SSE sockets",
            "public quote subscribers receive synthetic update markers; no market source or display freshness is measured",
            "research uses a deterministic fast compute stub; real backtest throughput is not measured",
            "notification channels are disabled and no email or webhook is sent",
            "harness RSS and CPU exclude PostgreSQL server processes",
            "not a complete trading-day, 72-hour, restart, backup, or restore acceptance",
        ],
        "parameters": {
            "users": users,
            "rounds": rounds,
            "pool_size_per_database": pool_size,
            "ledger_rows_per_tenant": ledger_rows,
            "nav_rows_per_tenant": nav_rows,
            "research_concurrency": research_concurrency,
            "research_delay_seconds": research_delay,
            "steady_request_interval_seconds": steady_request_interval,
        },
        "git": _git_state(repository),
    }
    sampler = None
    worker = None
    try:
        with _cluster(
            root_dsn,
            pool_size,
            name_prefix="tsp_p2s2_",
            base_url=BASE_URL,
        ) as cluster:
            report["schema_version"] = cluster.settings.schema_version
            report["database_size_before_bytes"] = _database_sizes(
                root_dsn,
                cluster.dbnames,
            )
            seeded = _seed(
                cluster,
                users,
                email_prefix="phase2-step2",
            )
            login = _login(cluster, seeded)
            principals = login.pop("principals")
            contexts = _contexts(cluster, principals)
            for principal, context in zip(principals, contexts, strict=True):
                principal["context"] = context
            report["identity"] = {
                "users": users,
                "login": login,
            }
            report["private_dataset"] = _seed_private_data(
                cluster,
                principals,
                ledger_rows=ledger_rows,
                nav_rows=nav_rows,
            )
            app, codec = _app(cluster, repository)
            submitted, submission = _submit_research(cluster, principals)
            report["research_submission"] = submission
            report["dataset_counts_before_load"] = _dataset_counts(cluster)
            compute = AcceptanceCompute(research_delay)
            worker = ResearchWorker(
                cluster.db,
                compute,
                concurrency=research_concurrency,
            )
            sampler = ResourceSampler(root_dsn, cluster.dbnames)
            sampler.start()
            research_started = time.time()
            worker.start()
            with ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="phase2-step2-side-load",
            ) as side_load:
                private_future = side_load.submit(
                    _private_events,
                    cluster,
                    contexts,
                )
                marker_future = side_load.submit(_public_markers, users)
                http_rounds = asyncio.run(
                    _http_workflows(
                        app,
                        codec,
                        principals,
                        rounds=rounds,
                        ledger_rows=ledger_rows,
                        nav_rows=nav_rows,
                        profile="synchronized_burst",
                    )
                )
                report["burst_http_rounds"] = http_rounds
                report["burst_http_summary"] = _http_summary(http_rounds)
                report["research"] = _await_research(
                    cluster,
                    submitted,
                    compute,
                    research_started,
                    timeout=60,
                )
                report["private_events"] = private_future.result(timeout=30)
                report["public_markers"] = marker_future.result(timeout=30)
            steady_rounds = (
                asyncio.run(
                    _http_workflows(
                        app,
                        codec,
                        principals,
                        rounds=1,
                        ledger_rows=ledger_rows,
                        nav_rows=nav_rows,
                        profile="steady_20_rps",
                        request_interval=steady_request_interval,
                        stagger_seconds=steady_request_interval,
                    )
                )
                if steady_request_interval
                else []
            )
            report["steady_http_rounds"] = steady_rounds
            report["steady_http_summary"] = _http_summary(steady_rounds)
            if any(item["failures"] for item in [*http_rounds, *steady_rounds]):
                raise AssertionError("one or more mixed HTTP workflows failed")
            if report["burst_http_summary"]["peak_active_workflows"] != users:
                raise AssertionError("the harness did not reach all-user concurrency")
            if (
                report["burst_http_summary"]["effective_request_error_rate"] != 0
                or report["steady_http_summary"]["effective_request_error_rate"] != 0
            ):
                raise AssertionError("mixed HTTP requests returned errors")
            if steady_rounds and (
                report["steady_http_summary"]["worst_round_request_p95_ms"] > 300
                or report["steady_http_summary"]["worst_round_request_p99_ms"] > 1000
            ):
                raise AssertionError("steady light API latency exceeded the phase gate")
            report["cross_tenant_isolation"] = _cross_tenant_isolation(
                cluster,
                principals,
            )
            report["dataset_counts_after_load"] = _dataset_counts(cluster)
            report["database_size_after_bytes"] = _database_sizes(
                root_dsn,
                cluster.dbnames,
            )
            report["resource_observation"] = sampler.close()
            sampler = None
            worker.close()
            worker = None
            report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if worker is not None:
            worker.close()
        if sampler is not None:
            report["resource_observation"] = sampler.close()
        report["elapsed_seconds"] = round(time.time() - started, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("TSP_TEST_POSTGRES_DSN"),
        help="Disposable PostgreSQL root DSN; defaults to TSP_TEST_POSTGRES_DSN",
    )
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--pool-size", type=int, default=32)
    parser.add_argument("--ledger-rows", type=int, default=240)
    parser.add_argument("--nav-rows", type=int, default=180)
    parser.add_argument("--research-concurrency", type=int, default=4)
    parser.add_argument("--research-delay", type=float, default=0.02)
    parser.add_argument("--steady-request-interval", type=float, default=5)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "../reports/phase2-step2-20261016/"
            "local-100-user-mixed-acceptance.json"
        ),
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or TSP_TEST_POSTGRES_DSN is required")
    if not 1 <= args.users <= 100:
        parser.error("--users must be in [1, 100]")
    if not 1 <= args.rounds <= 20:
        parser.error("--rounds must be in [1, 20]")
    if not 4 <= args.pool_size <= 32:
        parser.error("--pool-size must be in [4, 32]")
    if not 51 <= args.ledger_rows <= 5000:
        parser.error("--ledger-rows must be in [51, 5000]")
    if not 61 <= args.nav_rows <= 2000:
        parser.error("--nav-rows must be in [61, 2000]")
    if not 1 <= args.research_concurrency <= 4:
        parser.error("--research-concurrency must be in [1, 4]")
    if not 0 <= args.research_delay <= 5:
        parser.error("--research-delay must be in [0, 5]")
    if not 0 <= args.steady_request_interval <= 10:
        parser.error("--steady-request-interval must be in [0, 10]")
    result = run(
        args.dsn,
        args.users,
        args.rounds,
        args.pool_size,
        args.ledger_rows,
        args.nav_rows,
        args.research_concurrency,
        args.research_delay,
        args.steady_request_interval,
        args.output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
