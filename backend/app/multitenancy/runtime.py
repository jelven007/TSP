"""Full monitor/trading worker with durable state and fenced scheduler commits."""
from __future__ import annotations

import hashlib
import json
import logging
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.cell.database import Databases
from app.cell.scheduler import Scheduler
from app.market_time import cn_today
from app.multitenancy.alerts import append_in_transaction
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import append_event
from app.multitenancy.executor import ComputeExecutor
from app.multitenancy.identity import AccountIdentity
from app.multitenancy.market import PublicMarket, decode_frame
from app.multitenancy.monitor_compute import evaluate_snapshot, fresh_frame, needs_sandbox
from app.multitenancy.paper_storage import PaperSession, TenantPaper
from app.multitenancy.research import authorize
from app.strategy import paper, paper_auto

logger = logging.getLogger(__name__)
INPUT_NAMESPACES = ("strategy_sources", "strategy_overrides", "custom_factors", "custom_signals", "monitor_runtime")


def fingerprint(conn, *, include_state=True):
    documents = conn.execute(
        "SELECT namespace,id,revision FROM business.documents WHERE namespace=ANY(%s) ORDER BY namespace,id",
        ([name for name in INPUT_NAMESPACES if include_state or name != "monitor_runtime"],),
    ).fetchall()
    rules = conn.execute("SELECT id,version FROM business.monitor_rules ORDER BY id").fetchall()
    groups = conn.execute(
        """SELECT g.id,i.symbol FROM business.watchlist_groups g
           LEFT JOIN business.watchlist_group_items i ON i.tenant_id=g.tenant_id AND i.group_id=g.id
           ORDER BY g.id,i.symbol""",
    ).fetchall()
    return hashlib.sha256(json.dumps([documents, rules, groups], sort_keys=True).encode()).hexdigest()


def valid_claim(conn, claim):
    if claim is not None and not conn.execute(
        "SELECT scheduler.guard(%s,%s,%s) AS valid", (claim["tenant_id"], claim["kind"], claim["owner"]),
    ).fetchone()["valid"]:
        raise AccessError(409, "后台任务租约已过期")


class FullWorker:
    def __init__(self, db, repo, market, executor=None):
        self.db, self.repo, self.market = db, repo, market
        self.identity = AccountIdentity(db)
        self.executor = executor or ComputeExecutor()
        self.scheduler = Scheduler(db)
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=db.settings.worker_concurrency)

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def account(self, tenant_id):
        # Personal tenants run under their persisted owner, never an implicit
        # platform administrator that could revive a disabled user's work.
        with self.db.control.connection() as control:
            row = control.execute(
                """SELECT u.* FROM control.users u JOIN control.memberships m ON m.user_id=u.id
                   WHERE m.tenant_id=%s AND m.role='owner' ORDER BY u.created_at,u.id LIMIT 1""",
                (tenant_id,),
            ).fetchone()
            if not row or not row["active"] or row["account_state"] != "active":
                raise AccessError(403, "后台任务租户账户不可用")
            account = self.identity._public(control, row)
        return account, self.identity.context(account, tenant_id, uuid.uuid4().hex)

    def prepare(self, tenant_id, snapshot):
        account, context = self.account(tenant_id)
        documents = TenantDocuments(self.db, context)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction(write=False) as unit:
            conn = unit.connection
            definitions = conn.execute(
                "SELECT definition FROM business.monitor_rules WHERE enabled ORDER BY id",
            ).fetchall()
            rules = []
            for row in definitions:
                execution = row["definition"].get("_execution", {})
                if (execution.get("policy_version") != context.policy_version
                        or execution.get("routing_epoch") != context.routing_epoch):
                    continue
                try:
                    with authorize(self.identity, replace(context, user_id=execution["user_id"]), execution["auth_version"]):
                        rules.append(row["definition"])
                except AccessError:
                    continue
            groups = {row["id"]: [] for row in conn.execute("SELECT id FROM business.watchlist_groups").fetchall()}
            for row in conn.execute("SELECT group_id,symbol FROM business.watchlist_group_items").fetchall():
                groups[row["group_id"]].append(row["symbol"])
            payload = {
                "kind": "monitor", "tenant_id": tenant_id, "snapshot": snapshot, "rules": rules, "groups": groups,
                "state": unit.get("monitor_runtime", "default", {}),
                "sources": unit.values("strategy_sources"),
                "overrides": {row["id"]: row["value"] for row in unit.records("strategy_overrides")},
                "signals": unit.values("custom_signals"), "factors": unit.values("custom_factors"),
                **({"public_release_id": snapshot["public_release"]["id"]} if snapshot.get("public_release") else {}),
            }
            observed = fingerprint(conn)
        return account, context, payload, observed

    def evaluate(self, tenant_id, snapshot, claim=None):
        account, context, payload, observed = self.prepare(tenant_id, snapshot)
        sequence = int(snapshot.get("sequence", 0))
        now = time.time()
        fresh = (
            snapshot["status"].get("is_trading_hours")
            and 0 <= now - float(snapshot.get("captured_at", 0)) < 15
            and -2 <= now - sequence / 1000 <= 15
            and any(
                snapshot.get("days", {}).get(asset) == cn_today().isoformat()
                and not fresh_frame(decode_frame(encoded), now).is_empty()
                for asset, encoded in snapshot["frames"].items()
            )
        )
        previous_sequence = payload["state"].get("sequence", -1)
        result = None
        if fresh and sequence > previous_sequence and payload["rules"]:
            if needs_sandbox(payload["rules"]):
                cancelled = threading.Event()
                timer = threading.Timer(self.db.settings.worker_timeout, cancelled.set)
                timer.start()
                try:
                    result = self.executor.execute(payload, cancel=cancelled)
                finally:
                    timer.cancel()
            else:
                # Built-in rule expressions contain no Python execution. The
                # original engine accepts an explicit group snapshot.
                result = evaluate_snapshot({**payload, "signals": []}, self.repo)
        documents = TenantDocuments(self.db, context)
        with ExitStack() as locks:
            locks.enter_context(authorize(self.identity, context, account["auth_version"]))
            for user_id, version in {
                (rule["_execution"]["user_id"], rule["_execution"]["auth_version"]) for rule in payload["rules"]
            }:
                if user_id != context.user_id:
                    locks.enter_context(authorize(self.identity, replace(context, user_id=user_id), version))
            with documents.transaction() as unit:
                conn = unit.connection
                valid_claim(conn, claim)
                if fingerprint(conn) != observed:
                    raise AccessError(409, "监控配置已更新")
                events, execution_events = [], []
                if result is not None:
                    for index, event in enumerate(result["events"]):
                        event["id"] = hashlib.sha256(
                            f"{tenant_id}:{sequence}:{observed}:{index}".encode(),
                        ).hexdigest()
                    events = append_in_transaction(conn, tenant_id, result["events"])
                    for index, event in enumerate(result.get("execution_events", [])):
                        execution_events.append({
                            **event, "id": hashlib.sha256(
                                f"execution:{tenant_id}:{sequence}:{observed}:{index}".encode(),
                            ).hexdigest(),
                        })
                    unit.put("monitor_runtime", "default", {**result["state"], "sequence": sequence})
                    if result["results"]:
                        unit.put("monitor_results", "default", {
                            "sequence": sequence, "results": result["results"],
                            "fingerprint": fingerprint(conn, include_state=False),
                        })
                        append_event(conn, tenant_id, "strategy_results_updated", {"ts": int(now * 1000)})
                paper_documents = TenantPaper(self.db, context, self.repo.store.data_dir)
                session = PaperSession(paper_documents, conn, True)
                # Private computation can consume the quote's validity window.
                # Recheck before placing or retrying market orders.
                commit_now = time.time()
                fresh = fresh and (
                    0 <= commit_now - float(snapshot.get("captured_at", 0)) < 15
                    and -2 <= commit_now - sequence / 1000 <= 15
                )
                prices, opening_rows = {}, {}
                if fresh:
                    for asset in ("stock", "etf"):
                        if snapshot.get("days", {}).get(asset) != cn_today().isoformat():
                            continue
                        frame = decode_frame(snapshot["frames"][asset])
                        for row in frame.iter_rows(named=True):
                            stamp = row.get("quote_ts") or row.get("timestamp")
                            if isinstance(stamp, (float, int)):
                                stamp = stamp / 1000 if stamp > 1e12 else stamp
                                if -2 <= commit_now - stamp <= 15 and row.get("raw_close"):
                                    prices[row["symbol"]] = (row["raw_close"], stamp)
                                    opening_rows[row["symbol"]] = row
                for account_id in session.account_ids():
                    paper_auto.retry_exit_intents(session, account_id, submit=False)
                    if fresh:
                        paper.evaluate_opening(session, opening_rows, account_id)
                        paper.evaluate_intraday(
                            session, {symbol: quote[0] for symbol, quote in prices.items()}, account_id,
                            quote_times={symbol: quote[1] for symbol, quote in prices.items()},
                        )
                        paper_auto.evaluate_position_risk(
                            session, {symbol: quote[0] for symbol, quote in prices.items()}, account_id,
                            quote_times={symbol: quote[1] for symbol, quote in prices.items()},
                        )
                    # Strategy notifications may be batched/cooling down. Only
                    # their separate per-security output drives strategy orders.
                    paper_auto.on_rule_events(
                        session,
                        execution_events + [event for event in events if event["source"] != "strategy"],
                        account_id, prices={symbol: quote[0] for symbol, quote in prices.items()},
                        allow_entries=fresh,
                    )
                self.settle(session, snapshot)
                for account_id in session.account_ids():
                    paper_auto.retry_exit_intents(session, account_id)
                valid_claim(conn, claim)
        return len(events)

    @staticmethod
    def settle(session, snapshot):
        days = sorted(day for day in snapshot.get("settlement_days", []) if day <= cn_today().isoformat())
        for account_id in session.account_ids():
            account = session.account(account_id)
            last_day = session.document("paper_settlement", account_id)
            start = last_day["day"] if last_day else account["created_at"][:10]
            for day in [day for day in days if day > start or (last_day is None and day == start)][:32]:
                paper_auto.retry_exit_intents(session, account_id, submit=False)
                paper.settle_day(session, day, account_id)
                paper_auto.retry_exit_intents(session, account_id)
                session.put_document("paper_settlement", account_id, {"day": day})

    def tick(self):
        claims = self.scheduler.claim("realtime")
        if not claims:
            return 0
        try:
            snapshot = self.market.snapshot()
        except AccessError:
            for claim in claims:
                self.scheduler.finish(claim, datetime.now(UTC) + timedelta(seconds=5))
            return 0
        def process(claim):
            due = datetime.now(UTC) + timedelta(seconds=self.db.settings.realtime_interval)
            try:
                self.evaluate(claim["tenant_id"], snapshot, claim)
                _account, context = self.account(claim["tenant_id"])
                with self.db.business_transaction(context) as conn:
                    active = conn.execute(
                        """SELECT EXISTS(SELECT 1 FROM business.monitor_rules WHERE enabled)
                           OR EXISTS(SELECT 1 FROM business.full_paper_accounts) AS active""",
                    ).fetchone()["active"]
                if not active:
                    due = None
                elif not snapshot["status"].get("is_trading_hours"):
                    due = datetime.now(UTC) + timedelta(seconds=30)
            except AccessError as exc:
                logger.info("full worker deferred: %s", exc.code)
                due = datetime.now(UTC) + timedelta(seconds=5)
            except Exception:
                logger.exception("full monitor/trading worker failed")
                due = datetime.now(UTC) + timedelta(seconds=5)
            finally:
                self.scheduler.finish(claim, due)
        list(self.pool.map(process, claims))
        return len(claims)


def main():
    from app.config import settings
    from app.multitenancy.health import WorkerHealth
    from app.tickflow.repository import DataStore, KlineRepository

    logging.basicConfig(level=logging.INFO)
    db = Databases(CellSettings())
    db.open()
    market = PublicMarket(db.settings)
    store = DataStore(settings.data_dir.resolve(), read_only=True)
    worker = FullWorker(db, KlineRepository(store), market)
    health = WorkerHealth(db, "realtime")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: worker.stop.set())
    try:
        while not worker.stop.is_set():
            try:
                current = settings.data_dir.resolve()
                if current != store.data_dir:
                    replacement = DataStore(current, read_only=True)
                    worker.repo = KlineRepository(replacement)
                    store.db.close()
                    store = replacement
                processed = worker.tick()
                health.record()
                if not processed:
                    worker.stop.wait(.25)
            except Exception:
                logger.exception("full worker tick failed")
                worker.stop.wait(5)
    finally:
        worker.close()
        market.close()
        store.db.close()
        db.close()


if __name__ == "__main__":
    main()
