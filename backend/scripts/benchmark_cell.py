"""Disposable PostgreSQL capacity benchmark. Never uses a production DSN or market feed.

TSP_TEST_POSTGRES_DSN must reference a dedicated test server. Creates randomized
databases/roles and drops only those resources at exit. Writes measured JSON.
"""

import argparse
import json
import os
import statistics
import threading
import time
import uuid
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.cell.admin import migrate, register_cell
from app.cell.config import CellSettings
from app.cell.database import Databases
from app.cell.main import create_app
from app.cell.market import MarketQuote
from app.cell.passwords import hash_password
from app.cell.worker import CellWorker


def percentiles(values):
    ordered = sorted(values)
    def at(p):
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 3)
    return {"p50_ms": at(.5), "p95_ms": at(.95), "p99_ms": at(.99), "max_ms": at(1)}


class Market:
    def __init__(self):
        self.calls = 0
    def quotes(self, symbols):
        self.calls += 1
        return {s: MarketQuote(symbol=s, price=10, change_pct=.01, source_time=datetime.now(UTC),
                               sequence=getattr(self, "sequence", 100), trading=True, suspended=False, limits_known=True,
                               limit_up=11, limit_down=9) for s in symbols}
    def history(self, *args):
        raise AssertionError("benchmark must not compute strategies")


def benchmark(control, data, settings, count, baseline, *, rounds=1, rules_per_tenant=1, quote_delay_ms=0, trigger=False, cadence_seconds=8):
    register_cell(control, "capacity", "http://127.0.0.1:3020")
    salt, digest = hash_password("benchmark-local-only")
    control.execute(
        "INSERT INTO control.users(id,email,password_salt,password_hash) VALUES ('bench','bench@example.test',%s,%s)",
        (salt, digest),
    )
    control.execute(
        """INSERT INTO control.tenants(id,name,cell_id,status)
           SELECT 'capacity-'||lpad(i::text,5,'0'),'Capacity','capacity','active' FROM generate_series(1,%s) i""", (count,),
    )
    control.execute("INSERT INTO control.memberships SELECT id,'bench','owner' FROM control.tenants")
    data.execute("INSERT INTO business.tenant_bindings SELECT id,1,true FROM (SELECT 'capacity-'||lpad(i::text,5,'0') id FROM generate_series(1,%s) i) s", (count,))
    data.execute("ANALYZE")
    control.execute("ANALYZE")
    class DelayedMarket(Market):
        def quotes(self, symbols):
            if quote_delay_ms:
                time.sleep(quote_delay_ms / 1000)
            self.sequence += 1
            return super().quotes(symbols)
    market = DelayedMarket()
    market.sequence = 100
    db = Databases(settings)
    db.open()
    try:
        worker = CellWorker(db, market)
        result = {"tenants": count, "postgresql_version": control.execute("SELECT version() AS v").fetchone()["v"],
                  "worker_concurrency": settings.worker_concurrency, "pool_size_each": settings.pool_size,
                  "rules_per_tenant": rules_per_tenant, "quote_delay_ms": quote_delay_ms,
                  "triggered_alerts": trigger,
                  "market": "standardized synthetic quotes; simulated network delay; no strategy compute"}
        if baseline:
            started = time.monotonic()
            for i in range(1, count + 1):
                worker.evaluate(f"capacity-{i:05d}")
            result["baseline_idle_full_scan"] = {"seconds": round(time.monotonic() - started, 3),
                                                  "evaluated_tenants": count, "quote_requests": market.calls}
        times = []
        for _ in range(100):
            started = time.monotonic()
            assert worker.tick() == 0
            times.append((time.monotonic() - started) * 1000)
        result["idle_indexed_100_ticks"] = {**percentiles(times), "evaluated_tenants": 0, "quote_requests": market.calls}

        api_times, api_errors = [], []
        stop = threading.Event()
        with TestClient(create_app(settings)) as client:
            token = client.post("/api/auth/login", json={"email": "bench@example.test", "password": "benchmark-local-only"}).json()["token"]
            def foreground():
                while not stop.is_set():
                    start = time.monotonic()
                    response = client.get("/api/tenants/capacity-00001/watchlist",
                                          headers={"Authorization": "Bearer " + token, "X-Routing-Epoch": "1"})
                    api_times.append((time.monotonic() - start) * 1000)
                    if response.status_code != 200:
                        api_errors.append(response.status_code)
                    stop.wait(.05)
            thread = threading.Thread(target=foreground, daemon=True)
            data.execute(
                """INSERT INTO business.monitor_rules(tenant_id,id,definition)
                   SELECT tenant_id,'rule-'||i,'{"name":"capacity","symbols":["000001.SZ"],"field":"price",
                   "op":">","value":99,"cooldown_seconds":300,"severity":"info"}'::jsonb
                   FROM business.tenant_bindings CROSS JOIN generate_series(1,%s) i""", (rules_per_tenant,),
            )
            data.execute("ANALYZE")
            if trigger:
                data.execute(
                    "UPDATE business.monitor_rules SET definition=jsonb_set(definition,'{value}','9'::jsonb)"
                )
            thread.start()
            started = time.monotonic()
            tick_times, cycle_times = [], []
            try:
                for _ in range(rounds):
                    data.execute("UPDATE scheduler.work SET due_at=now() WHERE kind='realtime'")
                    evaluated = 0
                    cycle = time.monotonic()
                    while evaluated < count:
                        tick = time.monotonic()
                        batch = worker.tick()
                        tick_times.append((time.monotonic() - tick) * 1000)
                        if not batch:
                            raise AssertionError("active scheduler stalled")
                        evaluated += batch
                    cycle_times.append((time.monotonic() - cycle) * 1000)
                elapsed = time.monotonic() - started
            finally:
                stop.set()
                thread.join(timeout=10)
            visited = data.execute("SELECT count(DISTINCT tenant_id) AS n FROM business.monitor_state").fetchone()["n"]
            assert visited == count, (visited, count)
            expected_states = count * rules_per_tenant
            assert data.execute("SELECT count(*) AS n FROM business.monitor_state").fetchone()["n"] == expected_states
            result["all_active_one_round"] = {
                "seconds": round(cycle_times[0] / 1000, 3), "distinct_tenants_evaluated": visited,
                "tenants_per_second": round(count * rounds / elapsed, 2),
                "batch_latency": percentiles(tick_times), "quote_requests": market.calls,
                "alerts": data.execute("SELECT count(*) AS n FROM business.alerts").fetchone()["n"],
            }
            result["foreground_api_under_load"] = {"requests": len(api_times), "errors": api_errors,
                                                    **percentiles(api_times)}
            result["refresh_rounds"] = {"rounds": rounds, **percentiles(cycle_times),
                                        "all_rounds_below_2s": max(cycle_times) < 2000}
            # Exercise the real due_at cadence rather than forcibly waking each round.
            if rounds > 1:
                settings_interval = worker.db.settings.realtime_interval
                worker.db.settings = worker.db.settings.model_copy(update={"realtime_interval": 1})
                data.execute("UPDATE scheduler.work SET due_at=now() WHERE kind='realtime'")
                observations = {}
                original_evaluate = worker.evaluate
                observation_lock = threading.Lock()
                def timed_evaluate(tenant_id, quotes=None):
                    outcome = original_evaluate(tenant_id, quotes)
                    with observation_lock:
                        observations.setdefault(tenant_id, []).append(time.monotonic())
                    return outcome
                worker.evaluate = timed_evaluate
                stop.clear()
                thread = threading.Thread(target=foreground, daemon=True)
                thread.start()
                scheduled_start = time.monotonic()
                deadline = scheduled_start + cadence_seconds
                while time.monotonic() < deadline:
                    if not worker.tick():
                        time.sleep(.25)
                stop.set()
                thread.join(timeout=10)
                worker.evaluate = original_evaluate
                worker.db.settings = worker.db.settings.model_copy(update={"realtime_interval": settings_interval})
                gaps = [(b-a)*1000 for timestamps in observations.values() for a,b in pairwise(timestamps)]
                result["scheduled_refresh"] = {
                    "duration_seconds": cadence_seconds,
                    "tenants_observed": len(observations),
                    "minimum_evaluations": min(map(len, observations.values())),
                    "gaps": percentiles(gaps), "all_gaps_below_2s": max(gaps) < 2000,
                }
                result["foreground_api_under_load"] = {"requests": len(api_times), "errors": api_errors,
                                                        **percentiles(api_times)}
        # Measured queue orchestration, explicitly excludes expensive strategy compute.
        data.execute("DELETE FROM scheduler.work")
        data.execute(
            "INSERT INTO scheduler.work(tenant_id,kind,due_at) SELECT tenant_id,'strategies',now() FROM business.tenant_bindings"
        )
        started = time.monotonic()
        claimed = set()
        while len(claimed) < count:
            batch = worker.scheduler.claim("strategies", 4)
            if not batch:
                raise AssertionError("strategy scheduling stalled")
            for claim in batch:
                assert claim["tenant_id"] not in claimed
                claimed.add(claim["tenant_id"])
                assert worker.scheduler.finish(claim, None)
        result["strategy_queue_orchestration_only"] = {
            "seconds": round(time.monotonic() - started, 3), "unique_tenants": len(claimed),
            "cell_concurrent_job_budget": 4,
        }
        result["database_size_bytes"] = data.execute("SELECT pg_database_size(current_database()) AS n").fetchone()["n"]
        result["postgres_connections"] = control.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=ANY(%s)", ([control.info.dbname, data.info.dbname],)
        ).fetchone()["n"]
        result["tick_mean_ms"] = round(statistics.mean(tick_times), 3)
        return result
    finally:
        worker.close()
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=int, default=10000)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--rules-per-tenant", type=int, default=1)
    parser.add_argument("--quote-delay-ms", type=float, default=0)
    parser.add_argument("--trigger", action="store_true")
    parser.add_argument("--cadence-seconds", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.tenants <= 100000:
        parser.error("tenants must be 1..100000")
    if not 1 <= args.rounds <= 100 or not 1 <= args.rules_per_tenant <= 100 or not 0 <= args.quote_delay_ms <= 1000:
        parser.error("invalid rounds/rules/delay")
    if not 3 <= args.cadence_seconds <= 300:
        parser.error("cadence seconds must be 3..300")
    dsn = os.environ.get("TSP_TEST_POSTGRES_DSN")
    if not dsn:
        parser.error("TSP_TEST_POSTGRES_DSN is required")
    prefix = "tsp_capacity_" + uuid.uuid4().hex[:10]
    names = [prefix + "_control", prefix + "_data"]
    roles = [prefix + "_identity", prefix + "_runtime"]
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as root:
        try:
            for role in roles:
                root.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'capacity-local-only' NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
            for name in names:
                root.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with psycopg.connect(make_conninfo(dsn, dbname=names[0]), autocommit=True, row_factory=dict_row) as control, psycopg.connect(
                make_conninfo(dsn, dbname=names[1]), autocommit=True, row_factory=dict_row
            ) as data:
                migrate(control, "control", roles[0])
                migrate(data, "data", roles[1], "capacity")
                data.execute(
                    "UPDATE scheduler.capacity SET max_tenants=%s,max_realtime_units=%s,max_tenant_units=10000",
                    (max(1000, args.tenants), max(200, args.tenants * args.rules_per_tenant)),
                )
                settings = CellSettings(
                    id="capacity", pool_size=16, worker_concurrency=32,
                    # Long interval guarantees one pass without reprocessing early tenants.
                    realtime_interval=60,
                    control_dsn=make_conninfo(dsn, dbname=names[0], user=roles[0], password="capacity-local-only"),
                    data_dsn=make_conninfo(dsn, dbname=names[1], user=roles[1], password="capacity-local-only"),
                )
                # This benchmark measures a single round, not the periodic scheduler.
                # Avoid processing early tenants twice on slow hosts.
                settings = settings.model_copy(update={"realtime_interval": 3600})
                result = benchmark(control, data, settings, args.tenants, args.baseline,
                                   rounds=args.rounds, rules_per_tenant=args.rules_per_tenant,
                                   quote_delay_ms=args.quote_delay_ms, trigger=args.trigger,
                                   cadence_seconds=args.cadence_seconds)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
                print(json.dumps(result, ensure_ascii=False))
        finally:
            for name in names:
                root.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in roles:
                root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


if __name__ == "__main__":
    main()
