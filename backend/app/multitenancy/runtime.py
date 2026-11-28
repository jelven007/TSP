"""Full Worker: 消费已发布输入, 在授权/租约/配置/世代/截止守卫下事务提交结果。"""
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
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.cell.database import Databases
from app.cell.scheduler import Scheduler
from app.market_time import as_cn_datetime, cn_now, cn_today
from app.multitenancy.alerts import append_in_transaction
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import append_event
from app.multitenancy.executor import ComputeExecutor
from app.multitenancy.identity import AccountIdentity
from app.multitenancy.market import PublicMarket, decode_frame
from app.multitenancy.monitor_compute import evaluate_snapshot, fresh_frame, needs_sandbox
from app.multitenancy.paper_storage import PaperSession, TenantPaper
from app.multitenancy.research import authorize
from app.services.observability import measure_runtime_stage
from app.strategy import paper, paper_auto

logger = logging.getLogger(__name__)
INPUT_NAMESPACES = ("strategy_sources", "strategy_overrides", "custom_factors", "custom_signals",
                    "monitor_runtime", "strategy_monitoring")


def fingerprint(conn, *, include_state=True):
    """按租户输入文档版本、规则及分组计算提交指纹, 不用行情序号替代配置身份。"""
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
    """有调度claim时在当前事务检查租约所有权; 失效抛409, 禁止迟到任务提交。"""
    if claim is not None and not conn.execute(
        "SELECT scheduler.guard(%s,%s,%s) AS valid", (claim["tenant_id"], claim["kind"], claim["owner"]),
    ).fetchone()["valid"]:
        raise AccessError(409, "后台任务租约已过期")


def signal_event_identity(payload, event, *, as_of, execution=False):
    """S02 publication identity excludes retry sequence, wall clock and event position."""
    from app.services.strategy_input_manifest import digest
    sid = event.get("strategy_id")
    if sid != "custom_mvxxcp88" or event.get("type") not in ("buy_signal", "sell_signal"):
        return None
    frozen = payload.get("strategy_input_manifests", {}).get(sid, {}).get("stocks", {})
    return digest([
        "strategy_signal_publication_v1", execution, payload["tenant_id"], sid, as_of,
        payload.get("daily_identities", {}).get(sid), payload.get("strategy_task_generations", {}).get(sid),
        frozen.get("input_sha256") if event["type"] == "buy_signal" else None,
        event.get("rule_id"), event["type"], event.get("symbol"), sorted(event.get("signals") or []),
    ])


def freeze_task_history(payload, sid, saved, generation, contract):
    """Only the current generation can resume unfinished work or publish pending events."""
    previous = payload["strategy_task_results"].get(sid) or {}
    identity = payload["daily_identities"][sid]["runtime_identity"]
    if saved.get("contract") != contract or saved.get("runtime_identity") != identity:
        payload["strategy_task_results"].pop(sid, None)
        return {}, set()
    if saved.get("generation") == generation:
        if previous.get("runtime_identity") != identity:
            payload["strategy_task_results"].pop(sid, None)
        return saved, set()

    # Keep outcomes already committed for this definition and day so that a
    # monitor restart cannot repeat a published buy or sell. Unfinished work,
    # repair ownership and pending events belong to the old generation.
    tasks = saved.get("tasks") or {}
    buy = tasks.get("buy") or {}
    sell = tasks.get("sell") or {}
    normal = ("success", "rejected")
    kept = {}
    def pending_publication(signal):
        return (signal.get("joined_at") and signal.get("passed") is True and signal.get("breadth_required")
                and signal.get("publication_state") != "complete"
                and (signal.get("stocks") or {}).get("hits"))

    if buy.get("published") is True:
        signals = buy.get("signals") or {}
        settled = {key: sig for key, sig in signals.items()
                   if sig.get("state") in normal and not pending_publication(sig)}
        if buy.get("state") in normal and buy.get("terminal") is True and settled == signals:
            kept["buy"] = buy
        elif settled:
            kept["buy"] = {"signals": settled}
    if sell.get("state") in normal and sell.get("terminal") is True:
        kept["sell"] = sell
    cached = previous.get("monitor_tasks") or {}
    if previous.get("runtime_identity") == identity and any(cached.get(k) == v for k, v in kept.items()):
        previous["monitor_tasks"] = kept
        for kind, keys in (("buy", ("rows", "total", "entry_signal_hits", "entry_data_quality",
                                    "entry_filter_error", "execution_status")),
                           ("sell", ("exit_signal_hits", "exit_filter_error"))):
            if kind not in kept or cached.get(kind) != kept[kind]:
                for key in keys:
                    previous.pop(key, None)
                for key in ("sections", "section_statuses", "section_errors"):
                    (previous.get(key) or {}).pop(kind, None)
    else:
        payload["strategy_task_results"].pop(sid, None)
    closed = {kind for kind, task in tasks.items() if task.get("state") in ("cancelled", "expired")}
    return {"tasks": kept}, closed


class FullWorker:
    def __init__(self, db, repo, market, executor=None):
        from app.multitenancy.monitor_phase_inbox import PhaseInbox
        from app.services.strategy_minute_live import ShadowMinuteProbe

        self.db, self.repo, self.market = db, repo, market
        self.identity = AccountIdentity(db)
        self.executor = executor or ComputeExecutor()
        self.scheduler = Scheduler(db)
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=db.settings.worker_concurrency)
        self.phase_inbox = PhaseInbox()
        self.shadow_minute_probe = ShadowMinuteProbe()

    def close(self):
        self.stop.set()
        self.shadow_minute_probe.close()
        self.phase_inbox.close()
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

    def prepare(self, tenant_id, snapshot, claim=None):
        """在租户只读事务冻结定义、覆盖、信号、世代、按日输入及公共发布身份。

        到期动作由保存窗口规划, 完整池和预选分开传入; 不因新行情改变已
        完成阶段。返回指纹供提交事务复核, 计算不持有这里的业务读事务。
        冻结前先处理旧监控迁移及待确认补数取消; 返回payload不授权计算侧
        自行取上游或写租户配置。
        """
        account, context = self.account(tenant_id)
        documents = TenantDocuments(self.db, context)
        from app.services.strategy_monitor_tasks import retire_legacy

        retire_legacy(documents)
        for saved in documents.records("strategy_monitoring"):
            state = saved["value"]
            if not state.get("repair_cancel_pending"):
                continue
            try:
                self.market.cancel_monitor_repair(tenant_id, saved["id"], state["generation"])
                documents.put("strategy_monitoring", saved["id"], {**state, "repair_cancel_pending": False})
            except Exception:
                logger.warning("cancelled monitor input repair awaiting platform acknowledgement")
        priority_plan = None
        with authorize(self.identity, context, account["auth_version"]), documents.transaction(write=False) as unit:
            conn = unit.connection
            definitions = conn.execute(
                "SELECT definition FROM business.monitor_rules WHERE enabled ORDER BY id",
            ).fetchall()
            rules = []
            for row in definitions:
                sid = row["definition"].get("strategy_id")
                if (sid and row["definition"].get("type") == "strategy"
                        and not row["definition"].get("lot_id")
                        and unit.get("strategy_monitoring", sid, {}).get("enabled") is not True):
                    continue
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
            from app.multitenancy.watchlist import Watchlist
            from app.strategy.config import list_overrides

            payload = {
                "kind": "monitor", "tenant_id": tenant_id, "snapshot": snapshot, "rules": rules, "groups": groups,
                "administrator": "settings:manage" in account["permissions"],
                "watchlist": Watchlist._rows(conn),
                "state": unit.get("monitor_runtime", "default", {}),
                "sources": [{**row["value"], "id": row["id"]}
                            for row in unit.records("strategy_sources")],
                "overrides": list_overrides(unit),
                "signals": unit.values("custom_signals"), "factors": unit.values("custom_factors"),
                **({"public_release_id": snapshot["public_release"]["id"]} if snapshot.get("public_release") else {}),
            }
            from app.strategy.morning_liquidation import holding_snapshot
            from app.strategy.overnight_runtime import position_snapshot

            capacity_ids = {
                source["id"] for source in payload["sources"]
                if source.get("definition", {}).get("meta", {}).get("minute_execution") == "overnight_capacity_v1"
                and source.get("definition", {}).get("meta", {}).get("trigger_selection") == "optional_v1"
            }

            morning_columns = {
                "csgi_" + sig["id"] for sig in payload["signals"]
                if sig.get("template") == "morning_liquidation" and sig.get("enabled", True)
            }
            source_exits = {
                source["id"]: source.get("definition", {}).get("exit_signals", [])
                for source in payload["sources"]
            }
            payload["positions"] = {
                sid: (position_snapshot if sid in capacity_ids else holding_snapshot)(
                    PaperSession(unit, conn, False), sid, cn_today())
                for sid in {r.get("strategy_id") for r in rules}
                if sid and (sid in capacity_ids or morning_columns.intersection(
                    payload["overrides"].get(sid, {}).get("exit_signals", source_exits.get(sid, [])) or [],
                ))
            }
            from app.services.strategy_results import capture_identity, previous_daily_state
            payload["previous_states"] = {}
            payload["daily_identities"] = {}
            payload["entry_deadlines"] = {}
            payload["next_evaluation_at"] = None
            payload["late_task_at"] = None
            payload["late_task_strategy_ids"] = []
            payload["late_task_definitions"] = {}
            payload["late_task_scopes"] = {}
            payload["strategy_task_windows"] = {}
            payload["strategy_task_ids"] = []
            payload["strategy_task_receipts"] = {}
            payload["strategy_task_plans"] = {}
            payload["strategy_task_generations"] = {}
            payload["strategy_task_results"] = {
                sid: result for sid, result in
                ((unit.get("monitor_results", "default") or {}).get("results") or {}).items()
                if result.get("as_of") == str(cn_today())
            }
            payload["sell_rosters"] = {}
            from app.services.daily_selections import DailySelections
            payload["selection_snapshots"] = {
                sid: selected for sid in {r.get("strategy_id") for r in rules} if sid
                and (selected := DailySelections(unit).read(sid, cn_today(), live_entry=True)) is not None
            }
            payload["daily_pool_snapshots"] = {}
            if self.db.settings.schema_version >= 7:
                from pathlib import Path

                from app.multitenancy.strategies import TenantStrategyEngine
                from app.strategy.engine import StrategyEngine
                catalog = TenantStrategyEngine(unit, StrategyEngine(
                    [Path(__file__).resolve().parents[1] / "strategy/builtin"]), self.executor)
                payload["daily_pool_snapshots"] = catalog._saved_pools(unit, {
                    "strategy_ids": [sid for sid in {r.get("strategy_id") for r in rules}
                                     if sid and catalog.has(sid)],
                    "as_of": str(cn_today()),
                })
                # The deadline helper reads the same tenant signal definitions as computation.
                catalog._user_data_dir = lambda: unit
                from app.strategy.engine import StrategyDataContext
                from app.strategy.late_session_strength import (
                    next_task_time,
                )
                from app.strategy.signal_sections import (
                    live_entry_deadline as entry_deadline,
                )
                from app.strategy.signal_sections import (
                    pending_entry_time as pending_evaluation_time,
                )
                from app.strategy.version import runtime_identity

                deadline_context = StrategyDataContext(
                    "stock", "1m", cn_today(), observed_at=datetime.fromtimestamp(time.time(), UTC))
                late_columns = {
                    "csgi_" + sig["id"] for sig in payload["signals"]
                    if sig.get("template") == "late_session_strength" and sig.get("enabled", True)
                }
                priority_ids = [
                    sid for sid, strategy in catalog._strategies.items()
                    if sid in {r.get("strategy_id") for r in rules}
                    and strategy.execution_backend == "minute_filter"
                    and late_columns.intersection(catalog._effective_signals(
                        payload["overrides"].get(sid, {}), "entry_signals", strategy.entry_signals))
                ]
                if callable(getattr(self.market, "minute_priority", None)):
                    from app.services.minute_priority import build_plan
                    from app.services.screener import ScreenerService
                    try:
                        priority_plan = build_plan(catalog, ScreenerService(self.repo), unit,
                                                   priority_ids, cn_today(), overrides=payload["overrides"],
                                                   current=decode_frame(snapshot["frames"]["stock"]))
                    except ValueError:
                        # A missing scope input must never shrink the priority cohort.
                        priority_plan = {"day": str(cn_today()), "sequence": time.time_ns(),
                                         "symbols": self.repo.get_instruments_asset("stock")["symbol"].to_list(),
                                         "selected": []}
                for sid in {r.get("strategy_id") for r in rules} | set(priority_ids):
                    if sid and catalog.has(sid):
                        payload["daily_identities"][sid] = asdict(capture_identity(
                            catalog, unit, sid, overrides=payload["overrides"].get(sid),
                            timeframe=(catalog.get(sid).meta.get("timeframes") or ["1d"])[0]))
                        strategy = catalog.get(sid)
                        from app.market_time import published_trading_day
                        from app.services.routed_strategy_monitor import task_windows
                        from app.services.strategy_monitor_state import (
                            CONTRACT,
                            plan,
                            resume_confirmation_timeout,
                        )
                        from app.services.strategy_monitor_tasks import sell_roster, task_receipt
                        if (sid in {r.get("strategy_id") for r in rules}
                                and strategy.source in ("custom", "ai")):
                            from app.strategy.daily_pool import DEFINITION_VERSION
                            pool = payload["daily_pool_snapshots"].get(sid) or {}
                            if pool.get("definition_version") != DEFINITION_VERSION:
                                from app.services.strategy_monitor_tasks import trusted_scope
                                try:
                                    pool = trusted_scope(catalog, self.repo, unit, sid, cn_today(),
                                                         payload["overrides"].get(sid))
                                    from app.services.daily_stock_pools import DailyStockPools
                                    from app.services.strategy_results import ResultIdentity
                                    identity = ResultIdentity(**payload["daily_identities"][sid])
                                    payload["daily_pool_snapshots"][sid] = DailyStockPools(unit).save(
                                        identity, pool, started_at=time.time_ns())
                                except Exception as exc:
                                    payload["daily_pool_snapshots"][sid] = {"error": str(exc)}
                            payload["sell_rosters"][sid] = roster = sell_roster(unit, sid, cn_today())
                            try:
                                windows = task_windows(catalog, strategy, payload["overrides"].get(sid) or {},
                                    day=cn_today(), selection=payload["selection_snapshots"].get(sid),
                                    roster=roster, require_market_route=True)
                            except ValueError as exc:
                                windows = {"buy": [], "sell": [], "selection_error": str(exc)}
                            payload["strategy_task_windows"][sid] = windows
                            generation = unit.get("strategy_monitoring", sid)["generation"]
                            receipt = task_receipt(unit, sid, cn_today())
                            scope = f"{sid}:{runtime_identity(strategy, payload['overrides'].get(sid) or {})}"
                            if receipt and receipt.get("generation") != generation:
                                engine_state = payload["state"].get("engine") or {}
                                phase_states = engine_state.get("late_session_tasks") or {}
                                for key in [key for key in phase_states if key.startswith(scope + ":")]:
                                    del phase_states[key]
                            saved, closed = freeze_task_history(
                                payload, sid, receipt, generation, CONTRACT)
                            saved["tasks"] = resume_confirmation_timeout(
                                windows, saved.get("tasks", {}), deadline_context.observed_at)
                            payload["strategy_task_receipts"][sid] = saved
                            payload["strategy_task_generations"][sid] = generation
                            planning = plan(windows, saved.get("tasks", {}), deadline_context.observed_at,
                                            trading=published_trading_day(cn_today()))
                            # Reopening within a window must schedule real work; reopening
                            # after it closes leaves the old cancellation/expiry as evidence.
                            planning["actions"] = [action for action in planning["actions"]
                                                   if action["phase"] != "expire" or action["kind"] not in closed]
                            payload["strategy_task_plans"][sid] = planning
                            # 自定义监控只由按日阶段计划驱动; 不同时交给报价驱动路径重复计算。
                            payload["late_task_scopes"][sid] = scope
                            continue
                        if strategy.execution_backend == "minute_filter":
                            end = entry_deadline(catalog, catalog.get(sid),
                                                 payload["overrides"].get(sid), deadline_context,
                                                 expired_only=False)
                            if end is not None:
                                payload["entry_deadlines"][sid] = end.isoformat()
                            pending = pending_evaluation_time(
                                catalog, strategy, payload["overrides"].get(sid), deadline_context,
                            )
                            pending = as_cn_datetime(pending) if pending is not None else None
                            if pending is not None and (
                                payload["next_evaluation_at"] is None
                                or pending.isoformat() < payload["next_evaluation_at"]
                            ):
                                payload["next_evaluation_at"] = pending.isoformat()
                            scope = f"{sid}:{runtime_identity(strategy, payload['overrides'].get(sid) or {})}"
                            payload["late_task_scopes"][sid] = scope
                            selected = set(catalog._effective_signals(
                                payload["overrides"].get(sid, {}), "entry_signals", strategy.entry_signals))
                            for sig in payload["signals"]:
                                if sig.get("template") != "late_session_strength" or "csgi_" + sig["id"] not in selected:
                                    continue
                                payload["late_task_definitions"].setdefault(sid, []).append(sig)
                                saved = payload["state"].get("engine", {}).get("late_session_tasks", {}).get(
                                    f"{scope}:csgi_{sig['id']}", {})
                                next_at = next_task_time(sig, cn_today(), deadline_context.observed_at, saved)
                                if next_at is not None:
                                    next_at = as_cn_datetime(next_at).isoformat()
                                    if payload["late_task_at"] is None or next_at < payload["late_task_at"]:
                                        payload["late_task_at"] = next_at
                                    if sid not in payload["late_task_strategy_ids"]:
                                        payload["late_task_strategy_ids"].append(sid)
            for sid in {r.get("strategy_id") for r in rules} & {"s02"}:
                try:
                    payload["previous_states"][sid] = previous_daily_state(unit, sid, cn_today())
                    if not hasattr(self, "_daily_strategy_engine"):
                        from pathlib import Path

                        from app.strategy.engine import StrategyEngine
                        self._daily_strategy_engine = StrategyEngine(
                            [Path(__file__).resolve().parents[1] / "strategy/builtin"])
                    payload["daily_identities"][sid] = asdict(capture_identity(
                        self._daily_strategy_engine, unit, sid,
                        overrides=payload["overrides"].get(sid), timeframe="1m"))
                except ValueError:
                    # Missing history disables this strategy in the compute kernel.
                    pass
            observed = fingerprint(conn)
            payload["_configuration_fingerprint"] = fingerprint(conn, include_state=False)
        # Platform calls may wait on HTTP. Release both authorization and
        # tenant binding locks first; commit still verifies all frozen guards.
        from app.services.strategy_minute_live import (
            fetch_scoped_packets,
            live_mode,
            prioritize_collection,
        )
        from app.services.strategy_monitor_tasks import wait_for_repairs

        minute_observed_at = cn_now()
        if live_mode() == "shadow":
            self.shadow_minute_probe.offer(payload, self.market, minute_observed_at)
        if priority_plan is not None:
            try:
                if live_mode() == "active":
                    priority_plan = prioritize_collection(priority_plan, payload, minute_observed_at)
                self.market.minute_priority(tenant_id, priority_plan)
            except AccessError:
                logger.warning("minute priority bridge unavailable; ordinary collection retained")
        for sid, planning in payload["strategy_task_plans"].items():
            packets = fetch_scoped_packets(payload, self.market, sid, minute_observed_at)
            if packets:
                payload.setdefault("strategy_minute_packets", {})[sid] = packets
                payload["strategy_minute_observed_at"] = minute_observed_at.isoformat()
            # Only a complete scoped packet can bypass a repair of the same action.
            ready_actions = [action for action in planning["actions"] if action["kind"] in packets
                             and (action["kind"], action["phase"]) in (("buy", "stocks"), ("sell", "observe"))]
            pending_repairs = {**planning, "actions": [a for a in planning["actions"] if a not in ready_actions]}
            wait_for_repairs(pending_repairs, payload["strategy_task_receipts"][sid], self.market,
                             current_release=payload.get("public_release_id"))
            planning["actions"] = [a for a in planning["actions"]
                                   if a in ready_actions or a in pending_repairs["actions"]]
            next_at = planning["next_at"]
            if next_at and (payload["next_evaluation_at"] is None or next_at < payload["next_evaluation_at"]):
                payload["next_evaluation_at"] = next_at
            if planning["actions"]:
                payload["strategy_task_ids"].append(sid)
        return account, context, payload, observed

    def evaluate(self, tenant_id, snapshot, claim=None):
        """完成准备/计算/提交闭环; 异常状态也须通过当前租约和指纹守卫。"""
        started, started_at = time.perf_counter(), time.time()
        with measure_runtime_stage("monitor.prepare"):
            prepared = self.prepare(tenant_id, snapshot, claim)
        preparation_finished = time.perf_counter()
        try:
            return self._evaluate(tenant_id, snapshot, claim, prepared,
                                  timings=(started, started_at, preparation_finished))
        except Exception as exc:
            from app.strategy.execution_status import status

            account, context, payload, observed = prepared
            state = "deferred" if isinstance(exc, AccessError) else (
                "failed" if isinstance(exc, ValueError) else "retrying")
            try:
                with (
                    authorize(self.identity, context, account["auth_version"]),
                    TenantDocuments(self.db, context).transaction() as unit,
                ):
                    valid_claim(unit.connection, claim)
                    if fingerprint(unit.connection) == observed:
                        if not isinstance(exc, AccessError):
                            from app.multitenancy.monitor_compute import (
                                failed_task_results,
                                failure_events,
                            )
                            now = datetime.fromtimestamp(time.time(), UTC)
                            failures, failure_state = failure_events(
                                failed_task_results(payload, str(exc), now), payload["rules"],
                                payload["state"].get("signal_failures", {}), now)
                            if failures or payload.get("late_task_definitions"):
                                append_in_transaction(unit.connection, tenant_id, failures)
                                unit.put("monitor_runtime", "default", {
                                    **payload["state"], "signal_failures": failure_state})
                        unit.put("strategy_execution_status", "default", {
                            **status(state, str(exc), sequence=snapshot.get("sequence"),
                                     fingerprint=fingerprint(unit.connection, include_state=False)),
                            "as_of": str(cn_today()),
                            "strategy_ids": sorted({r["strategy_id"] for r in payload["rules"]
                                                    if r.get("strategy_id")}),
                        })
                        valid_claim(unit.connection, claim)
            except Exception:
                logger.warning("worker failure status could not be saved with current guards")
            raise

    def _evaluate(self, tenant_id, snapshot, claim, prepared_context, *, timings):
        """计算冻结输入后重查租约、配置指纹、监控世代和买卖截止再提交。

        自定义阶段不依赖新报价心跳; 普通监控失败保留已完成自定义结果。
        归档、阶段回执与通知在同一业务事务保存, 计算成功不等于已经成交。
        隔离阶段只接收本轮已完成回执, 未完成请求不持有租户周期或事务。
        各阶段在当前持久化状态上合并, 事务内再次检查截止; 提交后才确认回执。
        """
        account, context, payload, observed = prepared_context
        started, started_at, prepared = timings
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
        release_id = (snapshot.get("public_release") or {}).get("id")
        pending = payload["state"].get("next_evaluation_at")
        due = (pending is not None
               and payload["state"].get("evaluated_at", now)
               < as_cn_datetime(datetime.fromisoformat(pending)).timestamp() <= now)
        changed = (sequence > previous_sequence or (sequence == previous_sequence and (
            release_id != payload["state"].get("public_release_id") or due
        )))
        task_due = (payload.get("late_task_at") is not None
                    and as_cn_datetime(datetime.fromisoformat(payload["late_task_at"])).timestamp() <= now)
        task_due = task_due or bool(payload.get("strategy_task_ids"))
        result = None
        # 自定义按日任务独立执行, 不进入报价驱动普通监控; 避免心跳触发重复入场。
        custom_ids = set(payload.get("strategy_task_windows", {}))
        regular = [r for r in payload["rules"] if r.get("strategy_id") not in custom_ids]
        phase_pending = self.phase_inbox.pending(tenant_id)
        if (payload.get("strategy_task_ids") or phase_pending
                or any(self.phase_inbox.unpublished(payload).values())):
            payload["evaluation_started_at"] = now
            from app.services.strategy_input_binding import prepare_inputs
            prepare_inputs(payload, self.market)
            from app.multitenancy.monitor_compute import evaluate_strategy_phases
            if getattr(self.executor, "isolated", False):
                received = self.phase_inbox.advance(payload, self.repo, self.executor)
                result = received if received["results"] else None
                phase_pending = self.phase_inbox.pending(tenant_id)
                if phase_pending:
                    wake = datetime.fromtimestamp(now + 1, UTC).isoformat()
                    payload["next_evaluation_at"] = min(
                        payload.get("next_evaluation_at") or wake, wake)
            else:
                result = evaluate_strategy_phases(payload, self.repo, self.executor)
            from app.services.strategy_monitor_tasks import request_phase_repairs
            if result:
                request_phase_repairs(payload, result["results"], self.market, cn_now())
        payload["rules"] = regular + [r for r in payload["rules"] if r.get("strategy_id") in
                                    (set(payload.get("strategy_task_ids", []))
                                     | set(result["results"] if result else {}))]
        original_rules = payload["rules"]
        payload["rules"] = regular
        if ((fresh and changed) or task_due) and payload["rules"] and not phase_pending:
            payload["evaluation_started_at"] = now
            payload["late_task_only"] = task_due and not fresh
            with measure_runtime_stage("monitor.compute"):
                if needs_sandbox(payload["rules"]):
                    cancelled = threading.Event()
                    timer = threading.Timer(self.db.settings.worker_timeout, cancelled.set)
                    timer.start()
                    try:
                        try:
                            ordinary_payload = {key: value for key, value in payload.items()
                                                if key != "strategy_minute_packets"}
                            ordinary_result = self.executor.execute(ordinary_payload, cancel=cancelled)
                        except Exception as exc:
                            if result is None:
                                raise
                            logger.exception("ordinary monitor failed; completed custom phases retained")
                            ordinary_result = {
                                "results": {}, "execution_events": [],
                                "state": {**payload["state"], "ordinary_compute_error": str(exc)},
                                "events": [{"type": "monitor_calculation_failed", "source": "system",
                                    "severity": "warning", "ts": int(now * 1000),
                                    "message": f"普通监控计算失败: {exc}; 已完成策略阶段保留"}],
                            }
                    finally:
                        timer.cancel()
                else:
                    # Built-in expressions use the explicit group snapshot.
                    ordinary_result = evaluate_snapshot({**payload, "signals": []}, self.repo)
            if result is None:
                result = ordinary_result
            else:
                result["events"].extend(ordinary_result["events"])
                result["execution_events"].extend(ordinary_result.get("execution_events", []))
                result["results"].update(ordinary_result["results"])
                custom_tasks = result["state"].get("engine", {}).get("late_session_tasks", {})
                result["state"] = ordinary_result["state"]
                result["state"].setdefault("engine", {})["late_session_tasks"] = custom_tasks
        payload["rules"] = original_rules
        computed, computed_at = time.perf_counter(), time.time()
        release = snapshot.get("public_release") or {}
        latency = {
            "sequence": sequence, "release_id": release.get("id"),
            "minute_completed_at": release.get("minute_completed_at"),
            "release_published_at": release.get("published_at"),
            "worker_started_at": started_at, "compute_finished_at": computed_at,
            "prepare_ms": round((prepared - started) * 1000, 3),
            "compute_ms": round((computed - prepared) * 1000, 3),
            "compute": result.get("_compute_timing", {}) if result else {},
        }
        documents = TenantDocuments(self.db, context)
        with ExitStack() as locks:
            locks.enter_context(measure_runtime_stage("monitor.commit"))
            locks.enter_context(authorize(self.identity, context, account["auth_version"]))
            for user_id, version in {
                (rule["_execution"]["user_id"], rule["_execution"]["auth_version"]) for rule in payload["rules"]
            }:
                if user_id != context.user_id:
                    locks.enter_context(authorize(self.identity, replace(context, user_id=user_id), version))
            with documents.transaction() as unit:
                conn = unit.connection
                # 计算返回不授予提交权; 先检查租约和配置, 旧在途任务不能覆盖当前结果。
                valid_claim(conn, claim)
                if fingerprint(conn) != observed:
                    raise AccessError(409, "监控配置已更新")
                events, execution_events = [], []
                if result is not None:
                    from app.strategy.execution_status import expire_late_entries

                    expire_late_entries(
                        result, payload.get("entry_deadlines", {}),
                        datetime.fromtimestamp(time.time(), UTC),
                    )
                    from app.services.strategy_monitor_tasks import fence_tasks
                    # 真实提交时钟再次校验世代与截止; 删除迟到新买及执行事件, 不抹正常证据。
                    for sid, daily in result["results"].items():
                        if sid in payload.get("strategy_task_generations", {}):
                            from app.services.strategy_monitor_tasks import assert_generation
                            assert_generation(unit, sid, payload["strategy_task_generations"][sid])
                        rejected = fence_tasks(
                            daily, payload.get("strategy_task_windows", {}).get(sid, {}),
                            datetime.fromtimestamp(time.time(), UTC))
                        if rejected:
                            kinds = {"buy_signal" if kind == "buy" else "sell_signal" for kind in rejected}
                            result["events"] = [e for e in result["events"]
                                if not (e.get("strategy_id") == sid and e.get("type") in kinds)]
                            result["execution_events"] = [e for e in result.get("execution_events", [])
                                if not (e.get("strategy_id") == sid and e.get("type") in kinds)]
                    from app.multitenancy.monitor_compute import failure_events
                    failures, failure_state = failure_events(
                        result["results"], payload["rules"], payload["state"].get("signal_failures", {}),
                        datetime.fromtimestamp(now, UTC))
                    result["state"]["signal_failures"] = failure_state
                    failure_alerts = append_in_transaction(conn, tenant_id, failures)
                    for index, event in enumerate(result["events"]):
                        event["id"] = signal_event_identity(
                            payload, event, as_of=str(cn_today())) or hashlib.sha256(
                            f"{tenant_id}:{sequence}:{observed}:{index}".encode(),
                        ).hexdigest()
                    events = failure_alerts + append_in_transaction(conn, tenant_id, result["events"])
                    for index, event in enumerate(result.get("execution_events", [])):
                        execution_events.append({
                            **event, "id": signal_event_identity(
                                payload, event, as_of=str(cn_today()), execution=True) or hashlib.sha256(
                                f"execution:{tenant_id}:{sequence}:{observed}:{index}".encode(),
                            ).hexdigest(),
                        })
                    unit.put("monitor_runtime", "default", {
                        **result["state"], "sequence": sequence, "public_release_id": release_id,
                        "evaluated_at": now, "next_evaluation_at": payload.get("next_evaluation_at"),
                    })
                    if result["results"]:
                        from app.services.strategy_results import ResultIdentity, StrategyResults
                        from app.strategy.execution_status import merge_result, result_status
                        previous_results = (unit.get("monitor_results", "default") or {}).get("results", {})
                        for sid, daily in result["results"].items():
                            if daily.get("run_evidence"):
                                for phase in daily["run_evidence"]["phases"].values():
                                    phase["timing"].setdefault("commit_entered_at", cn_now().isoformat())
                            unit.put("strategy_execution_status", sid, {
                                **result_status(daily), "as_of": daily.get("as_of"),
                                "runtime_identity": daily.get("runtime_identity"),
                                "fingerprint": fingerprint(conn, include_state=False),
                            })
                            if (sid in payload.get("daily_identities", {})
                                    and ((result_status(daily)["state"] == "success"
                                          and not daily.get("showing_last_success"))
                                         or daily.get("monitor_contract") == "strategy_monitor_v2"
                                         or (daily.get("monitor_tasks") or {}).get("sell", {}).get("state")
                                         in ("success", "failed"))):
                                record = ResultIdentity(**payload["daily_identities"][sid])
                                if daily.get("sections") is not None:
                                    # 按日阶段可在无新报价时运行; 归档使用本次尝试序号,
                                    # 不能把冻结报价时钟当作本次计算提交顺序。
                                    archive_started = (int(started_at * 1_000_000_000)
                                        if daily.get("monitor_contract") == "strategy_monitor_v2"
                                        else sequence * 1000000)
                                    saved = StrategyResults(unit).save(record, daily, started_at=archive_started)
                                    daily.update(saved["result"])
                                elif daily.get("daily_stock_pool") is not None:
                                    from app.services.daily_stock_pools import DailyStockPools
                                    DailyStockPools(unit).save(record, daily["daily_stock_pool"],
                                        started_at=sequence * 1000000)
                            if daily.get("monitor_tasks"):
                                # 任务回执与上面的归档、通知同属业务事务, 失败不会发布半份结果。
                                from app.services.strategy_monitor_state import CONTRACT
                                unit.put("strategy_monitor_task_runs", f"{sid}:{daily['as_of']}", {
                                    "as_of": daily["as_of"], "strategy_id": sid,
                                    "tasks": daily["monitor_tasks"], "updated_at": int(now * 1000),
                                    "contract": CONTRACT, "runtime_identity": daily.get("runtime_identity"),
                                    "generation": payload.get("strategy_task_generations", {}).get(sid),
                                })
                            result["results"][sid] = merge_result(previous_results.get(sid), daily)
                            # Timings describe the current attempt and stay out of the archive.
                            result["results"][sid]["latency"] = latency
                        unit.put("monitor_results", "default", {
                            "sequence": sequence, "results": {**previous_results, **result["results"]},
                            "fingerprint": fingerprint(conn, include_state=False),
                        })
                        append_event(conn, tenant_id, "strategy_results_updated", {
                            "ts": int(now * 1000), "strategy_ids": sorted(result["results"])})
                from app.market_time import CN_TZ
                from app.services.strategy_results import ResultIdentity, StrategyResults
                from app.strategy.execution_status import status, successful_zero_receipt

                commit_observed = datetime.fromtimestamp(time.time(), CN_TZ).replace(tzinfo=None)
                for sid, deadline in payload.get("entry_deadlines", {}).items():
                    if commit_observed <= datetime.fromisoformat(deadline):
                        continue
                    identity = ResultIdentity(**payload["daily_identities"][sid])
                    saved = StrategyResults(unit).read(identity, str(cn_today()))
                    monitored = unit.get("monitor_results", "default") or {}
                    live = (monitored.get("results", {}).get(sid)
                            if monitored.get("fingerprint") == fingerprint(conn, include_state=False) else None)
                    candidates = [live, (saved or {}).get("result")]
                    confirmed = successful_zero_receipt(
                        unit.get("critical_strategy_runs", f"{sid}:{cn_today()}", {}),
                        sid, str(cn_today()), identity.runtime_identity,
                    ) or any(
                        daily and daily.get("as_of") == str(cn_today())
                        and daily.get("runtime_identity") in (None, identity.runtime_identity)
                        and (daily.get("section_statuses") or {}).get("buy") == "success"
                        and not daily.get("selection_only")
                        for daily in candidates
                    )
                    previous_attempt = unit.get("strategy_execution_status", sid) or {}
                    attempt = {
                        **status("success" if confirmed else "expired",
                                 None if confirmed else "当日未完成买入确认; 实时新入场已于14:55:00截止",
                                 sequence=sequence, fingerprint=fingerprint(conn, include_state=False)),
                        "as_of": str(cn_today()), "runtime_identity": identity.runtime_identity,
                        "entry_deadline_at": deadline,
                    }
                    if any(previous_attempt.get(key) != attempt[key]
                           for key in ("state", "fingerprint", "as_of", "runtime_identity", "entry_deadline_at")):
                        unit.put("strategy_execution_status", sid, attempt)
                        append_event(conn, tenant_id, "strategy_results_updated", {"ts": int(now * 1000)})

                unit.put("strategy_execution_status", "default", {
                    **status("success" if result is not None else "waiting" if payload["rules"] else "disabled",
                             None if result is not None else
                             "等待当日新鲜行情及更新序号" if payload["rules"] else "没有启用的有效监控规则",
                             sequence=sequence, fingerprint=fingerprint(conn, include_state=False)),
                    "as_of": str(cn_today()),
                    "strategy_ids": sorted({r["strategy_id"] for r in payload["rules"] if r.get("strategy_id")}),
                })
                from app.services.s02_run_evidence import SID, save_operational
                save_operational(unit, {**payload, "strategy_task_results": {
                    **payload.get("strategy_task_results", {}), **(result["results"] if result else {})}},
                    cn_now(), self.phase_inbox.evidence(tenant_id, SID))
                paper_documents = TenantPaper(self.db, context, self.repo.store.data_dir)
                session = PaperSession(paper_documents, conn, True)
                # Private computation can consume the quote's validity window.
                # Recheck before placing or retrying market orders.
                commit_now = time.time()
                from app.strategy.execution_status import entry_events_before_deadline
                execution_events = entry_events_before_deadline(
                    execution_events, {
                        **(result["results"] if result else {}),
                        **{sid: {"entry_deadline_at": end}
                           for sid, end in payload.get("entry_deadlines", {}).items()},
                    },
                    datetime.fromtimestamp(commit_now, UTC),
                )
                fresh = fresh and (
                    0 <= commit_now - float(snapshot.get("captured_at", 0)) < 15
                    and -2 <= commit_now - sequence / 1000 <= 15
                )
                prices, opening_rows = {}, {}
                if fresh:
                    for asset in ("stock",):
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
                        minute_repo=self.repo, completed_through=cn_now(),
                    )
                self.settle(session, snapshot)
                for account_id in session.account_ids():
                    paper_auto.retry_exit_intents(session, account_id)
                valid_claim(conn, claim)
                if result:
                    for sid, daily in result["results"].items():
                        if daily.get("run_evidence"):
                            prior_commit = unit.get("strategy_run_commits", f"{sid}:{daily['as_of']}") or {}
                            phases = prior_commit.get("phases", {})
                            for phase, value in daily["run_evidence"]["phases"].items():
                                if phases.get(phase, {}).get("input_sha256") != value["input_sha256"]:
                                    phases[phase] = {"input_sha256": value["input_sha256"],
                                                     "commit_recorded_at": cn_now().isoformat()}
                            unit.put("strategy_run_commits", f"{sid}:{daily['as_of']}", {
                                "schema": "s02_run_commit_v1", "phases": phases})
                            from app.services.strategy_day_views import receipt_updated
                            receipt_updated(unit, sid, daily["as_of"])
        if result is not None:
            # A failed or rolled-back transaction never acknowledges its futures.
            self.phase_inbox.ack(result.get("_phase_ack", []))
        if result is not None and result["results"]:
            logger.info(
                "monitor result committed sequence=%s release=%s committed_at=%.6f "
                "prepare_ms=%.3f compute_ms=%.3f commit_ms=%.3f total_ms=%.3f",
                sequence, release.get("id"), time.time(), latency["prepare_ms"],
                latency["compute_ms"], (time.perf_counter() - computed) * 1000,
                (time.perf_counter() - started) * 1000,
            )
        return len(events)

    @staticmethod
    def settle(session, snapshot):
        moment = cn_now()
        if moment.weekday() < 5 and (14, 25, 0) <= (
            moment.hour, moment.minute, moment.second
        ) <= (14, 55, 0):
            # Historical bookkeeping runs after the critical live signal window.
            return
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
                    receipt = conn.execute(
                        "SELECT value FROM business.documents WHERE namespace='monitor_runtime' AND id='default'"
                    ).fetchone()
                if not active:
                    due = None
                elif not snapshot["status"].get("is_trading_hours"):
                    due = datetime.now(UTC) + timedelta(seconds=30)
                if active and receipt and (pending := receipt["value"].get("next_evaluation_at")):
                    boundary = datetime.fromisoformat(pending)
                    if boundary > datetime.now(UTC):
                        due = min(due, boundary)
                if active and self.phase_inbox.pending(claim["tenant_id"]):
                    due = min(due, datetime.now(UTC) + timedelta(seconds=1))
            except AccessError as exc:
                logger.info("full worker deferred: %s", exc.code)
                due = datetime.now(UTC) + timedelta(seconds=1 if exc.status in {409, 429} else 5)
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
