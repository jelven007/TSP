"""自动跟单规则域 — 监控事件触发模拟盘自动下单 (V2)。

规则匹配: 事件 source=="strategy" 且 strategy_id 相等 (跟策略), 或 rule_id 相等
(跟任意监控规则, 含信号/价格规则)。策略可自动跟随入场/退出方向, 自动退出全平。
退出目标按持仓周期持久保存, T+1/待卖单/撮合拒绝后继续重试, 完成或取消后归档。
仓位: 固定金额 或 账户权益百分比; 最大持仓计入当前持仓和待成交买单标的。
冷却: 同规则同 symbol 最近一次自动下单后 N 个日历日内不再触发 (按订单
created_at 日期判断); 自动退出不受冷却限制。

规则按账户存放 (accounts/{account_id}/auto_rules/), 账户间隔离;
on_rule_events 由钩子对每个账户各调一次。

触发链路: 监控引擎逐证券策略事件 + 通用规则事件 →
paper_auto.on_rule_events → paper.create_order(source=f"auto:{rule_id}")。
重试链路: 实时轮询/盘后结算 → retry_exit_intents (没有新信号也推进)。
所有下单走 paper 域的同一把锁与校验, 账户冻结/资金/涨跌停等约束自动生效。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now, published_trading_days
from app.multitenancy.paper_storage import PaperSession, paper_lock, paper_operation
from app.services.fs_utils import atomic_write_text
from app.strategy import paper
from app.strategy.risk_policy import RiskPolicy, price_exit

logger = logging.getLogger(__name__)


def _dir(data_dir: Path, account_id: str) -> Path:
    d = paper._root(data_dir, account_id) / "auto_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id() -> str:
    return f"arule_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def validate_rule(rule: dict) -> None:
    """校验规则字段, 非法抛 ValueError (中文信息)。"""
    if (rule.get("name") or "").strip() == "":
        raise ValueError("规则名称不能为空")
    if rule.get("match_kind") not in ("strategy", "rule"):
        raise ValueError(f"match_kind 非法: {rule.get('match_kind')!r} (应为 strategy / rule)")
    if not (rule.get("match_id") or "").strip():
        raise ValueError("match_id 不能为空")
    if rule.get("side") not in ("buy", "sell", "auto"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("side") == "auto" and rule.get("match_kind") != "strategy":
        raise ValueError("自动方向仅支持跟策略, 监控规则请指定买入或卖出")
    if rule.get("size_mode") not in ("fixed_amount", "pct_equity"):
        raise ValueError(f"size_mode 非法: {rule.get('size_mode')!r}")
    value = rule.get("size_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("size_value 必须是正数")
    if rule.get("size_mode") == "pct_equity" and value > 100:
        raise ValueError("pct_equity 的 size_value 不能超过 100 (%)")
    if rule.get("order_type") not in ("market", "next_open", "close"):
        raise ValueError(f"order_type 非法: {rule.get('order_type')!r}")
    cooldown = rule.get("cooldown_days", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("cooldown_days 必须是非负整数")
    max_positions = rule.get("max_positions", 0)
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 0:
        raise ValueError("max_positions 必须是非负整数 (0 表示不限)")
    if rule.get("candidate_priority", "event") not in ("event", "score", "symbol"):
        raise ValueError("candidate_priority 应为 event / score / symbol")
    for key in ("max_symbol_exposure_pct", "max_total_exposure_pct"):
        value = rule.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{key} 必须在 0~100 (%) 内, 0 表示不限")
    if not isinstance(rule.get("risk_follow", False), bool):
        raise ValueError("risk_follow 必须是布尔值")
    if rule.get("risk_follow") and (rule["match_kind"] != "strategy" or rule["side"] == "sell"):
        raise ValueError("策略风控仅支持跟策略买入或自动方向")


def normalize_rule(rule: dict) -> dict:
    d = dict(rule)
    d["name"] = (d.get("name") or "").strip()
    d["match_id"] = (d.get("match_id") or "").strip()
    d.setdefault("side", "buy")
    d.setdefault("size_mode", "fixed_amount")
    d.setdefault("order_type", "next_open")
    d.setdefault("cooldown_days", 5)
    d.setdefault("max_positions", 0)
    d.setdefault("candidate_priority", "event")
    d.setdefault("max_symbol_exposure_pct", 0)
    d.setdefault("max_total_exposure_pct", 0)
    d.setdefault("risk_follow", False)
    d.setdefault("enabled", True)
    d.setdefault("created_at", _now_iso())
    return d


@paper_operation()
def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    paper.validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        rules = [normalize_rule(rule) for rule in data_dir.document_values(f"paper_auto:{account_id}")]
        return [rule for rule in rules if not enabled_only or rule.get("enabled")]
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = normalize_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:
            logger.warning("paper auto rule load failed %s: %s", f.name, e)
    return out


@paper_operation(write=True)
def save_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    validate_rule(rule)
    with paper_lock(data_dir, paper.PAPER_LOCK):
        old = next((r for r in load_auto_rules(data_dir, account_id) if r["id"] == rule["id"]), None)
        if not rule.get("enabled", True) or (old and _exit_rule_signature(old) != _exit_rule_signature(rule)):
            _cancel_rule_exits(data_dir, rule["id"], account_id, "自动规则停用或执行配置已变更")
        elif old and old.get("risk_follow") and not rule.get("risk_follow"):
            for intent in load_exit_intents(data_dir, account_id, active_only=True):
                if intent["rule_id"] == rule["id"] and intent.get("kind") == "risk":
                    _finish_intent(data_dir, intent, account_id, "cancelled", "策略风控已关闭")
        if isinstance(data_dir, PaperSession):
            if paper.get_account(data_dir, account_id) is None:
                raise ValueError("尚未创建模拟账户")
            namespace = f"paper_auto:{account_id}"
            if old is None and len(data_dir.document_values(namespace)) >= 100:
                raise ValueError("自动跟单规则数量已达上限")
            data_dir.put_document(namespace, rule["id"], rule)
        else:
            atomic_write_text(_dir(data_dir, account_id) / f"{rule['id']}.json",
                              json.dumps(rule, ensure_ascii=False, indent=2))
        return rule


@paper_operation(write=True)
def create_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    rule = normalize_rule({**rule, "id": _new_id()})
    validate_rule(rule)
    return save_auto_rule(data_dir, rule, account_id)


@paper_operation(write=True)
def delete_auto_rule(data_dir: Path, rule_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    paper.validate_account_id(account_id)
    with paper_lock(data_dir, paper.PAPER_LOCK):
        _cancel_rule_exits(data_dir, rule_id, account_id, "自动规则已删除")
        if isinstance(data_dir, PaperSession):
            return data_dir.delete_document(f"paper_auto:{account_id}", rule_id)
        p = _dir(data_dir, account_id) / f"{rule_id}.json"
        if p.exists():
            p.unlink()
            return True
        return False


@paper_operation(write=True)
def set_enabled(data_dir: Path, rule_id: str, enabled: bool, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    with paper_lock(data_dir, paper.PAPER_LOCK):
        rule = next((r for r in load_auto_rules(data_dir, account_id) if r["id"] == rule_id), None)
        if rule is None:
            return None
        rule["enabled"] = enabled
        return save_auto_rule(data_dir, rule, account_id)


def _matches(rule: dict, ev: dict) -> bool:
    if rule["match_kind"] == "strategy":
        return ev.get("source") == "strategy" and ev.get("strategy_id") == rule["match_id"]
    return ev.get("rule_id") == rule["match_id"]


def _in_cooldown(data_dir: Path, rule: dict, symbol: str, cooldown_days: int, account_id: str) -> bool:
    """同规则同 symbol 最近一次自动下单是否仍在冷却期 (按日历日, 含当日)。"""
    if cooldown_days <= 0:
        return False
    prefix = f"auto:{rule['id']}"
    today = cn_now().date()
    for order in paper.load_orders(data_dir, account_id):
        if order.get("source") != prefix or order.get("symbol") != symbol:
            continue
        try:
            created = _date.fromisoformat(order["created_at"][:10])
        except (KeyError, ValueError):
            continue
        if (today - created).days < cooldown_days:
            return True
    return False


def _sizing_qty(data_dir: Path, rule: dict, ref_price: float, account_id: str, prices=None) -> int:
    if ref_price <= 0:
        return 0
    budget = paper.portfolio_budget(data_dir, account_id, prices=prices)
    if rule["size_mode"] == "fixed_amount":
        amount = float(rule["size_value"])
    else:  # pct_equity: 按账户总权益 (现金 + 最新定版持仓市值)
        acc = paper.get_account(data_dir, account_id)
        if acc is None:
            return 0
        amount = budget["equity"] * float(rule["size_value"]) / 100.0
    if rule["side"] == "sell":
        symbol = rule["_symbol"]
        position = paper.load_positions(data_dir, account_id).get(symbol)
        if position is None:
            return 0
        available = paper._available_of(position, cn_now().date().isoformat())
        pending = sum(
            int(order["qty"]) for order in paper.load_orders(data_dir, account_id)
            if order["status"] == "pending" and order["side"] == "sell"
            and order["symbol"] == symbol
        )
        # 调用方持有账户写锁/事务; 金额可表达清仓, 但仍保留 T+1 与待卖占用。
        remaining = max(0, available - pending) // paper.LOT_SIZE * paper.LOT_SIZE
        return min(paper.qty_from_amount(amount, ref_price), remaining)
    exposure_room = math.inf
    for key, used in (
        ("max_symbol_exposure_pct", budget["holdings"].get(rule.get("_symbol"), 0) + budget["pending"].get(rule.get("_symbol"), 0)),
        ("max_total_exposure_pct", budget["exposure"]),
    ):
        if rule.get(key):
            exposure_room = min(exposure_room, max(0.0, budget["equity"] * rule[key] / 100 - used))
    acc = paper.get_account(data_dir, account_id)
    execution = paper.apply_slippage(ref_price, "buy", acc["slippage_bps"])
    qty = min(paper.qty_from_amount(amount, ref_price),
              paper.qty_from_amount(min(exposure_room, budget["available_cash"]), execution))
    while qty > 0:
        fee = paper.buy_fee(qty, execution, acc["commission_pct"])
        projected = {**budget, "equity": budget["equity"] - fee}
        if (qty * execution + fee <= budget["available_cash"] + 1e-6
                and not paper.exposure_error(projected, rule.get("_symbol"), qty * execution, rule)):
            break
        qty -= paper.LOT_SIZE
    return qty


def _resolve_side(rule: dict, ev: dict) -> str | None:
    """固定方向保持原行为; 自动方向只接受策略入场/退出事件。"""
    side = rule.get("side", "buy")
    if side != "auto":
        return side
    if rule["match_kind"] != "strategy" or ev.get("source") != "strategy":
        return None
    if ev.get("type") in ("buy_signal", "pool_entry"):
        return "buy"
    if ev.get("type") in ("sell_signal", "pool_exit"):
        return "sell"
    return None


def _position_slots_used(data_dir: Path, account_id: str) -> set[str]:
    """当前持仓与待成交买单占用的标的; 调用方在账户写事务/锁内检查并下单。"""
    used = {
        symbol for symbol, pos in paper.load_positions(data_dir, account_id).items()
        if pos["qty"] > 0
    }
    used.update(
        order["symbol"] for order in paper.load_orders(data_dir, account_id)
        if order["status"] == "pending" and order["side"] == "buy"
    )
    return used


_ACTIVE_EXITS = {"pending", "submitted"}


def _record_dir(data_dir: Path, kind: str, account_id: str) -> Path:
    path = paper._root(data_dir, account_id) / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record(data_dir, kind, key, account_id):
    if isinstance(data_dir, PaperSession):
        return data_dir.document(f"paper_{kind}:{account_id}", key)
    path = _record_dir(data_dir, kind, account_id) / f"{key}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _put_record(data_dir, kind, key, value, account_id):
    if isinstance(data_dir, PaperSession):
        if kind == "exit" and value["status"] not in _ACTIVE_EXITS:
            data_dir.put_document(f"paper_exit_archive:{account_id}", key, value)
            data_dir.delete_document(f"paper_exit:{account_id}", key)
        else:
            data_dir.put_document(f"paper_{kind}:{account_id}", key, value)
    else:
        atomic_write_text(_record_dir(data_dir, kind, account_id) / f"{key}.json",
                          json.dumps(value, ensure_ascii=False, indent=2))
        if kind == "exit" and value["status"] not in _ACTIVE_EXITS:
            # Atomic rename keeps terminal history out of the polling path.
            (_record_dir(data_dir, kind, account_id) / f"{key}.json").replace(
                _record_dir(data_dir, "exit_archive", account_id) / f"{key}.json",
            )


@paper_operation()
def load_exit_intents(
    data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, *, active_only: bool = False,
) -> list[dict]:
    """账户持久退出待办及终态; 读取损坏的文件时失败, 避免静默丢失退出。"""
    paper.validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        values = data_dir.document_values(f"paper_exit:{account_id}")
        if not active_only:
            values += data_dir.document_values(f"paper_exit_archive:{account_id}")
        return values
    paths = list(_record_dir(data_dir, "exit", account_id).glob("*.json"))
    if not active_only:
        paths.extend(_record_dir(data_dir, "exit_archive", account_id).glob("*.json"))
    values = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(paths)}
    return [value for value in values.values() if not active_only or value["status"] in _ACTIVE_EXITS]


def _exit_rule_signature(rule: dict) -> list:
    # 金额、买入冷却、持仓上限、显示文案不改变全平意图。
    return [rule.get(key) for key in ("match_kind", "match_id", "side", "order_type")]


def _update_intent(data_dir, intent, account_id, **changes):
    if all(intent.get(key) == value for key, value in changes.items()):
        return
    intent.update(changes, updated_at=_now_iso())
    _put_record(data_dir, "exit", intent["id"], intent, account_id)


def _finish_intent(data_dir, intent, account_id, status, reason):
    for order in paper.load_orders(data_dir, account_id):
        if order.get("exit_intent_id") == intent["id"] and order["status"] == "pending":
            paper.cancel_order(data_dir, order["id"], account_id)
    _update_intent(data_dir, intent, account_id, status=status, last_error=reason)


def _cancel_rule_exits(data_dir, rule_id, account_id, reason):
    for intent in load_exit_intents(data_dir, account_id, active_only=True):
        if intent["rule_id"] == rule_id and intent["status"] in _ACTIVE_EXITS:
            _finish_intent(data_dir, intent, account_id, "cancelled", reason)


def _position_cycle(data_dir, symbol, position, account_id):
    if position.get("cycle_id"):
        return position["cycle_id"]
    # Existing materialized positions predate cycle_id. Derive their identity
    # from the ledger rather than quantity/date, which can repeat after reentry.
    paper._materialize(data_dir, account_id)
    replayed = paper.load_positions(data_dir, account_id).get(symbol, {})
    position.update(replayed)
    return replayed.get("cycle_id")


def _valid_price(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _created_order(data_dir, order, account_id):
    if isinstance(data_dir, PaperSession):
        event = auto_order_events([order], account_id)[0]
        data_dir.alert({**event, "id": f"auto:{account_id}:{order['id']}"})


# ── 未执行原因留痕 ─────────────────────────────────────────
# 自动跟单一轮里被跳过的候选 (冷却/名额/缺价/风控快照缺失/额度不足/下单被拒等) 记入
# 一个按账户的有界环形列表, 供诊断"为什么没下单", 与成交/退出待办分开, 不影响撮合。
_MAX_SKIPS = 200
_SKIP_LABELS = {
    "active_exit": "该标的有未完成的退出待办, 暂不自动买入",
    "cooldown": "买入冷却期内",
    "max_positions": "持仓数已达规则上限",
    "missing_entry_policy": "缺入场政策快照, 风控跟随规则不买入",
    "no_reference_price": "缺有效参考价",
    "zero_qty": "按现金与仓位余量算得可买数量为 0",
    "no_sellable_qty": "按规则金额、T+1 可卖数量及待卖占用算得可卖数量为 0",
    "duplicate": "同规则同标的本轮已下过单",
    "order_rejected": "下单被拒 (资金/涨跌停/仓位额度等)",
}


def _skip_namespace(account_id: str) -> str:
    return f"paper_auto_skips:{account_id}"


def _record_skip(data_dir, account_id, rule, ev, reason, detail=None):
    """追加一条未执行原因 (有界, 新的在前)。文件与 SQL 两种存储都写单条列表文档。"""
    entry = {
        "ts": _now_iso(),
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "symbol": (ev.get("symbol") or "").strip(),
        "side": rule.get("side"),
        "reason": reason,
        "message": _SKIP_LABELS.get(reason, reason),
        "detail": detail,
    }
    existing = load_auto_skips(data_dir, account_id)
    updated = [entry, *existing][:_MAX_SKIPS]
    if isinstance(data_dir, PaperSession):
        data_dir.put_document(_skip_namespace(account_id), "default", {"items": updated})
    else:
        atomic_write_text(
            _record_dir(data_dir, "auto_skips", account_id) / "default.json",
            json.dumps({"items": updated}, ensure_ascii=False, indent=2),
        )
    return entry


@paper_operation()
def load_auto_skips(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """读取未执行原因列表 (新的在前, 有界)。"""
    paper.validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        doc = data_dir.document(_skip_namespace(account_id), "default")
        return list(doc.get("items", [])) if doc else []
    path = _record_dir(data_dir, "auto_skips", account_id) / "default.json"
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("items", []))
    except Exception:
        return []


@paper_operation(write=True)
def retry_exit_intents(
    data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, *,
    prices: dict[str, float] | None = None, allow_market: bool = False, submit: bool = True,
) -> list[dict]:
    """对账并重试自动全平。即时单须有调用方已验证的新鲜行情; 顺延单可无行情。

    submit=False 用于撮合前核对持仓周期/规则, 撤销失效的旧退出单。
    T+1 或其他待卖单阻塞时等待全量可卖, 不自动切换为部分卖出。
    """
    created = []
    with paper_lock(data_dir, paper.PAPER_LOCK):
        intents = load_exit_intents(data_dir, account_id, active_only=True)
        if not intents:
            return created
        rules = {rule["id"]: rule for rule in load_auto_rules(data_dir, account_id)}
        positions = paper.load_positions(data_dir, account_id)
        account = paper.get_account(data_dir, account_id)
        orders = paper.load_orders(data_dir, account_id)
        for intent in intents:
            if intent["status"] not in _ACTIVE_EXITS:
                continue
            rule = rules.get(intent["rule_id"])
            is_risk = intent.get("kind") == "risk"
            if (not rule or not rule.get("enabled")
                    or (is_risk and not rule.get("risk_follow"))
                    or (not is_risk and _exit_rule_signature(rule) != intent["rule_signature"])):
                _finish_intent(data_dir, intent, account_id, "cancelled", "自动规则停用、删除或执行配置已变更")
                continue
            symbol = intent["symbol"]
            execution_rule = intent["execution_rule"] if is_risk else rule
            pos = positions.get(symbol)
            if not pos or pos["qty"] <= 0:
                _finish_intent(data_dir, intent, account_id, "completed", None)
                continue
            if _position_cycle(data_dir, symbol, pos, account_id) != intent["cycle_id"]:
                _finish_intent(data_dir, intent, account_id, "cancelled", "原持仓周期已结束")
                continue
            linked = [order for order in orders if order.get("exit_intent_id") == intent["id"]]
            pending = [order for order in linked if order["status"] == "pending"]
            for order in pending:
                if order["qty"] != pos["qty"]:
                    paper.cancel_order(data_dir, order["id"], account_id)
                    order["status"] = "cancelled"
            if any(order["status"] == "pending" for order in pending):
                _update_intent(data_dir, intent, account_id, status="submitted",
                               order_ids=[order["id"] for order in linked], last_error=None)
                continue
            _update_intent(data_dir, intent, account_id, status="pending",
                           order_ids=[order["id"] for order in linked])
            if not submit:
                continue
            if not account or account.get("status") != "active":
                _update_intent(data_dir, intent, account_id, last_error="账户已冻结, 退出暂停")
                continue
            if paper._available_of(pos, paper.cn_today().isoformat()) < pos["qty"]:
                _update_intent(data_dir, intent, account_id, last_error="可卖数量不足 (T+1), 等待全量可卖")
                continue
            if any(order["status"] == "pending" and order["side"] == "sell" and order["symbol"] == symbol
                   for order in orders):
                _update_intent(data_dir, intent, account_id, last_error="已有待成交卖单, 等待其完成或撤销")
                continue
            price = (prices or {}).get(symbol)
            is_market = execution_rule["order_type"] == "market" and pos.get("asset_type", "stock") == "stock"
            if is_market and (not allow_market or not _valid_price(price)):
                _update_intent(data_dir, intent, account_id, last_error="等待新鲜行情")
                continue
            if is_market and linked:
                limit_error = paper._limit_price_error(
                    data_dir, symbol, "sell", pos.get("asset_type", "stock"),
                    paper.apply_slippage(float(price), "sell", float(account["slippage_bps"])),
                    paper.cn_today().isoformat(),
                )
                if limit_error:
                    _update_intent(data_dir, intent, account_id, last_error=limit_error)
                    continue
            if not _valid_price(price):
                price = intent.get("signal_price")
            qty = int(pos["qty"])
            if qty != pos["qty"]:
                _update_intent(data_dir, intent, account_id, last_error="全平数量不符合当前订单数量规则")
                continue
            order, error = paper.create_order(
                data_dir, symbol, "sell", account_id=account_id, qty=qty,
                order_type=execution_rule["order_type"], asset_type=pos.get("asset_type", "stock"),
                ref_price=float(price) if _valid_price(price) else None,
                source=f"auto:{rule['id']}", exit_intent_id=intent["id"],
                idempotency_key=f"exit:{intent['id']}:{len(linked)}",
            )
            if error:
                _update_intent(data_dir, intent, account_id, last_error=error)
                continue
            _update_intent(data_dir, intent, account_id, status="submitted", last_error=None,
                           order_ids=[item["id"] for item in linked] + [order["id"]])
            _created_order(data_dir, order, account_id)
            created.append(order)
            orders.append(order)
    return created


@paper_operation(write=True)
def evaluate_position_risk(data_dir, prices, account_id=paper.DEFAULT_ACCOUNT_ID, *,
                           day=None, quote_times=None, submit=True):
    """Latch risks using a cycle's first-fill policy and previously observed peak.

    No current strategy lookup or private code execution. Missing old snapshots
    remain unmanaged. T+1/frozen accounts retain the intent until executable.
    """
    day = day or paper.cn_today().isoformat()
    created = []
    with paper_lock(data_dir, paper.PAPER_LOCK):
        from app.strategy import paper_actions

        action_status = paper_actions.status(data_dir, day, account_id)
        waiting_symbols = {row["symbol"] for row in action_status["events"]
                           if row["ex_date"] <= day and (row.get("reason") or
                               ("ex" not in row["stages"] and ("register" not in row["stages"] or row["base_qty"])))}
        rules = {rule["id"]: rule for rule in load_auto_rules(data_dir, account_id, enabled_only=True)}
        for symbol, pos in paper.load_positions(data_dir, account_id).items():
            snapshot = pos.get("entry_policy")
            if (pos["qty"] <= 0 or not snapshot or pos.get("risk_adjustment_unknown")
                    or symbol in waiting_symbols):
                continue
            rule_id = snapshot.get("auto_rule_id")
            rule = rules.get(rule_id)
            if not rule or not rule.get("risk_follow"):
                continue
            factor_today = paper._factor_on(data_dir, symbol, pos.get("asset_type", "stock"), day)
            if (factor_today not in (None, 1.0) and pos.get("entry_date", day) < day
                    and not any((fill.get("kind") == "corp_action"
                                 or (fill.get("kind") == "corporate_action" and fill.get("stage") == "ex"))
                                and fill["symbol"] == symbol and fill["date"] == day
                                for fill in paper.load_fills(data_dir, account_id))):
                continue  # Await ledger adjustment; raw ex-price alone is not a loss.
            policy = RiskPolicy.from_mapping(snapshot["policy"])
            key = str(pos["cycle_id"])
            entry = float(pos["risk_entry_price"])
            factor = pos.get("risk_factor", 1.0)
            state = _record(data_dir, "risk", key, account_id) or {
                "peak": entry, "factor": factor, "last_day": pos["entry_date"],
            }
            if state.get("last_day", "") > day:
                continue
            peak = state["peak"] * state.get("factor", 1.0) / factor
            price = prices.get(symbol)
            stamp = (quote_times or {}).get(symbol)
            if quote_times is not None:
                if not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
                    continue
                stamp = stamp / 1000 if stamp > 1e12 else stamp
                if day != paper.cn_today().isoformat() or not -2 <= paper.cn_now().timestamp() - stamp <= 15:
                    continue
                if stamp <= state.get("last_quote_ts", 0):
                    continue
            reason = None
            if _valid_price(price):
                reason, _ = price_exit(policy, entry, peak, float(price), float(price), float(price))
            if reason is None and policy.max_hold_days:
                start, end = _date.fromisoformat(pos["entry_date"]), _date.fromisoformat(day)
                calendars = [published_trading_days(year) for year in range(start.year, end.year + 1)]
                if all(calendar is not None for calendar in calendars):
                    held = sum(start < trading_day <= end for calendar in calendars for trading_day in calendar)
                    if held >= policy.max_hold_days:
                        reason = "max_hold"
            state.update(peak=max(peak, float(price)) if _valid_price(price) else peak,
                         factor=factor, last_day=day)
            if stamp is not None:
                state["last_quote_ts"] = stamp
            _put_record(data_dir, "risk", key, state, account_id)
            if reason and not any(
                intent["symbol"] == symbol and intent["cycle_id"] == key
                for intent in load_exit_intents(data_dir, account_id, active_only=True)
            ):
                intent_id = hashlib.sha256(f"risk:{rule_id}:{key}:{uuid.uuid4().hex}".encode()).hexdigest()
                intent = {
                    "id": intent_id, "kind": "risk", "rule_id": rule_id, "symbol": symbol,
                    "cycle_id": key, "rule_signature": _exit_rule_signature(rule),
                    "execution_rule": {"order_type": "market" if pos.get("asset_type", "stock") == "stock" else "next_open"},
                    "risk_reason": reason, "status": "pending", "signal_price": price,
                    "event": {"type": "risk_exit", "day": day}, "order_ids": [],
                    "last_error": None, "created_at": _now_iso(), "updated_at": _now_iso(),
                }
                _put_record(data_dir, "exit", intent_id, intent, account_id)
        if submit:
            created = retry_exit_intents(data_dir, account_id, prices=prices, allow_market=True)
    return created


@paper_operation(write=True)
def on_rule_events(
    data_dir: Path, events: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID, *,
    prices: dict[str, float] | None = None, allow_entries: bool = True,
) -> list[dict]:
    """逐证券事件 → 跟单。先保存退出意图, 再处理入场, 最后重试全部退出待办。

    带 id/ts 的事件在文件模式也保存处理回执。退出回执与待办分离, 重放
    不重复创建意图, 空事件仍可推进待办; prices 应由调用方提供当前有效行情。
    allow_entries=False 时只接收自动退出目标, 供计算后行情失效的 Worker 使用。
    """
    created = []
    with paper_lock(data_dir, paper.PAPER_LOCK):
        retry_exit_intents(data_dir, account_id, submit=False)
        rules = load_auto_rules(data_dir, account_id, enabled_only=True)
        matches = [(rule, ev, _resolve_side(rule, ev))
                   for ev in events if (ev.get("symbol") or "").strip()
                   for rule in rules if _matches(rule, ev)]
        # Persist exit instructions before any entry can add to the same position.
        def priority(item):
            rule, ev, side = item
            mode = rule.get("candidate_priority", "event")
            score = ev.get("score")
            valid = not isinstance(score, bool) and isinstance(score, (float, int)) and math.isfinite(score)
            rank = -float(score) if mode == "score" and valid else (math.inf if mode == "score" else 0)
            return (not (rule["side"] == "auto" and side == "sell"), rank,
                    ev["symbol"] if mode in ("score", "symbol") else "", rule["id"])
        matches.sort(key=priority)
        allocation_prices = dict(prices or {})
        for _, ev, _ in matches:
            if _valid_price(ev.get("price")):
                allocation_prices.setdefault(ev["symbol"], ev["price"])
        event_prices = {}
        bought = set()
        for rule, ev, side in matches:
            symbol = ev["symbol"].strip()
            auto_exit = rule["side"] == "auto" and side == "sell"
            if not allow_entries and not auto_exit:
                continue
            if not auto_exit and prices is not None and symbol not in prices:
                continue
            price = prices.get(symbol, ev.get("price")) if prices is not None else ev.get("price")
            if side is None or (not auto_exit and not _valid_price(price)):
                if side == "buy":
                    _record_skip(data_dir, account_id, rule, ev, "no_reference_price")
                continue
            receipt = hashlib.sha256(json.dumps([rule["id"], ev], sort_keys=True).encode()).hexdigest()
            durable_receipt = isinstance(data_dir, PaperSession) or "id" in ev or "ts" in ev
            if durable_receipt and _record(data_dir, "event", receipt, account_id):
                continue
            if auto_exit:
                pos = paper.load_positions(data_dir, account_id).get(symbol)
                if pos and pos["qty"] > 0:
                    cycle = _position_cycle(data_dir, symbol, pos, account_id)
                    if cycle is None:
                        raise ValueError("无法从成交台账确定持仓周期")
                    active = next((intent for intent in load_exit_intents(data_dir, account_id, active_only=True)
                                   if intent["rule_id"] == rule["id"] and intent["symbol"] == symbol
                                   and intent["cycle_id"] == cycle and intent["status"] in _ACTIVE_EXITS), None)
                    if active is None:
                        intent = {
                            "id": hashlib.sha256(json.dumps([receipt, cycle]).encode()).hexdigest(),
                            "rule_id": rule["id"], "symbol": symbol,
                            "cycle_id": cycle, "rule_signature": _exit_rule_signature(rule),
                            "status": "pending", "signal_price": price if _valid_price(price) else None,
                            "event": ev, "order_ids": [], "last_error": None,
                            "created_at": _now_iso(), "updated_at": _now_iso(),
                        }
                        _put_record(data_dir, "exit", intent["id"], intent, account_id)
                if _valid_price(price):
                    event_prices[symbol] = float(price)
                if durable_receipt:
                    _put_record(data_dir, "event", receipt, {"processed": True}, account_id)
                continue
            if durable_receipt:
                _put_record(data_dir, "event", receipt, {"processed": True}, account_id)
            if side == "buy" and any(
                intent["symbol"] == symbol and intent["status"] in _ACTIVE_EXITS
                for intent in load_exit_intents(data_dir, account_id, active_only=True)
            ):
                _record_skip(data_dir, account_id, rule, ev, "active_exit")
                continue
            if _in_cooldown(data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id):
                _record_skip(data_dir, account_id, rule, ev, "cooldown")
                continue
            if side == "buy" and rule.get("max_positions", 0) > 0:
                used = _position_slots_used(data_dir, account_id)
                if symbol not in used and len(used) >= rule["max_positions"]:
                    _record_skip(data_dir, account_id, rule, ev, "max_positions",
                                 detail={"used": len(used), "max_positions": rule["max_positions"]})
                    continue
            if side == "buy" and rule.get("risk_follow") and not ev.get("entry_policy"):
                _record_skip(data_dir, account_id, rule, ev, "missing_entry_policy")
                continue
            qty = _sizing_qty(data_dir, {**rule, "_symbol": symbol}, float(price), account_id, allocation_prices)
            if qty <= 0:
                _record_skip(data_dir, account_id, rule, ev, "no_sellable_qty" if side == "sell" else "zero_qty")
                continue
            if side == "buy" and (rule["id"], symbol) in bought:
                _record_skip(data_dir, account_id, rule, ev, "duplicate")
                continue
            order, error = paper.create_order(
                data_dir, symbol, side, account_id=account_id, qty=qty,
                order_type=rule["order_type"], ref_price=float(price), source=f"auto:{rule['id']}",
                entry_policy={**ev["entry_policy"], "auto_rule_id": rule["id"]} if side == "buy" and ev.get("entry_policy") else None,
                risk_limits={key: rule[key] for key in ("max_symbol_exposure_pct", "max_total_exposure_pct")} if side == "buy" else None,
                valuation_prices=allocation_prices,
            )
            if error:
                logger.info("paper auto %s: %s 下单被拒: %s", rule["name"], symbol, error)
                _record_skip(data_dir, account_id, rule, ev, "order_rejected", detail={"error": error})
                continue
            created.append(order)
            if side == "buy":
                bought.add((rule["id"], symbol))
            _created_order(data_dir, order, account_id)
        created.extend(retry_exit_intents(
            data_dir, account_id, prices=event_prices if prices is None else prices,
            allow_market=True,
        ))
    return created


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}


def auto_order_events(created: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """自动跟单创建的订单 → 推送事件。与成交事件同构 (source=paper 对齐
    AlertEvent), 走同一推送通道; rule_id 从 order.source (auto:{rule_id}) 还原。
    """
    events: list[dict] = []
    for o in created:
        source = str(o.get("source", ""))
        rule_id = source.removeprefix("auto:") if source.startswith("auto:") else ""
        side_label = "买入" if o["side"] == "buy" else "卖出"
        type_label = _ORDER_TYPE_LABEL.get(o["order_type"], o["order_type"])
        events.append({
            "source": "paper",
            "type": "auto_order",
            "severity": "info",
            "ts": int(cn_now().timestamp() * 1000),
            "symbol": o["symbol"],
            "rule_id": rule_id,
            "side": o["side"],
            "qty": o["qty"],
            "account_id": account_id,
            "message": f"自动跟单触发{side_label}下单 ({type_label}) {o['qty']}股",
        })
    return events
