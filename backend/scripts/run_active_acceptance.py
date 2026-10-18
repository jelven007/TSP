"""Real HTTP/SSE traffic from 100 isolated accounts, with durable strict evaluation."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.cell.config import CellSettings
from app.cell.database import Databases
from app.market_time import CN_TZ
from app.multitenancy.events import append_event
from app.multitenancy.identity import AccountIdentity
from scripts.observe_candidate_soak import write_json

BUCKETS = (50, 100, 200, 500, 1000, 2000, 5000, 15000, 60000, 900000)
TERMINAL = {"succeeded", "failed"}


def now_text():
    return datetime.now(CN_TZ).isoformat()


def percentile(histogram: list[int], ratio: float) -> int | None:
    target = math.ceil(sum(histogram) * ratio)
    if not target:
        return None
    count = 0
    for upper, samples in zip(BUCKETS, histogram, strict=True):
        count += samples
        if count >= target:
            return upper
    return BUCKETS[-1]


def classify_watchlist_mismatch(
    values: list[dict], *, tenant_id: str, symbol: str, round_index: int,
) -> str | None:
    expected_note = f"acceptance:{tenant_id}:{round_index}"
    if (
        len(values) == 1
        and values[0].get("note") == expected_note
        and values[0].get("symbol") == symbol
    ):
        return None
    for value in values:
        note = value.get("note")
        if not isinstance(note, str) or not note.startswith("acceptance:"):
            continue
        parts = note.split(":", 2)
        if len(parts) == 3 and parts[1] != tenant_id:
            return "isolation"
    return "consistency"


def evaluate(record: dict) -> dict:
    users = record["users"]
    expected_rounds = record["duration_hours"] * 3600 / record["round_seconds"]
    requests = sum(v["count"] for v in record["requests"].values())
    errors = sum(v["errors"] for v in record["requests"].values())
    checks = {
        "100_distinct_users_and_tenants": len(users) == 100
        and len({u["user_id"] for u in users}) == 100
        and len({u["tenant_id"] for u in users}) == 100,
        "complete_duration": record.get("observed_seconds", 0) >= record["duration_hours"] * 3600,
        "all_users_continuously_active": all(
            u["rounds"] - u.get("baseline_rounds", 0) >= expected_rounds * .95
            and u["max_activity_gap_s"] <= 60 for u in users),
        "http_error_rate_below_0_1_percent": requests > 0 and errors / requests < .001,
        "read_write_p95_under_2_seconds": all(
            (percentile(v["histogram"], .95) or 0) <= 2000
            for route, v in record["requests"].items() if route != "login"),
        "no_isolation_violation": record["isolation_violations"] == 0,
        "no_consistency_violation": record.get("consistency_violations", 0) == 0,
        "all_private_probes_delivered": record["private_probe_timeouts"] == 0
        and record.get("probe_injection_errors", 0) == 0
        and record.get("pending_private_probes", 0) == 0
        and all(u["private_received"] > 0 for u in users),
        "sse_available_99_percent_per_user": all(
            u["stream_samples"] > 0
            and u["stream_up_seconds"] + u["stream_down_seconds"] > 0
            and u["stream_up_seconds"] / (u["stream_up_seconds"] + u["stream_down_seconds"]) >= .99
            for u in users),
        "live_public_events_received": record["duration_hours"] < 72
        or all(u["public_events"] > 0 for u in users),
        "real_compute_succeeded": record["research"]["succeeded"] > 0
        and record["research"]["failed"] == 0 and record["research"].get("in_flight") is None,
        "all_users_exercised_research": record["duration_hours"] < 72
        or len(record["research"]["user_indices"]) == 100,
        "observer_continuous": record.get("restarts", 0) == 0
        and record.get("max_sample_gap_s", 0) <= 45,
    }
    return {"status": "succeeded" if all(checks.values()) else "failed", "checks": checks,
            "failed_checks": [k for k, v in checks.items() if not v],
            "requests": requests, "http_errors": errors,
            "certified_72h_100_active_users": record["duration_hours"] >= 72 and all(checks.values())}


class Load:
    def __init__(self, root: Path, run_dir: Path, base: str, hours: float):
        self.root, self.run_dir, self.base = root, run_dir, base.rstrip("/")
        self.accounts = json.loads((root / "private/accounts.json").read_text())
        if len(self.accounts) != 100:
            raise ValueError("exactly 100 independent accounts required")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "status.json"
        if self.path.exists():
            self.record = json.loads(self.path.read_text())
            if self.record["status"] not in TERMINAL:
                self.record["restarts"] += 1
        else:
            self.record = {
                "status": "preparing", "created_at": now_text(), "duration_hours": hours,
                "round_seconds": 20, "requests": {}, "isolation_violations": 0,
                "consistency_violations": 0,
                "private_probe_timeouts": 0, "probe_injection_errors": 0, "restarts": 0,
                "max_sample_gap_s": 0, "samples": 0, "errors": [],
                "research": {"succeeded": 0, "failed": 0, "user_indices": []},
                "users": [{
                    "index": p["index"], "user_id": p["user_id"], "tenant_id": p["tenant_id"],
                    "rounds": 0, "max_activity_gap_s": 0, "private_received": 0,
                    "public_events": 0, "stream_samples": 0, "stream_up_samples": 0,
                    "stream_up_seconds": 0., "stream_down_seconds": 0.,
                    "stream_reconnects": 0, "last_event_id": "",
                } for p in self.accounts],
            }
        self.record.setdefault("consistency_violations", 0)
        self.clients = [httpx.AsyncClient(
            base_url=self.base, headers={"Origin": self.base}, trust_env=False,
            timeout=httpx.Timeout(15, connect=5), limits=httpx.Limits(max_connections=5),
        ) for _ in self.accounts]
        self.live = [{"connected": False, "last_ok": 0., "frame_at": 0., "account": None,
                      "stream_changed_at": time.monotonic()}
                     for _ in self.accounts]
        self.pending = {}
        self.stopping = False
        self.next_research = 0
        self.credentials = json.loads((root / "private/probes.json").read_text())
        self.db = Databases(CellSettings(
            id="full", schema_version=6, pool_size=2,
            control_dsn=self.credentials["control"], data_dsn=self.credentials["data"],
        ))
        self.identity = AccountIdentity(self.db)

    def error(self, category: str, index: int, error: str):
        self.record["errors"] = [*self.record["errors"][-19:],
                                  {"at": now_text(), "kind": category, "user": index, "error": error}]

    def stream_state(self, index, connected=None):
        live, user = self.live[index], self.record["users"][index]
        now = time.monotonic()
        if self.record["status"] == "running":
            key = "stream_up_seconds" if live["connected"] else "stream_down_seconds"
            user[key] += now - live["stream_changed_at"]
        live["stream_changed_at"] = now
        if connected is not None:
            live["connected"] = connected

    async def request(self, index, method, path, *, route=None, expected=200, **kwargs):
        started = time.monotonic()
        response = None
        metric = self.record["requests"].setdefault(route or path, {
            "count": 0, "errors": 0, "histogram": [0] * len(BUCKETS), "max_ms": 0.,
        })
        metric["count"] += 1
        try:
            response = await self.clients[index].request(method, path, **kwargs)
            if response.status_code != expected:
                raise ValueError(f"HTTP {response.status_code}, expected {expected}")
            return response
        except Exception as exc:
            metric["errors"] += 1
            self.error(route or path, index, type(exc).__name__ + (
                f": HTTP {response.status_code}" if response is not None else ""))
            return None
        finally:
            ms = (time.monotonic() - started) * 1000
            bucket = next((i for i, upper in enumerate(BUCKETS) if ms <= upper), len(BUCKETS) - 1)
            metric["histogram"][bucket] += 1
            metric["max_ms"] = max(metric["max_ms"], round(ms, 3))

    async def login(self, index):
        p = self.accounts[index]
        response = await self.request(index, "POST", "/api/auth/login", route="login",
                                      json={"email": p["email"], "password": p["password"]})
        if response is None:
            return False
        account = response.json()["user"]
        if account["id"] != p["user_id"]:
            self.record["isolation_violations"] += 1
            raise ValueError("wrong authenticated account")
        # The service correctly sets Secure cookies. This loopback HTTP harness
        # explicitly forwards its own cookie; it does not certify browser/TLS.
        self.clients[index].headers["Cookie"] = "tf_session=" + response.cookies["tf_session"]
        self.live[index]["account"] = account
        return True

    async def user(self, index):
        await asyncio.sleep(index * .2)
        p, u, live = self.accounts[index], self.record["users"][index], self.live[index]
        login_at = 0
        while not self.stopping:
            started = time.monotonic()
            if not live["account"] or started - login_at >= 12 * 3600:
                if not await self.login(index):
                    await asyncio.sleep(10)
                    continue
                login_at = started
            note = f"acceptance:{p['tenant_id']}:{u['rounds']}"
            saved = await self.request(index, "POST", "/api/watchlist",
                                       json={"symbol": p["symbol"], "note": note}, route="watchlist.write")
            rows = await self.request(index, "GET", "/api/watchlist", route="watchlist.read")
            market = await self.request(index, "GET", "/api/kline/daily", route="market.daily",
                                        params={"symbol": p["symbol"], "days": 30})
            valid = saved is not None and rows is not None and market is not None
            if rows is not None:
                values = rows.json()["symbols"]
                mismatch = classify_watchlist_mismatch(
                    values,
                    tenant_id=p["tenant_id"],
                    symbol=p["symbol"],
                    round_index=u["rounds"],
                )
                if mismatch is not None:
                    self.record[f"{mismatch}_violations"] += 1
                    valid = False
                    self.error(mismatch, index, "private watchlist marker mismatch")
            if u["rounds"] % 3 == 0:
                auth = await self.request(index, "GET", "/api/auth/status", route="account.status")
                valid &= auth is not None
                if auth is not None and (auth.json().get("user") or {}).get("id") != p["user_id"]:
                    self.record["isolation_violations"] += 1
                    valid = False
            if u["rounds"] % 6 == 0:
                valid &= await self.request(index, "GET", "/api/strategies", route="strategies.list") is not None
            if valid:
                now = time.monotonic()
                if live["last_ok"] and self.record["status"] == "running":
                    u["max_activity_gap_s"] = max(u["max_activity_gap_s"], round(now - live["last_ok"], 3))
                live["last_ok"] = now
                u["rounds"] += 1
            await asyncio.sleep(max(.1, self.record["round_seconds"] - (time.monotonic() - started)))

    async def sse(self, index):
        u, live = self.record["users"][index], self.live[index]
        while not self.stopping:
            if not live["account"]:
                await asyncio.sleep(1)
                continue
            response = await self.request(index, "POST", "/api/events/ticket", route="sse.ticket")
            if response is None:
                await asyncio.sleep(5)
                continue
            event, data, event_id = "", [], ""
            retry_delay = .05
            try:
                # Server authorization expires after 60 seconds. Renew promptly
                # with a new ticket and the durable Last-Event-ID cursor.
                async with asyncio.timeout(600):
                    async with self.clients[index].stream(
                        "GET", response.json()["stream"],
                        headers={"Last-Event-ID": u["last_event_id"]},
                        timeout=httpx.Timeout(35, connect=5),
                    ) as stream:
                        if stream.status_code != 200:
                            raise ValueError("SSE status")
                        self.stream_state(index, True)
                        live["frame_at"] = time.monotonic()
                        async for line in stream.aiter_lines():
                            live["frame_at"] = time.monotonic()
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                data.append(line[5:].strip())
                            elif line.startswith("id:"):
                                event_id = line[3:].strip()
                            elif not line:
                                if event_id:
                                    u["last_event_id"] = event_id
                                if event == "acceptance_probe":
                                    payload = json.loads("\n".join(data))
                                    if payload["tenant_id"] != u["tenant_id"] or payload["user_id"] != u["user_id"]:
                                        self.record["isolation_violations"] += 1
                                    elif payload["nonce"] in self.pending:
                                        self.pending.pop(payload["nonce"])
                                        u["private_received"] += 1
                                elif event in {"quotes_updated", "depth_updated"}:
                                    u["public_events"] += 1
                                event, data, event_id = "", [], ""
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                pass
            except Exception as exc:
                self.error("sse", index, type(exc).__name__)
                retry_delay = 1
            finally:
                self.stream_state(index, False)
                u["stream_reconnects"] += 1
            await asyncio.sleep(retry_delay)

    def inject(self, index, payload):
        p = self.accounts[index]
        context = self.identity.context(self.live[index]["account"], p["tenant_id"], "acceptance-probe")
        with self.db.business_transaction(context, write=True) as conn:
            append_event(conn, p["tenant_id"], "acceptance_probe", payload)

    async def probes(self):
        sequence = 0
        while not self.stopping:
            if self.record.get("window_end") and (
                datetime.fromisoformat(self.record["window_end"]) - datetime.now(CN_TZ)
            ).total_seconds() < 60:
                await asyncio.sleep(5)
                continue
            for index, live in enumerate(self.live):
                if not live["account"] or not live["connected"]:
                    continue
                p = self.accounts[index]
                nonce = f"{self.run_dir.name}:{index}:{sequence}"
                self.pending[nonce] = time.monotonic()
                try:
                    await asyncio.to_thread(self.inject, index, {
                        "tenant_id": p["tenant_id"], "user_id": p["user_id"], "nonce": nonce})
                except Exception as exc:
                    self.pending.pop(nonce, None)
                    self.record["probe_injection_errors"] += 1
                    self.error("private_probe_inject", index, type(exc).__name__)
            sequence += 1
            await asyncio.sleep(60)

    async def research(self):
        while not self.stopping:
            if self.record["status"] != "running":
                await asyncio.sleep(2)
                continue
            if self.next_research and (
                datetime.fromisoformat(self.record["window_end"]) - datetime.now(CN_TZ)
            ).total_seconds() < 600:
                await asyncio.sleep(5)
                continue
            index = self.next_research % 100
            self.next_research += 1
            started = time.monotonic()
            self.record["research"]["in_flight"] = index
            try:
                response = await self.clients[index].post(
                    "/api/backtest/strategy/run", timeout=600,
                    json={"strategy_id": "ma_golden_cross",
                          "symbols": [p["symbol"] for p in self.accounts[:20]],
                          "start": "2026-09-01", "end": "2026-10-16",
                          # Exercise matching/accounting even when this small
                          # real-data pool has no default MA-cross signals.
                          "params": {"require_ma_golden": False, "use_volume_filter": False,
                                     "require_above_ma60": False, "require_ma60_non_declining": False},
                          # Seed data has no historical listing/share metadata.
                          # This declared price-only workload does not certify
                          # default eligibility filters or research completeness.
                          "overrides": {"basic_filter": {"market_cap_min": None,
                                                        "exclude_new_days": 0, "exclude_st": False}}},
                )
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                jobs = await self.clients[index].get("/api/backtest/jobs")
                jobs.raise_for_status()
                latest = jobs.json()["items"][0]
                if latest["status"] != "succeeded":
                    raise ValueError("queued compute did not succeed")
                if response.json().get("stats", {}).get("n_trades", 0) <= 0:
                    raise ValueError("compute did not exercise trade accounting")
                self.record["research"]["last_trade_count"] = response.json()["stats"]["n_trades"]
                foreign = await self.request(
                    (index + 1) % 100, "GET", f"/api/backtest/jobs/{latest['id']}",
                    route="research.foreign_denied", expected=404,
                )
                if foreign is None:
                    self.record["isolation_violations"] += 1
                    raise ValueError("foreign research read not denied")
                self.record["research"]["succeeded"] += 1
                self.record["research"]["user_indices"] = sorted(set(
                    [*self.record["research"]["user_indices"], index]))
            except Exception as exc:
                self.record["research"]["failed"] += 1
                self.error("research", index, type(exc).__name__ + (
                    ": " + str(exc) if isinstance(exc, ValueError) else ""))
                # Avoid growing an unknown queue after a timeout.
                self.record["research"]["halted_after_failure"] = True
                return
            finally:
                self.record["research"]["last_seconds"] = round(time.monotonic() - started, 3)
                self.record["research"]["in_flight"] = None
            await asyncio.sleep(max(1, 900 - (time.monotonic() - started)))

    def persist(self):
        self.record["heartbeat_at"] = now_text()
        self.record["active_users_60s"] = sum(
            time.monotonic() - v["last_ok"] < 60 for v in self.live)
        self.record["connected_streams"] = sum(v["connected"] for v in self.live)
        self.record["pending_private_probes"] = len(self.pending)
        write_json(self.path, self.record)

    async def run(self):
        if self.record["status"] in TERMINAL:
            return
        self.db.open()
        tasks = [asyncio.create_task(self.user(i)) for i in range(100)]
        tasks += [asyncio.create_task(self.sse(i)) for i in range(100)]
        tasks += [asyncio.create_task(self.probes()), asyncio.create_task(self.research())]
        boot, sampled = time.monotonic(), 0
        try:
            while True:
                now = datetime.now(CN_TZ)
                failed_tasks = [t for t in tasks if t.done() and not t.cancelled() and t.exception()]
                if failed_tasks:
                    self.record["status"] = "failed"
                    self.record["fatal"] = type(failed_tasks[0].exception()).__name__
                    break
                if self.record["status"] == "preparing":
                    if all(v["last_ok"] and v["connected"] for v in self.live):
                        self.record.update(
                            status="running", window_start=now.isoformat(),
                            window_end=(now + timedelta(hours=self.record["duration_hours"])).isoformat())
                        for u in self.record["users"]:
                            u["baseline_rounds"] = u["rounds"]
                        for live in self.live:
                            live["stream_changed_at"] = time.monotonic()
                    elif time.monotonic() - boot > 240:
                        self.record.update(status="failed", fatal="100-user warmup timed out")
                        break
                if self.record["status"] == "running":
                    start = datetime.fromisoformat(self.record["window_start"])
                    end = datetime.fromisoformat(self.record["window_end"])
                    self.record["observed_seconds"] = (now - start).total_seconds()
                    for u, live in zip(self.record["users"], self.live, strict=True):
                        self.stream_state(u["index"])
                        u["stream_samples"] += 1
                        u["stream_up_samples"] += bool(
                            live["connected"] and time.monotonic() - live["frame_at"] < 35)
                        u["max_activity_gap_s"] = max(
                            u["max_activity_gap_s"], round(time.monotonic() - live["last_ok"], 3))
                    expired = [nonce for nonce, at in self.pending.items() if time.monotonic() - at > 45]
                    self.record["private_probe_timeouts"] += len(expired)
                    for nonce in expired:
                        self.pending.pop(nonce)
                    if sampled:
                        self.record["max_sample_gap_s"] = max(
                            self.record["max_sample_gap_s"], time.monotonic() - sampled)
                    sampled = time.monotonic()
                    self.record["samples"] += 1
                    self.persist()
                    with (self.run_dir / "telemetry.jsonl").open("a") as stream:
                        stream.write(json.dumps({k: self.record[k] for k in (
                            "heartbeat_at", "active_users_60s", "connected_streams", "observed_seconds",
                            "isolation_violations", "consistency_violations",
                            "private_probe_timeouts", "requests", "research",
                        )}) + "\n")
                    if now >= end:
                        report = evaluate(self.record)
                        write_json(self.run_dir / "report.json", report)
                        self.record.update(status=report["status"], checks=report["checks"])
                        break
                self.persist()
                await asyncio.sleep(15)
        finally:
            self.stopping = True
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            for client in self.clients:
                await client.aclose()
            self.db.close()
            self.persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/acceptance"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base", default="http://edge:8080")
    parser.add_argument("--hours", type=float, default=72)
    args = parser.parse_args()
    if not 0 < args.hours <= 168:
        raise ValueError("duration must be in (0,168]")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    with (args.run_dir / "observer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(Load(args.root, args.run_dir, args.base, args.hours).run())


if __name__ == "__main__":
    main()
