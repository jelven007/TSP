"""虚拟账户(模拟盘)域模块 — 虚拟资金 + 真实行情价格的模拟撮合, 不接券商不做实盘。

设计文档: docs/paper-trading-plan.md。与回测互补: 回测重放历史, 模拟盘实时向前。

账务正确性原则: fills.jsonl 成交台账是唯一事实源 (append-only), 持仓/现金/净值
全部可由台账重放推导; 修复一律以追加记录表达 (除权 corp_action / 冲正), 不改历史行。

价格口径: 全程不复权 raw 价 (与真实交易一致)。费用参数与回测引擎
(app/backtest/engine.py) 同名同默认值: commission_pct 双边佣金 / stamp_tax_pct
卖出印花税 / slippage_bps 滑点。

多账户 (V2): 数据目录 data/paper/accounts/{account_id}/, 账户间完全隔离
(订单/台账/持仓/净值/自动规则均按账户存放)。所有域函数带 account_id 参数
(默认 "default", 即迁移前的单账户)。旧版 data/paper/ 单账户布局首次访问时
自动迁移到 accounts/default/, 一次性且幂等。

撮合规则:
  - 即时单: 盘中由行情轮询钩子按最新快照价成交 (evaluate_intraday);
  - next_open / close 单: 盘后管道 settle_day 按当日 raw_open / raw_close 成交;
  - T+1: 当日买入次一交易日方可卖 (lots 按 buy_date 记账, available = date < today);
  - 涨跌停: 触及涨停的买单 / 跌停的卖单默认拒单 (expired 留痕); 账户开启
    queue_limit_orders 后转「排队次日重试」(转 next_open, 计顺延, 超限过期);
  - 停牌/缺行情: 顺延, 连续顺延超过 max_postpone 日自动过期;
  - 新账户按有日期证据的公司行为登记红利/送转/配股; 存量因子账本保留兼容。
"""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import threading
import uuid
from datetime import date as _date
from datetime import datetime
from datetime import time as _time
from pathlib import Path

import polars as pl

from app.instrument_history import instrument_status_on
from app.market_time import CN_TZ, cn_now, cn_today, published_trading_day
from app.multitenancy.paper_storage import PaperSession, market_path, paper_lock, paper_operation
from app.parquet import scan_daily_parquet
from app.price_limits import (
    is_risk_warning_name,
)
from app.price_limits import (
    price_limit_pct as stock_price_limit_pct,
)
from app.services.fs_utils import atomic_write_text
from app.strategy.paper_math import apply_slippage, buy_fee, sell_fee
from app.strategy.risk_policy import RiskPolicy, valid_price

logger = logging.getLogger(__name__)

# 单进程内全部写操作互斥 (API 下单 / 行情钩子撮合 / 盘后结算共用一把锁)
PAPER_LOCK = threading.RLock()

# 费用默认值 — 与回测 engine.py 一致
DEFAULT_COMMISSION_PCT = 0.00025   # 双边佣金 万2.5
MIN_COMMISSION = 5.0               # 佣金最低 5 元
DEFAULT_STAMP_TAX_PCT = 0.001      # 印花税, 仅卖出
DEFAULT_SLIPPAGE_BPS = 5.0         # 滑点 (bps, 万分之5)

MAX_POSTPONE_DAYS = 3             # 顺延上限: 连续 N 个交易日无法成交自动过期
# 结算价格的打印时刻 (北京墙钟): 开盘价 09:30 / 收盘价 15:00。下单晚于该时刻的单
# 不能按当日这个价成交 (下单时价格已知), 留到下一交易日结算
_SESSION_OPEN = _time(9, 30)
_SESSION_CLOSE = _time(15, 0)
LOT_SIZE = 100                     # 一手 100 股 (股票/ETF 同)
MAX_POSITION_SYMBOLS = 50          # 持仓标的数上限 (防误操作)
DEFAULT_ACCOUNT_ID = "default"
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_MIGRATION_DONE = False


# ── 路径与账户目录 ──────────────────────────────────────
def validate_account_id(account_id: str) -> str:
    """账户 ID 只允许字母/数字/下划线/短横线 (防路径穿越), 非法抛 ValueError。"""
    if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.match(account_id or ""):
        raise ValueError(f"账户 ID 非法 (仅限字母数字下划线短横线, ≤32 字符): {account_id!r}")
    return account_id


def accounts_base(data_dir: Path) -> Path:
    return Path(data_dir) / "paper" / "accounts"


def _migrate_legacy(data_dir: Path) -> None:
    """旧单账户布局 data/paper/* → data/paper/accounts/default/* (一次性, 幂等)。

    仅当旧 account.json 存在且 accounts/ 尚不存在时迁移; 移动失败只留痕不阻断
    (下次访问重试), 避免迁移异常导致模拟盘整体不可用。
    """
    legacy = Path(data_dir) / "paper"
    base = accounts_base(data_dir)
    if not (legacy / "account.json").exists() or base.exists():
        return
    dest = base / DEFAULT_ACCOUNT_ID
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("account.json", "orders", "fills.jsonl", "positions.json", "nav", "auto_rules"):
            src = legacy / name
            if src.exists():
                shutil.move(str(src), str(dest / name))
        logger.info("paper: 旧单账户数据已迁移到 %s", dest)
    except Exception as e:
        logger.warning("paper 旧数据迁移失败 (下次重试): %s", e)
        # 迁移半途而废时目标目录已建立, 会跳过后续重试 — 清掉半成品目录保证可重试
        try:
            if dest.exists() and not (dest / "account.json").exists():
                shutil.rmtree(dest, ignore_errors=True)
                if not any(base.iterdir()):
                    base.rmdir()
        except Exception:
            pass


def _root(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> Path:
    global _MIGRATION_DONE
    if not _MIGRATION_DONE:
        _migrate_legacy(data_dir)
        _MIGRATION_DONE = True
    d = accounts_base(data_dir) / validate_account_id(account_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _orders_dir(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> Path:
    d = _root(data_dir, account_id) / "orders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _nav_dir(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> Path:
    d = _root(data_dir, account_id) / "nav"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _order_sort_key(order: dict) -> tuple:
    """撮合顺序: created_at 先到先得; 同刻则按 id (内含毫秒戳+随机后缀, 稳定)。"""
    return (order.get("created_at", ""), order.get("id", ""))


# ── 账户 ────────────────────────────────────────────────
@paper_operation()
def get_account(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        return data_dir.account(account_id)
    p = _root(data_dir, account_id) / "account.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("paper account load failed: %s", e)
        return None


@paper_operation(write=True)
def create_account(
    data_dir: Path,
    initial_cash: float,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    name: str | None = None,
    commission_pct: float = DEFAULT_COMMISSION_PCT,
    stamp_tax_pct: float = DEFAULT_STAMP_TAX_PCT,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    queue_limit_orders: bool = False,
    corporate_action_mode: str = "events",
) -> dict:
    """创建账户 (同 id 已存在则原样返回, 不覆盖 — 幂等)。"""
    validate_account_id(account_id)
    with paper_lock(data_dir, PAPER_LOCK):
        existing = get_account(data_dir, account_id)
        if existing is not None:
            return existing
        if corporate_action_mode not in {"events", "legacy_factor"}:
            raise ValueError("公司行为账务模式无效")
        if isinstance(initial_cash, bool) or not isinstance(initial_cash, (int, float)) or not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("初始资金必须是正数")
        for key, value, maximum in (
            ("commission_pct", commission_pct, 0.01),
            ("stamp_tax_pct", stamp_tax_pct, 0.05), ("slippage_bps", slippage_bps, 200),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f"{key} 超出合理范围 (0~{maximum})")
        acc = {
            "id": account_id,
            "name": (name or "").strip() or account_id,
            "initial_cash": float(initial_cash),
            "cash": float(initial_cash),
            "commission_pct": float(commission_pct),
            "stamp_tax_pct": float(stamp_tax_pct),
            "slippage_bps": float(slippage_bps),
            "queue_limit_orders": bool(queue_limit_orders),
            "corporate_action_mode": corporate_action_mode,
            "stamp_tax_mode": "fixed",
            "status": "active",  # active / frozen
            "created_at": _now_iso(),
        }
        save_account(data_dir, acc, account_id)
        return acc


@paper_operation(write=True)
def save_account(data_dir: Path, acc: dict, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        data_dir.save_account(account_id, acc)
        return
    atomic_write_text(
        _root(data_dir, account_id) / "account.json",
        json.dumps(acc, ensure_ascii=False, indent=2),
    )


@paper_operation()
def list_account_ids(data_dir: Path) -> list[str]:
    """全部账户 id (按创建时间; 供钩子/结算遍历)。"""
    if isinstance(data_dir, PaperSession):
        return data_dir.account_ids()
    base = accounts_base(data_dir)
    if not base.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in base.glob("*/account.json"):
        try:
            acc = json.loads(p.read_text(encoding="utf-8"))
            out.append((acc.get("created_at", ""), acc.get("id", p.parent.name)))
        except Exception:
            continue
    return [aid for _, aid in sorted(out)]


@paper_operation()
def list_accounts(data_dir: Path) -> list[dict]:
    """账户列表 (带最新净值摘要, 供切换器展示)。"""
    out: list[dict] = []
    for aid in list_account_ids(data_dir):
        acc = get_account(data_dir, aid)
        if acc is None:
            continue
        nav = load_nav(data_dir, aid)
        out.append({
            "id": acc["id"],
            "name": acc.get("name", acc["id"]),
            "status": acc.get("status", "active"),
            "initial_cash": acc.get("initial_cash"),
            "cash": acc.get("cash"),
            "latest_nav": nav[-1]["nav"] if nav else acc.get("cash"),
            "created_at": acc.get("created_at"),
        })
    return out


@paper_operation(write=True)
def update_settings(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID, **fields) -> dict:
    """更新账户设置 (涨跌停排队 / 费用三参数); 未知字段忽略。返回更新后账户。

    费用只影响之后的新成交 (与回测费用模型同口径), 已有台账不重算。
    """
    with paper_lock(data_dir, PAPER_LOCK):
        acc = get_account(data_dir, account_id)
        if acc is None:
            raise ValueError("尚未创建模拟账户")
        if "queue_limit_orders" in fields and fields["queue_limit_orders"] is not None:
            acc["queue_limit_orders"] = bool(fields["queue_limit_orders"])
        for key, lo, hi in (
            ("commission_pct", 0.0, 0.01),    # 佣金率 ≤1% (100‱)
            ("stamp_tax_pct", 0.0, 0.05),     # 印花税 ≤5% (仅卖出)
            ("slippage_bps", 0.0, 200.0),     # 滑点 ≤200bps
        ):
            v = fields.get(key)
            if v is None:
                continue
            v = float(v)
            if not (lo <= v <= hi):
                raise ValueError(f"{key} 超出合理范围 ({lo}~{hi})")
            acc[key] = v
        save_account(data_dir, acc, account_id)
        return acc


@paper_operation(write=True)
def set_frozen(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID, *, frozen: bool = True) -> dict:
    with paper_lock(data_dir, PAPER_LOCK):
        acc = get_account(data_dir, account_id)
        if acc is None:
            raise ValueError("尚未创建模拟账户")
        acc["status"] = "frozen" if frozen else "active"
        save_account(data_dir, acc, account_id)
        return acc


# ── 费用 (纯函数) ───────────────────────────────────────
# ── 涨跌停 (纯函数, 金融口径须测试) ─────────────────────
def limit_pct(
    symbol: str,
    asset_type: str,
    *,
    trade_date: _date | None = None,
    is_risk_warning: bool = False,
) -> float:
    """Return the applicable limit, including date-aware stock ST rules."""
    if asset_type == "etf":
        return 0.10
    return stock_price_limit_pct(
        symbol,
        trade_date or cn_today(),
        is_risk_warning=is_risk_warning,
    )


def limit_prices(
    prev_close: float,
    symbol: str,
    asset_type: str,
    *,
    trade_date: _date | None = None,
    is_risk_warning: bool = False,
) -> tuple[float, float]:
    """(涨停价, 跌停价)。股票 2 位小数, 基金 3 位小数。"""
    pct = limit_pct(
        symbol,
        asset_type,
        trade_date=trade_date,
        is_risk_warning=is_risk_warning,
    )
    digits = 3 if asset_type == "etf" else 2
    return round(prev_close * (1 + pct), digits), round(prev_close * (1 - pct), digits)


# ── 数量 (纯函数) ───────────────────────────────────────
def normalize_qty(qty: int) -> int:
    """下取整到百股整数倍。"""
    return int(qty // LOT_SIZE) * LOT_SIZE


def qty_from_amount(amount: float, ref_price: float) -> int:
    """按金额估算数量: 金额 / 参考价 向下取整到百股 (未计费用, 手续费从现金扣)。"""
    if ref_price <= 0:
        return 0
    return normalize_qty(int(amount / ref_price))


@paper_operation()
def portfolio_budget(data_dir, account_id=DEFAULT_ACCOUNT_ID, *, prices=None, exclude_order=None):
    """Mark held shares and reserve pending buys, including slip and fees.

    Pending sales never release exposure before a fill. Unknown pending buy
    prices consume all remaining cash, so automated allocation cannot overbook.
    """
    acc = get_account(data_dir, account_id)
    prices = prices or {}
    holdings, pending = {}, {}
    positions = load_positions(data_dir, account_id)
    fallback = {}
    for asset in ("stock", "etf"):
        missing = [symbol for symbol, pos in positions.items() if pos["qty"] > 0
                   and pos.get("asset_type", "stock") == asset and not valid_price(prices.get(symbol))]
        base = _daily_path(data_dir, asset)
        if missing and base.exists():
            try:
                rows = (pl.scan_parquet((base / "**" / "*.parquet").as_posix())
                        .filter(pl.col("symbol").is_in(missing) & (pl.col("date") <= cn_today()))
                        .group_by("symbol").agg(pl.col("close").sort_by("date").last())
                        .collect())
                fallback.update({row["symbol"]: row["close"] for row in rows.iter_rows(named=True)})
            except Exception as exc:
                logger.warning("paper portfolio marks unavailable: %s", exc)
    for symbol, pos in positions.items():
        if pos["qty"] <= 0:
            continue
        price = prices.get(symbol)
        if not valid_price(price):
            price = fallback.get(symbol)
        if not valid_price(price):
            price = pos["avg_cost"]
        holdings[symbol] = pos["qty"] * float(price)
    from app.strategy.paper_actions import receivable_assets

    cash_receivable, pending_shares = receivable_assets(
        load_fills(data_dir, account_id), cn_today().isoformat(), {**fallback, **prices},
    )
    for symbol, value in pending_shares.items():
        holdings[symbol] = holdings.get(symbol, 0) + value
    reserved = 0.0
    unknown = False
    for order in load_orders(data_dir, account_id):
        if order["status"] != "pending" or order["side"] != "buy" or order["id"] == exclude_order:
            continue
        price = order.get("ref_price")
        if not valid_price(price):
            unknown = True
            continue
        mark = prices.get(order["symbol"])
        if valid_price(mark):
            price = max(float(price), float(mark))
        execution = apply_slippage(float(price), "buy", acc["slippage_bps"])
        value = order["qty"] * execution
        pending[order["symbol"]] = pending.get(order["symbol"], 0.0) + value
        reserved += value + buy_fee(order["qty"], execution, acc["commission_pct"])
    equity = float(acc["cash"]) + sum(holdings.values()) + cash_receivable
    return {"equity": equity, "holdings": holdings, "pending": pending,
            "exposure": sum(holdings.values()) + sum(pending.values()),
            "available_cash": 0.0 if unknown else max(0.0, float(acc["cash"]) - reserved),
            "unknown_pending": unknown}


def exposure_error(budget, symbol, value, limits):
    if budget["unknown_pending"]:
        return "待成交买单缺少参考价, 无法核定仓位预算"
    for key, used in (
        ("max_symbol_exposure_pct", budget["holdings"].get(symbol, 0) + budget["pending"].get(symbol, 0)),
        ("max_total_exposure_pct", budget["exposure"]),
    ):
        cap = limits.get(key, 0)
        if cap and used + value > budget["equity"] * cap / 100 + 1e-6:
            return f"{key} 仓位上限不足"
    return None


# ── 订单 ────────────────────────────────────────────────
@paper_operation()
def load_orders(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        return data_dir.orders(account_id)
    out: list[dict] = []
    for f in sorted(_orders_dir(data_dir, account_id).glob("order_*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("paper order load failed %s: %s", f.name, e)
    return out


@paper_operation()
def load_orders_page(
    data_dir: Path,
    account_id: str = DEFAULT_ACCOUNT_ID,
    *,
    status: str | None = None,
    limit: int = 100,
    before: tuple[str, str] | None = None,
) -> tuple[list[dict], int, tuple[str, str] | None]:
    """Newest-first HTTP history page; domain replay keeps using ``load_orders``."""
    validate_account_id(account_id)
    if not 1 <= limit <= 500:
        raise ValueError("订单分页大小无效")
    if isinstance(data_dir, PaperSession):
        return data_dir.order_page(account_id, status=status, limit=limit, before=before)
    orders = load_orders(data_dir, account_id)
    if status is not None:
        orders = [order for order in orders if order.get("status") == status]
    total = len(orders)
    orders.sort(key=_order_sort_key, reverse=True)
    if before is not None:
        orders = [order for order in orders if _order_sort_key(order) < before]
    rows = orders[:limit + 1]
    page = rows[:limit]
    next_key = _order_sort_key(page[-1]) if len(rows) > limit else None
    return page, total, next_key


@paper_operation(write=True)
def save_order(data_dir: Path, order: dict, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        data_dir.save_order(account_id, order)
        return
    p = _orders_dir(data_dir, account_id) / f"{order['id']}.json"
    atomic_write_text(p, json.dumps(order, ensure_ascii=False, indent=2))


@paper_operation()
def get_order(data_dir: Path, order_id: str, account_id: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        return data_dir.order(account_id, order_id)
    p = _orders_dir(data_dir, account_id) / f"{order_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


@paper_operation(write=True)
def create_order(
    data_dir: Path,
    symbol: str,
    side: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    qty: int | None = None,
    amount: float | None = None,
    order_type: str = "market",
    asset_type: str | None = None,
    ref_price: float | None = None,
    source: str = "manual",
    idempotency_key: str | None = None,
    exit_intent_id: str | None = None,
    entry_policy: dict | None = None,
    risk_limits: dict | None = None,
    valuation_prices: dict | None = None,
) -> tuple[dict | None, str | None]:
    """创建订单并校验。qty 与 amount 二选一 (amount 按参考价折算百股)。

    order_type: market(即时, 盘中钩子撮合) / next_open / close。
    ETF 即时单自动转 next_open (盘中钩子只喂股票快照)。
    返回 (order, None) 或 (None, 错误信息)。
    """
    with paper_lock(data_dir, PAPER_LOCK):
        digest = None
        if isinstance(data_dir, PaperSession):
            previous, digest = data_dir.idempotent_order(account_id, idempotency_key, {
                "symbol": symbol, "side": side, "qty": qty, "amount": amount, "order_type": order_type,
                "asset_type": asset_type, "ref_price": ref_price, "source": source,
                **({"exit_intent_id": exit_intent_id} if exit_intent_id is not None else {}),
                **({"entry_policy": entry_policy} if entry_policy is not None else {}),
                **({"risk_limits": risk_limits} if risk_limits is not None else {}),
            })
            if previous:
                return previous, None
        acc = get_account(data_dir, account_id)
        if acc is None:
            raise ValueError("尚未创建模拟账户")
        if acc.get("status") != "active":
            return None, "账户已冻结, 拒绝新订单"
        if side not in ("buy", "sell"):
            return None, f"side 非法: {side!r}"
        if order_type not in ("market", "next_open", "close"):
            return None, f"order_type 非法: {order_type!r}"
        symbol = (symbol or "").strip()
        if not symbol:
            return None, "symbol 不能为空"
        from app.market_scope import removed_market_symbol
        if removed_market_symbol(symbol):
            return None, "模拟交易仅支持沪深市场"
        if asset_type is None:
            asset_type = "etf" if symbol.endswith((".SH", ".SZ")) and symbol.split(".")[0].startswith(("51", "56", "58", "15")) else "stock"

        # ETF 即时单 → 次日开盘 (盘中钩子只喂股票快照)
        if order_type == "market" and asset_type == "etf":
            order_type = "next_open"

        if qty is None and amount is None:
            return None, "qty 与 amount 必须提供一个"
        if qty is None:
            if ref_price is None or ref_price <= 0:
                return None, "amount 模式需要参考价 ref_price"
            qty = qty_from_amount(float(amount), ref_price)
            if qty <= 0:
                return None, f"金额不足以买一手 (参考价 {ref_price})"
        qty = int(qty)
        if qty <= 0 or (side == "buy" and qty % LOT_SIZE != 0):
            return None, f"数量必须是 {LOT_SIZE} 的整数倍"

        if side == "buy":
            # 买入资金预检 (按参考价上界估算, 撮合时二次校验)
            est_price = float(ref_price) if ref_price else 0.0
            budget = portfolio_budget(data_dir, account_id, prices={**(valuation_prices or {}), symbol: est_price})
            execution = apply_slippage(est_price, "buy", acc["slippage_bps"])
            if est_price > 0 and qty * execution + buy_fee(qty, execution, acc["commission_pct"]) > budget["available_cash"]:
                return None, f"可用资金不足 (需约 {qty * est_price:.0f}, 扣待成交买单后可用 {budget['available_cash']:.0f})"
            if risk_limits:
                projected = {**budget, "equity": budget["equity"] - buy_fee(qty, execution, acc["commission_pct"])}
                error = exposure_error(projected, symbol, qty * execution, risk_limits)
                if error:
                    return None, error
        else:
            pos = load_positions(data_dir, account_id).get(symbol)
            if pos is None or pos["qty"] <= 0:
                return None, f"无 {symbol} 持仓, 不能卖出"
            # 可卖数量按当前交易日现算 (与 _fill_order / overview 同口径): 物化文件里的
            # available_qty 是上次重建时的 T+1 口径, 跨日后不会更新, 次日仍会是 0
            available = _available_of(pos, cn_today().isoformat())
            if qty % LOT_SIZE and qty != pos["qty"]:
                return None, "零股只能随全部持仓一次卖出"
            if qty > available:
                return None, f"可卖数量不足 (T+1): 可卖 {available}, 请求数量 {qty}"
            # 超卖防护: pending 卖出单占用可卖额度 — 同 symbol 的 pending 卖出合计
            # 不得超过可卖数量, 否则多张单各自通过校验、成交时逐张扣减会超额
            pending_sell = sum(
                int(o["qty"]) for o in load_orders(data_dir, account_id)
                if o["status"] == "pending" and o["side"] == "sell" and o["symbol"] == symbol
            )
            if pending_sell + qty > available:
                return None, (
                    f"可卖数量不足 (T+1): 可卖 {available}, "
                    f"已有待成交卖出 {pending_sell}, 请求数量 {qty}"
                )

        # 持仓标的数上限 (仅新开仓的买入; 加仓已有持仓不受限)
        positions = load_positions(data_dir, account_id)
        used = {sym for sym, pos in positions.items() if pos["qty"] > 0}
        used.update(o["symbol"] for o in load_orders(data_dir, account_id)
                    if o["side"] == "buy" and o["status"] == "pending")
        if side == "buy" and symbol not in used and len(used) >= MAX_POSITION_SYMBOLS:
            return None, f"持仓标的数已达上限 {MAX_POSITION_SYMBOLS}, 不能再开新仓"

        order = {
            "id": _new_id("order"),
            "symbol": symbol,
            "asset_type": asset_type,
            "side": side,
            "qty": qty,
            "order_type": order_type,
            "status": "pending",   # pending / filled / cancelled / expired
            "ref_price": float(ref_price) if ref_price else None,
            "postponed": 0,        # 顺延交易日计数 (停牌顺延 / 涨跌停排队共用)
            "source": source,      # manual / auto:{rule_id}
            "created_at": _now_iso(),
            "filled_at": None,
            "fill_price": None,
            "fees": None,
            "reason": None,
        }
        if exit_intent_id is not None:
            order["exit_intent_id"] = exit_intent_id
        if side == "buy" and entry_policy is not None:
            RiskPolicy.from_mapping(entry_policy.get("policy", {}))
            order["entry_policy"] = json.loads(json.dumps(entry_policy, allow_nan=False))
        if side == "buy" and risk_limits:
            order["risk_limits"] = dict(risk_limits)
        save_order(data_dir, order, account_id)
        if isinstance(data_dir, PaperSession):
            data_dir.bind_order_request(account_id, order["id"], idempotency_key, digest)
        return order, None


@paper_operation(write=True)
def cancel_order(data_dir: Path, order_id: str, account_id: str = DEFAULT_ACCOUNT_ID) -> tuple[dict | None, str | None]:
    """撤销 pending 订单; 已成交/已过期不可撤。"""
    with paper_lock(data_dir, PAPER_LOCK):
        order = get_order(data_dir, order_id, account_id)
        if order is None:
            return None, f"订单不存在: {order_id}"
        if order["status"] != "pending":
            return None, f"订单状态为 {order['status']}, 不可撤销"
        order["status"] = "cancelled"
        order["reason"] = "manual cancel"
        save_order(data_dir, order, account_id)
        return order, None


# ── 成交台账与持仓 ──────────────────────────────────────
def _fills_path(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> Path:
    return _root(data_dir, account_id) / "fills.jsonl"


@paper_operation()
def load_fills(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        return data_dir.fills(account_id)
    p = _fills_path(data_dir, account_id)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception as e:
            logger.warning("paper fill line skipped: %s", e)
    return out


@paper_operation()
def load_fills_page(
    data_dir: Path,
    account_id: str = DEFAULT_ACCOUNT_ID,
    *,
    limit: int = 100,
    before: int | None = None,
) -> tuple[list[dict], int, int, int | None]:
    """Newest-first fill page with stable append ordinals."""
    validate_account_id(account_id)
    if not 1 <= limit <= 500:
        raise ValueError("成交分页大小无效")
    if isinstance(data_dir, PaperSession):
        return data_dir.fill_page(account_id, limit=limit, before=before)
    fills = load_fills(data_dir, account_id)
    total = len(fills)
    execution_total = sum(fill.get("kind", "fill") == "fill" for fill in fills)
    rows = list(enumerate(fills, start=1))
    if before is not None:
        rows = [row for row in rows if row[0] < before]
    rows.sort(key=lambda row: row[0], reverse=True)
    selected = rows[:limit + 1]
    page = selected[:limit]
    next_key = page[-1][0] if len(selected) > limit else None
    return [fill for _, fill in page], total, execution_total, next_key


@paper_operation()
def load_positions(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, dict]:
    """读物化持仓 (无文件时从台账重建)。"""
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        positions = data_dir.positions(account_id)
        return positions if positions is not None else replay_positions(data_dir, account_id=account_id)[0]
    p = _root(data_dir, account_id) / "positions.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("positions", {})
        except Exception as e:
            logger.warning("paper positions load failed, rebuilding: %s", e)
    return replay_positions(data_dir, account_id=account_id)[0]


@paper_operation()
def replay_positions(
    data_dir: Path,
    fills: list[dict] | None = None,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> tuple[dict[str, dict], float]:
    """由台账重放推导 (持仓, 现金)。纯函数 — 重建与校验的唯一权威实现。

    持仓结构: {symbol: {asset_type, qty, avg_cost, available_qty,
                        lots: [{date: 'YYYY-MM-DD', qty}]}}
    corp_action 行: 数量乘 factor、成本除以 factor、日期归属保持原 buy_date。
    """
    acc = get_account(data_dir, account_id)
    cash = float(acc["initial_cash"]) if acc else 0.0
    positions: dict[str, dict] = {}
    for f in fills if fills is not None else load_fills(data_dir, account_id):
        kind = f.get("kind", "fill")
        symbol = f["symbol"]
        if kind == "corporate_action":
            from app.strategy.paper_actions import apply_entry

            cash += apply_entry(positions, f)
            continue
        if kind == "corp_action":
            pos = positions.get(symbol)
            if pos is None:
                continue
            factor = float(f["factor"])
            for lot in pos["lots"]:
                lot["qty"] = round(lot["qty"] * factor, 6)
            pos["qty"] = round(pos["qty"] * factor, 6)
            pos["avg_cost"] = round(pos["avg_cost"] / factor, 6) if factor else 0.0
            if pos.get("risk_entry_price") and factor:
                pos["risk_entry_price"] /= factor
                pos["risk_factor"] = pos.get("risk_factor", 1.0) * factor
            pos["available_qty"] = round(sum(lot["qty"] for lot in pos["lots"]), 6)
            continue
        side, qty, price, fee = f["side"], int(f["qty"]), float(f["price"]), float(f.get("fee", 0))
        if side == "buy":
            cash -= qty * price + fee
            pos = positions.setdefault(symbol, {
                "asset_type": f.get("asset_type", "stock"),
                "qty": 0, "avg_cost": 0.0, "available_qty": 0, "lots": [],
            })
            if pos["qty"] <= 0:
                # Stable through partial sales/additions; changes only after
                # flattening and reentry. Rebuilt from the authoritative ledger.
                pos["cycle_id"] = f["order_id"]
                # The first fill owns the immutable policy for this cycle.
                pos["entry_policy"] = f.get("entry_policy")
                pos["entry_date"] = f.get("date")
                pos["risk_entry_price"] = price
                pos["risk_factor"] = 1.0
            total_cost = pos["avg_cost"] * pos["qty"] + qty * price + fee
            pos["qty"] += qty
            pos["avg_cost"] = round(total_cost / pos["qty"], 6) if pos["qty"] else 0.0
            pos["lots"].append({"date": f.get("date", ""), "qty": qty})
        else:
            cash += qty * price - fee
            pos = positions.get(symbol)
            if pos is None:
                logger.warning("replay: sell without position %s (台账异常)", symbol)
                pos = positions.setdefault(symbol, {
                    "asset_type": f.get("asset_type", "stock"),
                    "qty": 0, "avg_cost": 0.0, "available_qty": 0, "lots": [],
                })
            remain = qty
            # FIFO 消耗批次; 台账正常时不会透支, 防御性 clamp
            for lot in pos["lots"]:
                if remain <= 0:
                    break
                if lot.get("available_on", "") > f.get("date", ""):
                    continue
                take = min(lot["qty"], remain)
                lot["qty"] -= take
                remain -= take
            pos["lots"] = [lot for lot in pos["lots"] if lot["qty"] > 1e-9]
            pos["qty"] = max(0, pos["qty"] - qty)
            if pos["qty"] == 0:
                pos["avg_cost"] = 0.0
        pos["available_qty"] = _available_of(pos)
    return positions, round(cash, 2)


def _available_of(pos: dict, today: str | None = None) -> int:
    """T+1 可卖数量: buy_date < today 的批次之和。today 为空则全部可卖 (重建口径)。"""
    if today is None:
        return int(sum(lot["qty"] for lot in pos["lots"]))
    return int(sum(lot["qty"] for lot in pos["lots"]
                   if (lot["available_on"] <= today if lot.get("available_on") else lot["date"] < today)))


@paper_operation(write=True)
def _append_fill(data_dir: Path, fill: dict, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    if isinstance(data_dir, PaperSession):
        data_dir.append_fill(account_id, fill)
        return
    p = _fills_path(data_dir, account_id)
    with PAPER_LOCK, p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(fill, ensure_ascii=False) + "\n")


@paper_operation(write=True)
def _materialize(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    """由台账重建 positions.json (每次成交后调用, 保持物化缓存与台账一致)。"""
    positions, _cash = replay_positions(data_dir, account_id=account_id)
    acc = get_account(data_dir, account_id)
    if acc is not None:
        today = cn_today().isoformat()
        for pos in positions.values():
            pos["available_qty"] = _available_of(pos, today)
    if isinstance(data_dir, PaperSession):
        data_dir.save_positions(account_id, positions)
        return
    atomic_write_text(
        _root(data_dir, account_id) / "positions.json",
        json.dumps({"updated_at": _now_iso(), "positions": positions}, ensure_ascii=False, indent=2),
    )


@paper_operation(write=True)
def rebuild_positions(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, dict]:
    """修复兜底: 强制由台账重建物化持仓。"""
    with paper_lock(data_dir, PAPER_LOCK):
        positions, cash = replay_positions(data_dir, account_id=account_id)
        acc = get_account(data_dir, account_id)
        if acc is not None:
            acc["cash"] = cash
            save_account(data_dir, acc, account_id)
        today = cn_today().isoformat()
        for pos in positions.values():
            pos["available_qty"] = _available_of(pos, today)
        if isinstance(data_dir, PaperSession):
            data_dir.save_positions(account_id, positions)
            return positions
        atomic_write_text(
            _root(data_dir, account_id) / "positions.json",
            json.dumps({"updated_at": _now_iso(), "positions": positions}, ensure_ascii=False, indent=2),
        )
        return positions


# ── 撮合 ────────────────────────────────────────────────
def _limit_price_error(
    data_dir: Path, symbol: str, side: str, asset_type: str, price: float, day: str,
) -> str | None:
    """成交价(已含滑点)的涨跌停约束; 撮合与退出重试共用同一口径。"""
    prev = _prev_close(data_dir, symbol, asset_type, day)
    if prev is None:
        return None
    from app.services.corporate_actions import load_actions

    actions = [action for action in load_actions(market_path(data_dir), {symbol})
               if action.ex_date.isoformat() == day and not action.missing_evidence()]
    if actions:
        dividend = sum(action.cash_per_share for action in actions)
        bonus = sum(action.bonus_per_share for action in actions)
        rights = sum(action.rights_per_share for action in actions)
        payment = sum(action.rights_per_share * (action.rights_price or 0) for action in actions)
        prev = round((prev - dividend + payment) / (1 + bonus + rights), 3 if asset_type == "etf" else 2)
    trade_date = _date.fromisoformat(day)
    up, down = limit_prices(
        prev, symbol, asset_type, trade_date=trade_date,
        is_risk_warning=_risk_warning_on(data_dir, symbol, trade_date),
    )
    if side == "buy" and price >= up:
        return f"触及涨停 {up} 买不进 (模拟)"
    if side == "sell" and price <= down:
        return f"触及跌停 {down} 卖不出 (模拟)"
    return None


@paper_operation(write=True)
def _fill_order(data_dir: Path, order: dict, raw_price: float, day: str, account_id: str = DEFAULT_ACCOUNT_ID,
                *, valuation_prices=None) -> dict | None:
    """按指定 raw 价成交一笔订单 (费用/滑点/资金与持仓校验), 返回 fill 或 None。

    调用方持锁。涨跌停按账户 queue_limit_orders 开关: 关 → expired 留痕;
    开 → 转 next_open 排队次日重试 (计顺延, 超限过期)。资金/可卖不足直接过期。
    """
    if isinstance(data_dir, PaperSession):
        current = get_order(data_dir, order["id"], account_id)
        if current is None or current["status"] != "pending":
            return None
        order.update(current)
    if not math.isfinite(raw_price) or raw_price <= 0:
        return None
    acc = get_account(data_dir, account_id)
    if acc["status"] != "active":
        return None
    symbol, side, qty = order["symbol"], order["side"], int(order["qty"])
    asset_type = order.get("asset_type", "stock")
    from app.strategy import paper_actions

    if paper_actions.event_mode(acc):
        report = paper_actions.advance(data_dir, day, account_id)
        blocked = [row for row in report["blocked"] if row["symbol"] == symbol]
        known = any(row["symbol"] == symbol and row["ex_date"] == day and not row.get("reason")
                    for row in report["events"])
        if blocked or (_factor_on(data_dir, symbol, asset_type, day) not in (None, 1.0) and not known):
            order["reason"] = "公司行为日期或历史账务待核对, 暂停撮合"
            save_order(data_dir, order, account_id)
            return None
        acc = get_account(data_dir, account_id)
    price = apply_slippage(raw_price, side, float(acc["slippage_bps"]))

    # 涨跌停检查 (基于上一交易日 raw close; 无上日收盘则跳过检查 — 新股/数据缺失)
    limit_error = _limit_price_error(data_dir, symbol, side, asset_type, price, day)
    if limit_error:
        _queue_or_expire(data_dir, order, acc, limit_error, account_id)
        return None

    fee = buy_fee(qty, price, acc["commission_pct"]) if side == "buy" else sell_fee(qty, price, acc["commission_pct"], acc["stamp_tax_pct"])
    gross = qty * price
    if side == "buy":
        if order.get("risk_limits"):
            budget = portfolio_budget(data_dir, account_id, prices={**(valuation_prices or {}), symbol: raw_price},
                                      exclude_order=order["id"])
            budget["equity"] -= fee
            error = exposure_error(budget, symbol, gross, order["risk_limits"])
            if error:
                _expire(data_dir, order, error, account_id)
                return None
        if gross + fee > float(acc["cash"]) + 1e-6:
            _expire(data_dir, order, f"资金不足 (需 {gross + fee:.2f}, 可用 {acc['cash']:.2f})", account_id)
            return None
        # 资金占用兜底: 只计入 created_at 早于本单的其他 pending 买入 (先到先得,
        # 早单优先成交, 晚单不占早单的额度) — 防止多张 pending 买入各自通过预检、
        # 成交时逐张扣现金而穿透。
        earlier_cash = sum(
            int(o["qty"]) * apply_slippage(float(o["ref_price"] or 0), "buy", acc["slippage_bps"])
            + buy_fee(int(o["qty"]), apply_slippage(float(o["ref_price"] or 0), "buy", acc["slippage_bps"]), acc["commission_pct"])
            for o in load_orders(data_dir, account_id)
            if o["status"] == "pending" and o["side"] == "buy"
            and o["id"] != order["id"]
            and _order_sort_key(o) < _order_sort_key(order)
        )
        if earlier_cash and gross + fee + earlier_cash > float(acc["cash"]) + 1e-6:
            _expire(data_dir, order, f"资金被更早的待成交买入单占用 (约 {earlier_cash:.0f}, 可用 {acc['cash']:.0f})", account_id)
            return None
    if side == "sell":
        pos = load_positions(data_dir, account_id).get(symbol)
        avail = _available_of(pos, day) if pos else 0
        if pos is None or pos["qty"] < qty or avail < qty:
            _expire(data_dir, order, f"可卖数量不足 (T+1): 可卖 {avail}", account_id)
            return None
        # 超卖防护 (撮合侧兜底): 只计入 created_at 早于本单的其他 pending 卖出 —
        # 先到先得, 早单优先成交; 剩余额度不足则本单拒 (而非成交出负持仓)。
        pending_sell = sum(
            int(o["qty"]) for o in load_orders(data_dir, account_id)
            if o["status"] == "pending" and o["side"] == "sell" and o["symbol"] == symbol
            and o["id"] != order["id"]
            and _order_sort_key(o) < _order_sort_key(order)
        )
        if pending_sell + qty > avail:
            _expire(data_dir, order, f"可卖数量被更早的待成交卖出单占用: 可卖 {avail}, 先到 {pending_sell}", account_id)
            return None

    fill = {
        "seq": int(datetime.now().timestamp() * 1000),
        "ts": _now_iso(),
        "date": day,
        "order_id": order["id"],
        "symbol": symbol,
        "asset_type": asset_type,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
        "kind": "fill",
    }
    if side == "buy" and order.get("entry_policy") is not None:
        fill["entry_policy"] = order["entry_policy"]
    _append_fill(data_dir, fill, account_id)

    order["status"] = "filled"
    order["filled_at"] = fill["ts"]
    order["fill_price"] = price
    order["fees"] = fee
    save_order(data_dir, order, account_id)

    # 现金按台账重放结果定版 (重放是权威, 避免双写不一致)
    _cash = replay_positions(data_dir, account_id=account_id)[1]
    acc["cash"] = _cash
    save_account(data_dir, acc, account_id)
    _materialize(data_dir, account_id)

    if isinstance(data_dir, PaperSession):
        data_dir.alert({**_fill_event(fill, account_id), "id": f"fill:{account_id}:{order['id']}"})
        return fill

    # 成交落告警记录 (监控中心触发历史可见; source=paper 走前端通用渲染)
    try:
        label = "模拟盘" if account_id == DEFAULT_ACCOUNT_ID else f"模拟盘[{acc.get('name') or account_id}]"
        from app.services import alert_store
        alert_store.append_many(data_dir, [{
            "ts": int(datetime.now().timestamp() * 1000),
            "source": "paper",
            "type": "paper_fill",
            "rule_id": None,
            "rule_name": "",
            "strategy_id": None,
            "symbol": symbol,
            "name": "",
            "message": f"{label}{'买入' if side == 'buy' else '卖出'} {symbol} {qty}股 @ {price:.3f} (费 {fee:.2f})",
            "price": price,
            "change_pct": 0,
            "signals": [],
            "severity": "info",
            "conditions": [],
            "logic": "and",
        }])
    except Exception as e:
        logger.warning("paper 成交告警落盘失败 (不影响账务): %s", e)

    logger.info("paper filled: %s %s %d x %.3f fee %.2f", side, symbol, qty, price, fee)
    return fill


def _risk_warning_on(data_dir: Path, symbol: str, day: _date) -> bool:
    data_dir = market_path(data_dir)
    if day <= cn_today():
        status = instrument_status_on(data_dir, symbol, day)
        if status is not None:
            return bool(status["is_risk_warning"])
    if day != cn_today():
        return False
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return False
    try:
        row = (
            pl.scan_parquet(path)
            .filter(pl.col("symbol") == symbol)
            .select("name")
            .collect()
        )
    except Exception as exc:
        logger.warning("paper instrument status read failed %s: %s", symbol, exc)
        return False
    if row.is_empty():
        return False
    return is_risk_warning_name(row["name"][0])


def _expire(data_dir: Path, order: dict, reason: str, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    """拒单置 expired 并立即落盘 (撮合拒绝必须留痕, 不允许静默丢弃)。"""
    order["status"] = "expired"
    order["reason"] = reason
    save_order(data_dir, order, account_id)


def _queue_or_expire(data_dir: Path, order: dict, acc: dict, reason: str, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    """涨跌停拒单处理: 排队开启 → 转 next_open 次日重试 (计顺延, 超限过期); 关 → 过期。"""
    postponed = int(order.get("postponed", 0))
    if acc.get("queue_limit_orders"):
        if postponed < MAX_POSTPONE_DAYS:
            order["order_type"] = "next_open"
            order["postponed"] = postponed + 1
            order["reason"] = f"{reason}; 排队次日重试 ({postponed + 1}/{MAX_POSTPONE_DAYS})"
            # 排队时刻: 盘中排队的单不能按当日 (排队之前已打印的) 开盘价成交
            order["queued_at"] = _now_iso()
            save_order(data_dir, order, account_id)
            return
        reason = f"{reason}; 排队 {MAX_POSTPONE_DAYS} 日未成交, 过期"
    _expire(data_dir, order, reason, account_id)


@paper_operation()
def day_fill_events(data_dir: Path, day: str, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    """指定交易日的全部成交 → 推送事件。

    盘后结算路径 (daily_pipeline paper_settle) 用它把结算成交留痕到告警中心;
    盘中即时成交走 evaluate_intraday 返回值, 不经过本函数。
    """
    return [
        _fill_event(f, account_id)
        for f in load_fills(data_dir, account_id)
        if f.get("date") == day and f.get("kind") == "fill"
    ]


def _fill_event(fill: dict, account_id: str) -> dict:
    """成交台账行 → 推送事件。字段对齐监控告警 (AlertEvent), 供 SSE toast /
    语音播报 / 系统通知 / alert_store 留痕 / Webhook 复用, 前端无需新管道。
    ts 用 fill.seq (毫秒), 与 alert_store 的 ts 口径一致; 文案用中文可读措辞。
    """
    side_label = "买入" if fill["side"] == "buy" else "卖出"
    return {
        "source": "paper",
        "type": "fill",
        "severity": "info",
        "ts": int(fill["seq"]),
        "symbol": fill["symbol"],
        "side": fill["side"],
        "qty": fill["qty"],
        "price": fill["price"],
        "account_id": account_id,
        "message": f"模拟盘{side_label}成交 {fill['qty']}股 @ {fill['price']:.2f}",
    }


@paper_operation(write=True)
def evaluate_intraday(
    data_dir: Path, snapshot: dict[str, float], account_id: str = DEFAULT_ACCOUNT_ID,
    *, quote_times: dict[str, float] | None = None,
) -> list[dict]:
    """盘中钩子: 用最新快照价撮合 pending 即时单。返回本次成交事件列表。

    snapshot: {symbol: raw 最新价} (来自 enriched raw_close)。仅处理 market 单;
    调用方 (quote_service) 已保证交易时段。ETF 不在快照内自然顺延。
    事件由调用方广播 (SSE/语音/系统通知/留痕/Webhook), 域模块只产出不投递。
    """
    events: list[dict] = []
    with paper_lock(data_dir, PAPER_LOCK):
        today = cn_today().isoformat()
        from app.strategy import paper_actions

        paper_actions.advance(data_dir, today, account_id)
        pending_orders = sorted(
            (o for o in load_orders(data_dir, account_id) if o["status"] == "pending" and o["order_type"] == "market"),
            key=_order_sort_key,
        )
        for order in pending_orders:
            if quote_times is not None:
                stamp = quote_times.get(order["symbol"])
                try:
                    placed = datetime.fromisoformat(order["created_at"])
                    if placed.tzinfo is None:
                        placed = placed.replace(tzinfo=CN_TZ)
                except (ValueError, TypeError, KeyError):
                    continue
                if stamp is None or not math.isfinite(stamp) or stamp <= placed.timestamp():
                    continue
            price = snapshot.get(order["symbol"])
            if price is None or price <= 0:
                continue  # 无快照顺延
            fill = _fill_order(data_dir, order, float(price), today, account_id, valuation_prices=snapshot)
            if fill is not None:
                events.append(_fill_event(fill, account_id))
    return events


# ── 行情读取 (结算用, 与账户无关) ───────────────────────
@paper_operation(write=True)
def evaluate_opening(data_dir, rows, account_id=DEFAULT_ACCOUNT_ID):
    """Book next_open at the first fresh authoritative raw opening print.

    Old enriched versions lacking raw_open retain daily settlement. Require
    an unambiguous placement/queue time before this open; never backdate an
    order submitted after the price was printed.
    """
    now = cn_now()
    today = now.date().isoformat()
    if (published_trading_day(now.date()) is not True or now.time() < _SESSION_OPEN
            or now.time() > _SESSION_CLOSE or _time(11, 30) < now.time() < _time(13)):
        return []
    events = []
    with paper_lock(data_dir, PAPER_LOCK):
        for order in sorted(load_orders(data_dir, account_id), key=_order_sort_key):
            if order["status"] != "pending" or order["order_type"] != "next_open":
                continue
            row = rows.get(order["symbol"], {})
            raw = row.get("raw_open")
            stamp = row.get("quote_ts") or row.get("timestamp")
            if row.get("quote_time_basis") in ("received", "unknown"):
                continue
            if not valid_price(raw) or str(row.get("date"))[:10] != today:
                continue
            if row.get("volume") is not None and row["volume"] <= 0:
                continue
            if not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
                continue
            stamp = stamp / 1000 if stamp > 1e12 else stamp
            if not -2 <= now.timestamp() - stamp <= 15:
                continue
            opening = datetime.combine(now.date(), _SESSION_OPEN, CN_TZ)
            if stamp < opening.timestamp():
                continue
            try:
                placed = datetime.fromisoformat(order.get("queued_at") or order["created_at"])
                placed = placed.replace(tzinfo=CN_TZ) if placed.tzinfo is None else placed.astimezone(CN_TZ)
            except (ValueError, TypeError, KeyError):
                continue
            if placed >= opening:
                continue
            factor = _factor_on(data_dir, order["symbol"], order.get("asset_type", "stock"), today)
            from app.strategy.paper_actions import event_mode

            if factor not in (None, 1.0) and not event_mode(get_account(data_dir, account_id)):
                continue  # Corporate-action execution is reconciled by daily settlement.
            fill = _fill_order(data_dir, order, float(raw), today, account_id,
                               valuation_prices={sym: values.get("raw_open") for sym, values in rows.items()
                                                 if str(values.get("date"))[:10] == today})
            if fill:
                events.append(_fill_event(fill, account_id))
    return events


def _daily_path(data_dir: Path, asset_type: str) -> Path:
    data_dir = market_path(data_dir)
    sub = "kline_etf_daily" if asset_type == "etf" else "kline_daily"
    return data_dir / sub


def read_daily_bar(data_dir: Path, symbol: str, asset_type: str, day: str) -> dict | None:
    """读某标的某日 raw OHLC (不复权); 无数据返回 None。"""
    base = _daily_path(data_dir, asset_type)
    if not base.exists():
        return None
    try:
        df = (
            scan_daily_parquet((base / "**" / "*.parquet").as_posix())
            .filter((pl.col("symbol") == symbol) & (pl.col("date") == _date.fromisoformat(day)))
            .select(["open", "close"])
            .collect()
        )
    except Exception as e:
        logger.warning("paper read_daily_bar failed %s %s: %s", symbol, day, e)
        return None
    if df.is_empty():
        return None
    return {"open": float(df["open"][0]), "close": float(df["close"][0])}


def _prev_close(data_dir: Path, symbol: str, asset_type: str, day: str) -> float | None:
    """day 之前最近一个有数据的交易日 raw close (严格 < day, 按日期排序取最后)。"""
    base = _daily_path(data_dir, asset_type)
    if not base.exists():
        return None
    try:
        df = (
            scan_daily_parquet((base / "**" / "*.parquet").as_posix())
            .filter((pl.col("symbol") == symbol) & (pl.col("date") < _date.fromisoformat(day)))
            .sort("date")
            .select(pl.col("close").last())
            .collect()
        )
    except Exception as e:
        logger.warning("paper prev_close failed %s: %s", symbol, e)
        return None
    if df.is_empty() or df["close"][0] is None:
        return None
    return float(df["close"][0])


def _index_close(data_dir: Path, day: str) -> float | None:
    """沪深300 当日 raw close (基准对比); 指数日K缺失返回 None。"""
    data_dir = market_path(data_dir)
    base = data_dir / "kline_index_daily"
    if not base.exists():
        return None
    try:
        df = (
            scan_daily_parquet((base / "**" / "*.parquet").as_posix())
            .filter((pl.col("symbol") == "000300.SH") & (pl.col("date") == _date.fromisoformat(day)))
            .select("close")
            .collect()
        )
    except Exception as e:
        logger.warning("paper index close read failed: %s", e)
        return None
    if df.is_empty() or df["close"][0] is None:
        return None
    return float(df["close"][0])


def _factor_on(data_dir: Path, symbol: str, asset_type: str, day: str) -> float | None:
    """symbol 在 day 的除权因子 (无因子文件/无当日事件返回 None)。"""
    data_dir = market_path(data_dir)
    sub = "adj_factor_etf" if asset_type == "etf" else "adj_factor"
    p = data_dir / sub / "all.parquet"
    if not p.exists():
        return None
    try:
        df = (
            pl.scan_parquet(p.as_posix())
            .filter((pl.col("symbol") == symbol) & (pl.col("trade_date") == _date.fromisoformat(day)))
            .select("ex_factor")
            .collect()
        )
    except Exception as e:
        logger.warning("paper factor read failed %s: %s", symbol, e)
        return None
    if df.is_empty() or df["ex_factor"][0] is None:
        return None
    f = float(df["ex_factor"][0])
    return f if f > 0 else None


# ── 盘后结算 ────────────────────────────────────────────
def _placed_after_price_time(order: dict, day: str) -> bool:
    """订单 (盘中排队的按排队时刻) 是否晚于 day 结算价格的打印时刻。

    next_open 用当日 09:30 开盘价, close / market 兜底用当日 15:00 收盘价。下单或
    排队发生在同一交易日该时刻之后, 说明这个价格在下单时已经打印过, 按它成交等于
    拿已知价格回填 (次日开盘单的口径是次一交易日 raw_open), 应留到下一交易日结算。
    更早日期的订单不受影响; 时间戳缺失或无法解析时不拦截 (沿用原行为)。
    """
    stamp = order.get("queued_at") or order.get("created_at")
    try:
        ts = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is not None:
        ts = ts.astimezone(CN_TZ)
    if ts.date().isoformat() != day:
        return False
    price_time = _SESSION_OPEN if order.get("order_type") == "next_open" else _SESSION_CLOSE
    return ts.time() >= price_time


@paper_operation(write=True)
def settle_day(data_dir: Path, day: str, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
    """事件入账 → 撮合顺延单 → 收盘权益登记 → 定版净值。幂等 (重跑同日安全)。

    撮合顺序: close 单用 raw_close; next_open 单用 raw_open; 仍 pending 的
    market 单 (当日无快照) 也按 raw_close 兜底成交 — 避免停牌外无限顺延。
    当日开盘后才下 / 排队的 next_open 单, 以及收盘后才下的 close / market 单,
    当日不成交也不计顺延, 留到下一交易日 (见 _placed_after_price_time)。
    """
    summary = {"filled": 0, "expired": 0, "corp_actions": 0, "nav": None, "account": account_id}
    with paper_lock(data_dir, PAPER_LOCK):
        acc = get_account(data_dir, account_id)
        if acc is None:
            return summary
        from app.strategy import paper_actions

        action_report = paper_actions.advance(data_dir, day, account_id)
        summary["corp_actions"] += action_report.get("posted", 0)
        summary["corporate_action_blocks"] = action_report["blocked"]
        pending_orders = sorted(
            (o for o in load_orders(data_dir, account_id) if o["status"] == "pending"),
            key=_order_sort_key,
        )
        for order in pending_orders:
            if isinstance(data_dir, PaperSession):
                if order.get("last_settled_day", "") >= day:
                    continue
                try:
                    if datetime.fromisoformat(order["created_at"]).date().isoformat() > day:
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
                order["last_settled_day"] = day
            bar = read_daily_bar(data_dir, order["symbol"], order.get("asset_type", "stock"), day)
            if bar is None:
                order["postponed"] = int(order.get("postponed", 0)) + 1
                if order["postponed"] > MAX_POSTPONE_DAYS:
                    order["status"] = "expired"
                    order["reason"] = f"连续 {MAX_POSTPONE_DAYS} 个交易日无行情, 自动过期"
                    save_order(data_dir, order, account_id)
                    summary["expired"] += 1
                else:
                    save_order(data_dir, order, account_id)
                continue
            if _placed_after_price_time(order, day):
                continue  # 下单/排队时该价已打印, 留到下一交易日 (不计顺延)
            if isinstance(data_dir, PaperSession):
                save_order(data_dir, order, account_id)
            # next_open 用开盘价; close 与 market 兜底用收盘价
            raw = bar["open"] if order["order_type"] == "next_open" else bar["close"]
            before = order["status"]
            if _fill_order(data_dir, order, raw, day, account_id) is not None:
                summary["filled"] += 1
            elif before == "pending" and order["status"] == "expired":
                summary["expired"] += 1

        # 除权调整 (仅持仓标的; 幂等: 同一 symbol 同日只应用一次 — 重跑结算不二次乘因子)
        positions = load_positions(data_dir, account_id)
        applied = {
            (f["symbol"], f["date"])
            for f in load_fills(data_dir, account_id)
            if f.get("kind") == "corp_action"
        }
        for symbol, pos in positions.items():
            if paper_actions.event_mode(acc):
                break
            if pos["qty"] <= 0:
                continue
            factor = _factor_on(data_dir, symbol, pos.get("asset_type", "stock"), day)
            if factor is None or factor == 1.0 or (symbol, day) in applied:
                continue
            before_qty, before_cost = pos["qty"], pos["avg_cost"]
            _append_fill(data_dir, {
                "seq": int(datetime.now().timestamp() * 1000),
                "ts": _now_iso(),
                "date": day,
                "order_id": None,
                "symbol": symbol,
                "asset_type": pos.get("asset_type", "stock"),
                "side": "corp_action",
                "kind": "corp_action",
                "factor": factor,
                "qty_before": before_qty,
                "cost_before": before_cost,
            }, account_id)
            summary["corp_actions"] += 1
        if summary["corp_actions"]:
            _materialize(data_dir, account_id)
        closing_actions = paper_actions.advance(data_dir, day, account_id, record_close=True)
        summary["corp_actions"] += closing_actions.get("posted", 0)
        summary["corporate_action_blocks"] = closing_actions["blocked"]

        # Closing observations latch risks without inventing an earlier fill.
        # Fresh realtime execution or the next valid opening advances the exit.
        from app.strategy import paper_auto
        paper_auto.evaluate_position_risk(
            data_dir, latest_prices_from_daily(data_dir, day, account_id),
            account_id, day=day, submit=False,
        )

        # 定版净值 (幂等: 重写当日行); 顺带记录沪深300 收盘作基准对比
        if summary["corporate_action_blocks"]:
            return summary  # Unresolved entitlements cannot produce a certified closing NAV.
        nav = daily_nav(data_dir, day, account_id=account_id)
        if nav is not None:
            benchmark = _index_close(data_dir, day)
            if benchmark is not None:
                nav["benchmark_close"] = benchmark
            _write_nav_line(data_dir, day, nav, account_id)
            summary["nav"] = nav
    return summary


# ── 净值与总览 ──────────────────────────────────────────
@paper_operation()
def latest_prices_from_daily(data_dir: Path, day: str, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, float]:
    """day 收盘后各持仓标的 raw_close (定版用)。"""
    out: dict[str, float] = {}
    for symbol, pos in load_positions(data_dir, account_id).items():
        if pos["qty"] <= 0:
            continue
        bar = read_daily_bar(data_dir, symbol, pos.get("asset_type", "stock"), day)
        if bar is not None:
            out[symbol] = bar["close"]
    return out


@paper_operation()
def daily_nav(
    data_dir: Path,
    day: str,
    price_map: dict[str, float] | None = None,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict | None:
    """某日定版净值 {date, cash, mv, nav}; 缺行情的持仓按成本计 (保守, 留痕调用方)。"""
    acc = get_account(data_dir, account_id)
    if acc is None:
        return None
    positions = load_positions(data_dir, account_id)
    prices = price_map if price_map is not None else latest_prices_from_daily(data_dir, day, account_id)
    missing = [s for s, p in positions.items() if p["qty"] > 0 and s not in prices]
    if missing:
        logger.warning("paper nav %s: 缺行情按成本计: %s", day, missing)
    mv = 0.0
    for symbol, pos in positions.items():
        if pos["qty"] <= 0:
            continue
        price = prices.get(symbol, pos["avg_cost"])
        mv += pos["qty"] * price
    from app.strategy.paper_actions import receivables

    pending = receivables(load_fills(data_dir, account_id), day, prices)
    return {"date": day, "cash": round(float(acc["cash"]), 2), "mv": round(mv, 2),
            "receivables": pending, "nav": round(float(acc["cash"]) + mv + pending, 2)}


@paper_operation(write=True)
def _write_nav_line(data_dir: Path, day: str, nav: dict, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    if isinstance(data_dir, PaperSession):
        data_dir.save_nav(account_id, day, nav)
        return
    p = _nav_dir(data_dir, account_id) / "daily.jsonl"
    lines: list[str] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("date") != day:
                    lines.append(line)
            except Exception:
                continue
    lines.append(json.dumps(nav, ensure_ascii=False))
    lines.sort(key=lambda s: json.loads(s)["date"])
    atomic_write_text(p, "\n".join(lines) + "\n")


@paper_operation()
def load_nav(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    validate_account_id(account_id)
    if isinstance(data_dir, PaperSession):
        return data_dir.nav(account_id)
    p = _nav_dir(data_dir, account_id) / "daily.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


@paper_operation()
def load_nav_page(
    data_dir: Path,
    account_id: str = DEFAULT_ACCOUNT_ID,
    *,
    limit: int = 500,
    before: str | None = None,
) -> tuple[list[dict], int, str | None]:
    """Latest NAV window in chronological order for chart rendering."""
    validate_account_id(account_id)
    if not 1 <= limit <= 1000:
        raise ValueError("净值分页大小无效")
    if isinstance(data_dir, PaperSession):
        return data_dir.nav_page(account_id, limit=limit, before=before)
    rows = load_nav(data_dir, account_id)
    total = len(rows)
    if before is not None:
        rows = [row for row in rows if row.get("date", "") < before]
    rows.sort(key=lambda row: row.get("date", ""), reverse=True)
    selected = rows[:limit + 1]
    page = selected[:limit]
    next_key = page[-1].get("date") if len(selected) > limit else None
    return list(reversed(page)), total, next_key


@paper_operation()
def overview(
    data_dir: Path,
    price_map: dict[str, float] | None = None,
    account_id: str = DEFAULT_ACCOUNT_ID,
    *,
    quote_ts: float | None = None,
    basis: str | None = None,
) -> dict:
    """总览估值: 现金 + 持仓市值 + 估算总资产。

    估值永远是估算口径, 不是收盘定版净值 (后者走 load_nav)。每个持仓标注价源:
      - live        盘中实时快照 (price_map 命中, basis=live)
      - daily_close 最近日线收盘 (price_map 命中, basis=daily_close)
      - cost        无可用行情, 退回持仓成本 (last_price=avg_cost, 市值≈成本, 仅占位)
    返回的 valuation 块给出整体口径、快照时间与覆盖情况, 供前端标注而非与定版混用。
    """
    acc = get_account(data_dir, account_id)
    if acc is None:
        return {"initialized": False}
    positions = load_positions(data_dir, account_id)
    today = cn_today().isoformat()
    # basis 显式优先; 否则有 price_map 视为实时估算, 无则纯成本占位。
    priced_basis = basis if basis in ("live", "daily_close") else ("live" if price_map else "cost")
    holdings = []
    mv = 0.0
    covered = 0
    missing: list[str] = []
    for symbol, pos in sorted(positions.items()):
        if pos["qty"] <= 0:
            continue
        price = (price_map or {}).get(symbol)
        has_quote = price is not None and valid_price(price)
        last = float(price) if has_quote else pos["avg_cost"]
        source = priced_basis if has_quote else "cost"
        if has_quote:
            covered += 1
        else:
            missing.append(symbol)
        mv += pos["qty"] * last
        holdings.append({
            "symbol": symbol,
            "asset_type": pos.get("asset_type", "stock"),
            "qty": pos["qty"],
            "avg_cost": pos["avg_cost"],
            "last_price": round(last, 4),
            "price_source": source,
            "market_value": round(pos["qty"] * last, 2),
            "pnl": round(pos["qty"] * (last - pos["avg_cost"]), 2),
            "pnl_pct": round((last / pos["avg_cost"] - 1) * 100, 2) if pos["avg_cost"] else 0.0,
            "available_qty": _available_of(pos, today),
        })
    total_holdings = len(holdings)
    # 整体口径: 任一持仓缺价退回成本时, 口径保守降级到 cost (不假装全量实时)。
    overall_basis = priced_basis if total_holdings and not missing else "cost"
    from app.strategy.paper_actions import receivables

    pending = receivables(load_fills(data_dir, account_id), today, price_map or {})
    return {
        "initialized": True,
        "account_id": account_id,
        "account_name": acc.get("name", account_id),
        "status": acc["status"],
        "queue_limit_orders": bool(acc.get("queue_limit_orders")),
        "cash": round(float(acc["cash"]), 2),
        "market_value": round(mv, 2),
        "receivables": pending,
        "corporate_action_mode": acc.get("corporate_action_mode", "legacy_factor"),
        "stamp_tax_mode": "fixed",
        "total": round(float(acc["cash"]) + mv + pending, 2),
        "total_pnl": round(float(acc["cash"]) + mv + pending - float(acc["initial_cash"]), 2),
        "initial_cash": float(acc["initial_cash"]),
        "estimating": price_map is not None,
        "valuation": {
            "basis": overall_basis,
            "as_of": today,
            "quote_ts": int(quote_ts) if quote_ts is not None and math.isfinite(quote_ts) else None,
            "covered": covered,
            "total": total_holdings,
            "missing": missing,
        },
        "holdings": holdings,
    }


# ── 回合统计 (FIFO) ─────────────────────────────────────
@paper_operation()
def round_trips(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    """FIFO 配对片段 (开→平): 入场价含买入费, 出场净额扣卖出费。

    每条是一次卖出与一个买入批次的配对, 不是一次完整持仓周期 —— 一次分批建仓/
    分批平仓会产生多条相关片段 (kind="pairing")。cycle_seq 标注该片段所属的持仓周期
    (从零建仓到再次清零为一个周期, 按证券分别计数), 供上层区分片段数与周期数。
    """
    fills = load_fills(data_dir, account_id)
    lots: dict[str, list[dict]] = {}
    held: dict[str, float] = {}      # 当前每标的持仓数量 (判定清零=周期结束)
    cycle_no: dict[str, int] = {}    # 每标的当前持仓周期序号 (从 1 起)
    rounds: list[dict] = []
    for f in fills:
        symbol = f["symbol"]
        if f.get("kind") == "corporate_action":
            if f["stage"] in {"ex", "rights_listed"} and f.get("qty"):
                if held.get(symbol, 0) <= 0:
                    cycle_no[symbol] = cycle_no.get(symbol, 0) + 1
                held[symbol] = held.get(symbol, 0) + f["qty"]
                lots.setdefault(symbol, []).append({
                    "date": f["date"], "qty": f["qty"],
                    "cost": f["cash_amount"] if f["stage"] == "rights_listed" else 0,
                    "available_on": f["available_on"],
                })
            continue
        if f.get("kind") == "corp_action":
            factor = float(f["factor"])
            for lot in lots.get(symbol, []):
                lot["qty"] = round(lot["qty"] * factor, 6)
            if symbol in held:
                held[symbol] = round(held[symbol] * factor, 6)
            continue
        if f["side"] == "buy":
            qty = int(f["qty"])
            # 从清零状态再次建仓 → 新的持仓周期。
            if held.get(symbol, 0) <= 0:
                cycle_no[symbol] = cycle_no.get(symbol, 0) + 1
            held[symbol] = held.get(symbol, 0) + qty
            lots.setdefault(symbol, []).append({
                "date": f["date"], "qty": qty,
                "cost": float(f["price"]) * qty + float(f.get("fee", 0)),
            })
            continue
        remain = int(f["qty"])
        proceeds_unit = float(f["price"]) - float(f.get("fee", 0)) / max(int(f["qty"]), 1)
        pool = lots.get(symbol, [])
        for lot in list(pool):
            if remain <= 0:
                break
            if lot.get("available_on", "") > f["date"]:
                continue
            take = min(lot["qty"], remain)
            cost_part = lot["cost"] * take / lot["qty"]
            pnl = proceeds_unit * take - cost_part
            rounds.append({
                "kind": "pairing",
                "symbol": symbol,
                "cycle_seq": cycle_no.get(symbol, 1),
                "open_date": lot["date"],
                "close_date": f["date"],
                "qty": take,
                "cost_basis": round(cost_part, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / cost_part * 100, 2) if cost_part else 0.0,
                "holding_days": _days_between(lot["date"], f["date"]),
            })
            lot["qty"] -= take
            lot["cost"] -= cost_part
            if lot["qty"] <= 0:
                pool.remove(lot)
            remain -= take
        held[symbol] = round(held.get(symbol, 0) - int(f["qty"]), 6)
    return rounds


@paper_operation()
def position_cycles(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    """把配对片段按 (证券, 持仓周期) 聚合成完整持仓周期 (从零建仓到清零)。

    一个周期的收益是其所含片段 pnl 之和, 收益率按周期总成本的资金加权 (非片段
    pnl_pct 的简单平均), 持有天数取周期首个开仓日到末个平仓日的自然日跨度。
    仍有未平仓余量的周期不在此列 (尚未闭合, 无最终收益)。
    """
    pairings = round_trips(data_dir, account_id)
    grouped: dict[tuple[str, int], list[dict]] = {}
    for p in pairings:
        grouped.setdefault((p["symbol"], p.get("cycle_seq", 1)), []).append(p)
    cycles: list[dict] = []
    for (symbol, seq), parts in grouped.items():
        pnl = sum(p["pnl"] for p in parts)
        cost = sum(p.get("cost_basis", 0.0) for p in parts)
        open_date = min(p["open_date"] for p in parts)
        close_date = max(p["close_date"] for p in parts)
        cycles.append({
            "symbol": symbol,
            "cycle_seq": seq,
            "open_date": open_date,
            "close_date": close_date,
            "pairings": len(parts),
            "qty": sum(p["qty"] for p in parts),
            "cost_basis": round(cost, 2),
            "pnl": round(pnl, 2),
            # 资金加权: 周期总收益 / 周期总成本, 不是各片段收益率的算术平均。
            "pnl_pct": round(pnl / cost * 100, 2) if cost else 0.0,
            "holding_days": _days_between(open_date, close_date),
        })
    cycles.sort(key=lambda c: (c["close_date"], c["symbol"], c["cycle_seq"]))
    return cycles


def max_drawdown(nav_values: list[float]) -> float | None:
    """定版净值序列的最大回撤 (0~1 小数; 空序列返回 None, 单点为 0)。"""
    peak: float | None = None
    mdd: float | None = None
    for v in nav_values:
        if v <= 0:
            continue
        peak = v if peak is None else max(peak, v)
        dd = (peak - v) / peak
        mdd = dd if mdd is None else max(mdd, dd)
    return mdd


@paper_operation()
def stats(data_dir: Path, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
    """回合汇总。

    口径明确区分两层:
      - 配对片段 (rounds/win_rate/profit_loss_ratio/avg_holding_days): FIFO 卖出与买入
        批次的配对, 胜率与盈亏比按片段计, 一次分批平仓会拆成多条。沿用历史字段名。
      - 持仓周期 (cycles.*): 从零建仓到清零的完整周期, 收益率资金加权, 胜率按周期计。
    已实现盈亏对两层相等 (同一批成交), 最大回撤取定版净值序列。
    """
    rounds = round_trips(data_dir, account_id)
    wins = [r for r in rounds if r["pnl"] > 0]
    losses = [r for r in rounds if r["pnl"] <= 0]
    avg_win = sum(r["pnl"] for r in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(r["pnl"] for r in losses) / len(losses)) if losses else 0.0
    mdd = max_drawdown([n["nav"] for n in load_nav(data_dir, account_id)])

    cycles = position_cycles(data_dir, account_id)
    cycle_wins = [c for c in cycles if c["pnl"] > 0]
    cycle_cost = sum(c["cost_basis"] for c in cycles)
    cycle_pnl = sum(c["pnl"] for c in cycles)
    corporate_income = sum(
        fill["cash_amount"] for fill in load_fills(data_dir, account_id)
        if fill.get("kind") == "corporate_action" and fill["stage"] == "cash_paid"
    )
    return {
        "corporate_income": round(corporate_income, 2),
        "corporate_income_basis": "gross_cash_separate_from_trade_pairings",
        "rounds": len(rounds),
        "win_rate": round(len(wins) / len(rounds) * 100, 2) if rounds else 0.0,
        "profit_loss_ratio": round(avg_win / avg_loss, 2) if avg_loss else None,
        "avg_holding_days": round(sum(r["holding_days"] for r in rounds) / len(rounds), 1) if rounds else 0.0,
        "realized_pnl": round(sum(r["pnl"] for r in rounds), 2),
        "max_drawdown": round(mdd * 100, 2) if mdd is not None else None,
        # 持仓周期口径 (资金加权), 与片段并列展示, 不互相覆盖。
        "cycles": {
            "count": len(cycles),
            "pairings": len(rounds),
            "win_rate": round(len(cycle_wins) / len(cycles) * 100, 2) if cycles else 0.0,
            "avg_holding_days": round(sum(c["holding_days"] for c in cycles) / len(cycles), 1) if cycles else 0.0,
            # 资金加权总收益率: 周期总盈亏 / 周期总成本。
            "money_weighted_return_pct": round(cycle_pnl / cycle_cost * 100, 2) if cycle_cost else None,
        },
    }


def _days_between(d1: str, d2: str) -> int:
    try:
        return (_date.fromisoformat(d2) - _date.fromisoformat(d1)).days
    except Exception:
        return 0
