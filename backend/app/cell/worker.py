"""Bounded Cell worker: durable SQL claims, per-tenant RLS, isolated strategy subprocess."""

import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from app.cell.alerts import Alerts, append_alert
from app.cell.automation import Automations
from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.cell.database import Databases
from app.cell.identity import Identity
from app.cell.market import HttpMarketGateway, MarketGateway
from app.cell.notifications import Notifications
from app.cell.paper import Paper
from app.cell.scheduler import Scheduler

logger = logging.getLogger(__name__)


def run_compute(payload: dict, timeout: int) -> dict:
    # Built-in strategies only. Process receives market input and no app/database/AI env.
    allowed = {"PATH", "SYSTEMROOT", "LANG", "TZ", "PYTHONPATH"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({"POLARS_MAX_THREADS": "2", "NUMBA_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1",
                        "OMP_NUM_THREADS": "1", "NUMBA_CACHE_DIR": "/tmp/tsp-numba", "TICKFLOW_ENV_FILE": "/dev/null"})
    encoded = json.dumps(payload, default=str, allow_nan=False)
    if len(encoded.encode()) > 64 * 1024 * 1024:
        raise ValueError("strategy_input_limit")
    environment["TSP_COMPUTE_CPU_SECONDS"] = str(timeout)
    result = subprocess.run(
        [sys.executable, "-m", "app.cell.compute"],
        input=encoded, text=True, capture_output=True,
        timeout=timeout, env=environment, check=True,
    )
    return json.loads(result.stdout)


class CellWorker:
    def __init__(self, db: Databases, market: MarketGateway, compute=run_compute):
        self.db, self.market, self.compute = db, market, compute
        self.identity = Identity(db)
        self.scheduler = Scheduler(db)
        self.automations = Automations(db, market)
        self.notifications = Notifications(db)
        self._executor = ThreadPoolExecutor(max_workers=db.settings.worker_concurrency)

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def authorize_job(control, ctx, job) -> None:
        Identity.authorize_background(control, ctx, job, {"strategies:write"})

    def process_jobs(self, tenant_id: str) -> int:
        with self.identity.worker_context(tenant_id) as (ctx, _), self.db.business_transaction(
            ctx, write=True, permission="strategies:write"
        ) as conn:
            # Expired attempts cannot later commit; they require explicit resubmission.
            conn.execute(
                """UPDATE business.strategy_jobs SET status='interrupted',finished_at=now(),execution_epoch=execution_epoch+1
                   WHERE status IN ('running','cancelling') AND lease_until IS NOT NULL AND lease_until < now()"""
            )
            conn.execute(
                "UPDATE business.strategy_jobs SET status='cancelled',finished_at=now() WHERE status='cancelling' AND lease_until IS NULL"
            )
            job = conn.execute(
                """SELECT * FROM business.strategy_jobs WHERE status='queued' ORDER BY created_at
                   FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not job:
                return 0
            epoch = job["execution_epoch"] + 1
            conn.execute(
                """UPDATE business.strategy_jobs SET status='running',started_at=now(),execution_epoch=%s,lease_until=%s
                   WHERE id=%s""",
                (epoch, datetime.now(UTC) + timedelta(seconds=self.db.settings.worker_timeout + 30), job["id"]),
            )
        error = None
        result = None
        try:
            with self.identity.worker_context(tenant_id) as (ctx, control):
                self.authorize_job(control, ctx, job)
            payload = job["input"]
            payload["market"] = self.market.history(payload["as_of"], payload["symbols"], payload.get("history_bars", 120))
            result = self.compute(payload, self.db.settings.worker_timeout)
        except AccessError as exc:
            error = exc.code
        except Exception:
            error = "strategy_execution_failed"
        # No DB transaction/credential is held during compute. Re-authorize final commit.
        with self.identity.worker_context(tenant_id) as (ctx, control), self.db.business_transaction(
            ctx, write=True, permission="strategies:write"
        ) as conn:
            current = conn.execute(
                "SELECT * FROM business.strategy_jobs WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if current["execution_epoch"] != epoch or not current["lease_until"] or current["lease_until"] < datetime.now(UTC):
                return 0
            if current["status"] == "cancelling":
                status = "cancelled"
                result = None
            elif current["status"] != "running":
                return 0
            else:
                try:
                    self.authorize_job(control, ctx, job)
                except AccessError as exc:
                    error = exc.code
                status = "failed" if error else "succeeded"
            conn.execute(
                "UPDATE business.strategy_jobs SET status=%s,result=%s,error=%s,finished_at=now(),lease_until=NULL WHERE id=%s",
                (status, Jsonb(result) if result and not error else None, error, job["id"]),
            )
            if status == "succeeded":
                Automations.capture_result(conn, ctx, job, result)
                append_alert(conn, tenant_id, "strategy:" + job["id"], {
                    "source": "strategy", "message": f"策略运行完成, 命中 {result['total']} 只证券",
                    "strategy_id": job["strategy_id"], "severity": "info",
                })
        return 1

    def symbols(self, tenant_id):
        with self.identity.worker_context(tenant_id) as (ctx, _), self.db.business_transaction(
            ctx, permission="alerts:write"
        ) as conn:
            rules = conn.execute("SELECT definition FROM business.monitor_rules WHERE enabled").fetchall()
            orders = conn.execute("SELECT symbol FROM business.paper_orders WHERE status='queued'").fetchall()
            holdings = conn.execute(
                """SELECT DISTINCT f.symbol FROM business.paper_fills f JOIN business.trade_automations a
                   ON a.account_id=f.account_id AND a.tenant_id=f.tenant_id WHERE a.enabled"""
            ).fetchall()
            return sorted({symbol for rule in rules for symbol in rule["definition"]["symbols"]} |
                          {row["symbol"] for row in orders + holdings})

    def evaluate(self, tenant_id: str, quotes=None) -> dict:
        if quotes is None:
            symbols = self.symbols(tenant_id)
            quotes = {}
            for start in range(0, len(symbols), 1000):
                quotes.update(self.market.quotes(symbols[start:start + 1000]))
        with self.identity.worker_context(tenant_id) as (ctx, control), self.db.business_transaction(
            ctx, write=True, permission="alerts:write"
        ) as conn:
            rules = Automations.authorized_rules(conn, control, ctx)
            Automations.risk_exits(conn, ctx, rules, quotes)
            return {
                "alerts": Alerts.evaluate(conn, ctx, quotes), "fills": Paper.match(conn, ctx, quotes),
                "next_due": Scheduler.next_due(conn, "realtime", self.db.settings.realtime_interval),
            }

    def tick(self) -> int:
        kind = self.db.settings.worker_kind
        claims = self.scheduler.claim(kind)
        shared = {}
        if kind == "realtime" and claims:
            symbols = set()
            try:
                def subscriptions(claim):
                    try:
                        return self.symbols(claim["tenant_id"])
                    except AccessError:
                        return []
                for batch in self._executor.map(subscriptions, claims):
                    symbols.update(batch)
                ordered = sorted(symbols)
                for start in range(0, len(ordered), 1000):
                    shared.update(self.market.quotes(ordered[start:start + 1000]))
            except Exception:
                for claim in claims:
                    self.scheduler.finish(claim, datetime.now(UTC) + timedelta(seconds=5))
                return 0

        def execute(claim):
            due = datetime.now(UTC) + timedelta(seconds=5)
            try:
                if kind == "strategies":
                    self.process_jobs(claim["tenant_id"])
                elif kind == "notifications":
                    self.notifications.process(claim["tenant_id"])
                elif kind == "automation":
                    self.automations.process(claim["tenant_id"])
                else:
                    due = self.evaluate(claim["tenant_id"], shared)["next_due"]
                if kind != "realtime":
                    with self.identity.worker_context(claim["tenant_id"]) as (ctx, _), self.db.business_transaction(
                        ctx, permission="alerts:write"
                    ) as conn:
                        due = Scheduler.next_due(conn, kind, self.db.settings.realtime_interval)
            except AccessError:
                # Keep a bounded wake-up for suspended work so reactivation can resume.
                due = datetime.now(UTC) + timedelta(seconds=60)
            except Exception:
                logger.exception("Cell worker failed tenant_id=%s kind=%s", claim["tenant_id"], kind)
            finally:
                self.scheduler.finish(claim, due)

        if claims:
            list(self._executor.map(execute, claims))
        return len(claims)


def main():
    settings = CellSettings()
    if settings.worker_kind != "notifications" and (not settings.market_url or not settings.market_token.get_secret_value()):
        raise SystemExit("Worker requires TSP_CELL_MARKET_URL and TSP_CELL_MARKET_TOKEN")
    db = Databases(settings)
    market = HttpMarketGateway(settings.market_url, settings.market_token.get_secret_value())
    db.open()
    try:
        worker = CellWorker(db, market)
        while True:
            if not worker.tick():
                time.sleep(.25)
    finally:
        worker.close()
        market.close()
        db.close()


if __name__ == "__main__":
    main()
