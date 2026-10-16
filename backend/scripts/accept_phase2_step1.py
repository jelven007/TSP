"""Run phase-two step-one functional acceptance against disposable PostgreSQL.

The supplied root DSN must point to a disposable PostgreSQL server where the
current user may create and drop databases and roles.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
from fastapi import FastAPI
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr

from app.api import events as event_api
from app.api import strategy as strategy_api
from app.api import watchlist as watchlist_api
from app.cell.admin import (
    add_user,
    bootstrap_full_identity,
    migrate,
    provision_tenant,
    register_cell,
)
from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.cell.database import Databases
from app.multitenancy import access
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import EventHub, append_event
from app.multitenancy.identity import AccountIdentity
from app.multitenancy.stream_auth import (
    RevocationListener,
    StreamTicketCodec,
    notify_access_change,
)
from app.services.quote_service import QuoteService
from app.strategy.engine import StrategyEngine

PASSWORD = "phase2-step1-password"
BASE_URL = "https://phase2-step1.test"


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _latency(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "p99_ms": round(_percentile(values, 0.99), 3),
        "max_ms": round(max(values, default=0), 3),
    }


def _git_state(root: Path) -> dict:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--short"], cwd=root, text=True,
    )
    return {
        "head": head,
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
    }


@contextmanager
def _cluster(
    root_dsn: str,
    pool_size: int,
    *,
    name_prefix="tsp_p2s1_",
    base_url=BASE_URL,
):
    prefix = name_prefix + uuid.uuid4().hex[:10]
    dbnames = [prefix + "_control", prefix + "_data"]
    roles = [prefix + "_identity", prefix + "_business"]
    with psycopg.connect(root_dsn, autocommit=True, row_factory=dict_row) as root:
        try:
            for role in roles:
                root.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(role),
                        sql.Literal(PASSWORD),
                    )
                )
            for name in dbnames:
                root.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with ExitStack() as resources:
                admins = [
                    resources.enter_context(
                        psycopg.connect(
                            make_conninfo(root_dsn, dbname=name),
                            autocommit=True,
                            row_factory=dict_row,
                        )
                    )
                    for name in dbnames
                ]
                for kind, conn, role in zip(
                    ("control", "data"), admins, roles, strict=True,
                ):
                    migrate(conn, kind, role, "full", target_version=6)
                register_cell(admins[0], "full", base_url)
                bootstrap_full_identity(admins[0])
                settings = CellSettings(
                    id="full",
                    schema_version=6,
                    pool_size=pool_size,
                    control_dsn=make_conninfo(
                        root_dsn,
                        dbname=dbnames[0],
                        user=roles[0],
                        password=PASSWORD,
                    ),
                    data_dsn=make_conninfo(
                        root_dsn,
                        dbname=dbnames[1],
                        user=roles[1],
                        password=PASSWORD,
                    ),
                    stream_ticket_secret=SecretStr("phase2-step1-" + "s" * 32),
                )
                db = Databases(settings)
                db.open()
                resources.callback(db.close)
                yield SimpleNamespace(
                    control=admins[0],
                    data=admins[1],
                    db=db,
                    identity=AccountIdentity(db),
                    settings=settings,
                    dbnames=tuple(dbnames),
                )
        finally:
            for name in reversed(dbnames):
                root.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
            for role in reversed(roles):
                root.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def _seed(cluster, count: int, *, email_prefix="phase2-step1") -> list[dict]:
    principals = []
    for index in range(count):
        email = f"{email_prefix}-{index:04d}@example.test"
        user_id = add_user(cluster.control, email, PASSWORD)
        tenant_id = uuid.uuid4().hex
        provision_tenant(
            cluster.control,
            cluster.data,
            tenant_id,
            f"验收租户 {index:04d}",
            "full",
            user_id,
        )
        cluster.control.execute(
            "UPDATE control.users SET default_tenant_id=%s WHERE id=%s",
            (tenant_id, user_id),
        )
        principals.append(
            {
                "index": index,
                "email": email,
                "user_id": user_id,
                "tenant_id": tenant_id,
            }
        )
    return principals


def _login(cluster, principals: list[dict]) -> dict:
    def one(principal):
        started = time.perf_counter()
        token, account = cluster.identity.login(
            principal["email"],
            PASSWORD,
            f"198.51.100.{principal['index'] + 1}",
        )
        if account["id"] != principal["user_id"]:
            raise AssertionError("login returned the wrong account")
        return {
            **principal,
            "token": token,
            "account": account,
            "login_ms": (time.perf_counter() - started) * 1000,
        }

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(32, len(principals))) as executor:
        logged_in = list(executor.map(one, principals))
    return {
        "principals": logged_in,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "latency": _latency([item["login_ms"] for item in logged_in]),
    }


def _app(cluster, root: Path) -> tuple[FastAPI, StreamTicketCodec]:
    app = FastAPI()
    app.state.service_role = "api"
    app.state.account_identity = cluster.identity
    app.state.quote_service = SimpleNamespace(ensure_fresh=lambda: None)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=root),
        get_name_map=lambda symbols: {},
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
    app.include_router(event_api.router)
    return app, codec


async def _http_round(
    client: httpx.AsyncClient,
    codec: StreamTicketCodec,
    principals: list[dict],
    round_index: int,
) -> dict:
    async def one(principal):
        started = time.perf_counter()
        headers = {
            "Cookie": f"tf_session={principal['token']}",
            "Origin": BASE_URL,
        }
        note = f"user-{principal['index']}-round-{round_index}"
        saved = await client.post(
            "/api/watchlist",
            headers=headers,
            json={"symbol": "600000.SH", "note": note},
        )
        listed = await client.get("/api/watchlist", headers=headers)
        strategies = await client.get("/api/strategies", headers=headers)
        ticket = await client.post("/api/events/ticket", headers=headers)
        responses = (saved, listed, strategies, ticket)
        if any(response.status_code != 200 for response in responses):
            raise AssertionError(
                f"user {principal['index']} statuses "
                f"{[response.status_code for response in responses]}"
            )
        rows = listed.json()["symbols"]
        if len(rows) != 1 or rows[0]["symbol"] != "600000.SH" or rows[0]["note"] != note:
            raise AssertionError(f"user {principal['index']} read another tenant's watchlist")
        if not strategies.json()["strategies"]:
            raise AssertionError("strategy catalog is unexpectedly empty")
        claims = codec.consume(ticket.json()["ticket"])
        if claims is None or claims.tenant_id != principal["tenant_id"]:
            raise AssertionError("stream ticket lost its tenant binding")
        if codec.consume(ticket.json()["ticket"]) is not None:
            raise AssertionError("stream ticket replay was accepted")
        return (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    latencies = await asyncio.gather(*(one(principal) for principal in principals))
    return {
        "round": round_index + 1,
        "successes": len(latencies),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "workflow_latency": _latency(latencies),
    }


async def _http_workflows(
    app: FastAPI,
    codec: StreamTicketCodec,
    principals: list[dict],
    rounds: int,
) -> list[dict]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
        timeout=30,
    ) as client:
        return [
            await _http_round(client, codec, principals, index)
            for index in range(rounds)
        ]


def _contexts(cluster, principals: list[dict]):
    return [
        cluster.identity.context(
            principal["account"],
            principal["tenant_id"],
            f"acceptance-{principal['index']}",
        )
        for principal in principals
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
                    {"owner": index},
                )
        received: dict[int, list[dict]] = {}
        deadline = time.time() + 15
        while len(received) < len(subscriptions) and time.time() < deadline:
            for index, subscription in enumerate(subscriptions):
                rows = subscription.pop()
                if rows:
                    received.setdefault(index, []).extend(rows)
            if len(received) < len(subscriptions):
                time.sleep(0.02)
        for index in range(len(subscriptions)):
            rows = received.get(index, [])
            if len(rows) != 1 or rows[0]["payload"] != {"owner": index}:
                raise AssertionError(f"private event isolation failed for user {index}")
        return {
            "successes": len(received),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "hub": hub.status(),
        }
    finally:
        hub.close()


def _public_markers(count: int) -> dict:
    service = QuoteService()
    subscribers = [service.subscribe() for _ in range(count)]
    started = time.perf_counter()
    service._broadcast_quote_updated()
    service.notify_depth_updated()
    received = [
        value
        for subscriber in subscribers
        if (value := subscriber.pop())["quote_updated"] and value["depth_updated"]
    ]
    for subscriber in subscribers:
        service.unsubscribe(subscriber)
    if len(received) != count:
        raise AssertionError("public marker broadcast lost subscribers")
    return {
        "successes": len(received),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _revocation(cluster, codec: StreamTicketCodec, principal: dict, context) -> dict:
    listener = RevocationListener(cluster.settings.control_dsn.get_secret_value())
    listener.start()
    try:
        deadline = time.time() + 10
        while not listener.connected and time.time() < deadline:
            time.sleep(0.02)
        if not listener.connected:
            raise AssertionError("revocation LISTEN did not connect")
        ticket, _ = codec.issue(
            principal["account"],
            context,
            principal="session:acceptance-revocation",
        )
        claims = codec.consume(ticket)
        listener.ensure_active(claims)
        started = time.perf_counter()
        with cluster.control.transaction():
            notify_access_change(
                cluster.control,
                principal="session:acceptance-revocation",
            )
        deadline = time.time() + 5
        revoked = False
        while time.time() < deadline:
            try:
                listener.ensure_active(claims)
            except AccessError:
                revoked = True
                break
            time.sleep(0.01)
        if not revoked:
            raise AssertionError("revocation notification did not terminate authorization")
        return {
            "revoked": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "listener": listener.status(),
        }
    finally:
        listener.close()


def run(root_dsn: str, users: int, rounds: int, pool_size: int, output: Path) -> dict:
    repository = Path(__file__).resolve().parents[2]
    started = time.time()
    report = {
        "status": "failed",
        "scope": "local_disposable_postgresql_functional_acceptance",
        "limits": [
            "not a production or candidate-host deployment",
            "not a real trading-session market freshness measurement",
            "ASGI requests run in process; network proxy capacity is not measured",
        ],
        "parameters": {
            "users": users,
            "rounds": rounds,
            "pool_size": pool_size,
        },
        "git": _git_state(repository),
    }
    try:
        with _cluster(root_dsn, pool_size) as cluster:
            seeded = _seed(cluster, users)
            login = _login(cluster, seeded)
            principals = login.pop("principals")
            app, codec = _app(cluster, repository)
            workflows = asyncio.run(
                _http_workflows(app, codec, principals, rounds)
            )
            contexts = _contexts(cluster, principals)
            report.update(
                {
                    "login": login,
                    "http_workflows": workflows,
                    "private_events": _private_events(cluster, contexts),
                    "public_markers": _public_markers(users),
                    "revocation": _revocation(
                        cluster,
                        codec,
                        principals[0],
                        contexts[0],
                    ),
                    "status": "passed",
                }
            )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["elapsed_seconds"] = round(time.time() - started, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
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
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--pool-size", type=int, default=32)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("../reports/phase2-step1-20261016/local-100-user-acceptance.json"),
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or TSP_TEST_POSTGRES_DSN is required")
    if not 1 <= args.users <= 1000:
        parser.error("--users must be in [1, 1000]")
    if not 1 <= args.rounds <= 100:
        parser.error("--rounds must be in [1, 100]")
    if not 4 <= args.pool_size <= 32:
        parser.error("--pool-size must be in [4, 32]")
    result = run(args.dsn, args.users, args.rounds, args.pool_size, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
