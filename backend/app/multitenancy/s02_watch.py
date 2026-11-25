"""API独立观察S02任务: 读取实际身份/回执/Worker心跳并保存异常证据。

不采集、不计算策略、不发送外部消息; Worker受阻时仍可观测。
异常阈值统一由s02_run_evidence定义, 当前契约见docs/reference/s02.md。
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict

from app.cell.context import AccessError
from app.market_time import cn_now
from app.multitenancy.documents import TenantDocuments
from app.services.s02_run_evidence import SID, save_operational

logger = logging.getLogger(__name__)


class S02Watch:
    """按租户分页巡检, 每秒推进; 当前观察和首次异常分别持久保存。"""
    def __init__(self, db, identity):
        self.db, self.identity = db, identity
        self.stop = threading.Event()
        self.thread = None
        self.cursor = ""

    def start(self):
        """启动API观察线程, 与策略Compute和Worker调度线程分离。"""
        self.thread = threading.Thread(target=self._run, name="s02-independent-watch", daemon=True)
        self.thread.start()

    def close(self):
        """发出停止信号, 有限等待线程退出。"""
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)

    @staticmethod
    def _windows(unit):
        """只加载已保存S02定义及覆盖, 获得真实窗口和运行身份。"""
        from app.multitenancy.strategies import TenantStrategyEngine
        from app.services.strategy_monitor_tasks import task_windows
        from app.services.strategy_results import capture_identity
        from app.strategy.config import load_override
        from app.strategy.engine import StrategyEngine

        # 观察器已持租户事务; 不加载无关内置源码, 避免解析期间阻塞API/Worker读写。
        engine = TenantStrategyEngine(unit, StrategyEngine([]), None)
        overrides = load_override(unit, SID)
        return (task_windows(engine, engine.get(SID), overrides),
                asdict(capture_identity(engine, unit, SID, overrides=overrides,
                                        timeframe="1m"))["runtime_identity"])

    def observe(self, unit, now, *, worker_ready):
        """只消费当前启用策略同世代/身份回执, 旧记录不能遮蔽今日漏触发。"""
        monitoring = unit.get("strategy_monitoring", SID, {})
        if not monitoring.get("enabled") or not unit.get("strategy_sources", SID):
            return
        windows, identity = self._windows(unit)
        receipt = unit.get("strategy_monitor_task_runs", f"{SID}:{now.date()}", {})
        if (receipt.get("generation") != monitoring["generation"]
                or receipt.get("runtime_identity") != identity):
            receipt = {}
        steps = (receipt.get("tasks") or {}).get("buy", {}).get("signals", {})
        status = {phase: {"state": step.get("input_state"), "reason": step.get("input_reason")}
                  for signal in steps.values() for phase in ("stocks", "breadth")
                  if (step := signal.get(phase))}
        save_operational(unit, {
            "strategy_task_windows": {SID: windows},
            "strategy_task_generations": {SID: monitoring["generation"]},
            "daily_identities": {SID: {"runtime_identity": identity}},
            "strategy_task_receipts": {SID: receipt},
            "s02_input_status": {SID: status},
        }, now, {}, observer="api", worker_ready=worker_ready)

    def tick(self):
        """读取活跃租户及实际心跳, 逐租户授权后写观察, 错误不阻断其他租户。"""
        from app.multitenancy.health import worker_fresh
        from app.multitenancy.research import authorize

        with self.db.control.connection() as conn:
            tenants = conn.execute(
                """SELECT t.id AS tenant_id,u.* FROM control.tenants t
                   JOIN control.memberships m ON m.tenant_id=t.id AND m.role='owner'
                   JOIN control.users u ON u.id=m.user_id
                   WHERE t.cell_id=%s AND t.status='active' AND t.id>%s
                   AND u.active AND u.account_state='active'
                   ORDER BY t.id LIMIT 100""", (self.db.settings.id, self.cursor)).fetchall()
            heartbeat = conn.execute(
                """SELECT healthy,extract(epoch FROM clock_timestamp()-checked_at) AS age
                   FROM control.full_worker_health WHERE cell_id=%s AND kind='realtime'""",
                (self.db.settings.id,)).fetchone()
            accounts = [(row["tenant_id"], self.identity._public(conn, row)) for row in tenants]
        ready = bool(heartbeat and worker_fresh("realtime", heartbeat["healthy"], float(heartbeat["age"])))
        self.cursor = tenants[-1]["tenant_id"] if len(tenants) == 100 else ""
        now = cn_now()
        for tenant_id, account in accounts:
            if self.stop.is_set():
                break
            try:
                context = self.identity.context(account, tenant_id, uuid.uuid4().hex)
                with (authorize(self.identity, context, account["auth_version"]),
                      TenantDocuments(self.db, context).transaction() as unit):
                    self.observe(unit, now, worker_ready=ready)
            except AccessError:
                logger.info("S02 observer skipped unavailable tenant authorization")
            except (KeyError, ValueError):
                logger.exception("S02 observer rejected invalid saved tenant strategy")

    def _run(self):
        """单次巡检异常记录日志后继续, 停止事件可立即打断间隔等待。"""
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("S02 independent observation failed")
            self.stop.wait(1)
