"""批次登记域 — 薄"批次"页 (持仓提醒): 只生成监控规则, 不做会计。

每行一个买入批次 → 派生两条规则: lot_{id}_p (price 止盈止损) / lot_{id}_d (date 到期提醒)。
记账/加减仓属"交易口径", 不在本模块 (issue #230)。纯函数 + 文件存储, 镜像 monitor_rules.py,
不做 API、不做引擎重载。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from datetime import date as _date
from pathlib import Path

from app.multitenancy.documents import TenantDocuments
from app.services.fs_utils import atomic_write_text
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine  # 复用条件文本拼装 (静态方法)

logger = logging.getLogger(__name__)

# id 需满足规则 id 同款正则, 且为后缀留位: 派生规则 {id}_p/_d 不得超过 40 字符
_ID = monitor_rules.ID_RE
_MAX_ID_LEN = 40 - 2  # 派生规则 id 后缀 "_p" / "_d"


def expiry_options(base: _date) -> dict[str, str | None]:
    """基准日之后第 1/5 个 A 股交易日; 未覆盖日历不猜测, 返回空选项。"""
    from app.market_time import published_trading_day

    days: list[str] = []
    current = base
    while len(days) < 5 and current < _date.max:
        current += timedelta(days=1)
        trading = published_trading_day(current)
        if trading is None:
            break
        if trading:
            days.append(current.isoformat())
    return {
        "base_date": base.isoformat(),
        "next_trading_day": days[0] if days else None,
        "five_trading_days": days[4] if len(days) == 5 else None,
    }


def _dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "lots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, lot_id: str) -> Path:
    return _dir(data_dir) / f"{lot_id}.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── 校验与归一化 ────────────────────────────────────────
def validate_lot(lot: dict) -> None:
    """校验批次字段, 非法抛 ValueError (中文信息)。"""
    lot_id = lot.get("id")
    if lot_id is not None and (
        not isinstance(lot_id, str) or not _ID.match(lot_id) or len(lot_id) > _MAX_ID_LEN
    ):
        raise ValueError(f"批次 id 非法 (仅小写字母数字下划线, 且需为派生规则 id 留位): {lot_id!r}")
    if not (lot.get("symbol") or "").strip():
        raise ValueError("symbol 不能为空")
    cost = lot.get("cost_price")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0:
        raise ValueError("cost_price 必须是正数")
    for key, label in (("qty", "数量"), ("target_pct", "止盈%"), ("stop_pct", "止损%")):
        v = lot.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{label} 不能为负数")
    lead = lot.get("lead_days", 0)
    if isinstance(lead, bool) or not isinstance(lead, int) or lead < 0:
        raise ValueError("lead_days 必须是非负整数")
    for key, label in (("buy_date", "买入日期"), ("remind_date", "到期日")):
        raw = lot.get(key)
        if raw not in (None, ""):
            try:
                _date.fromisoformat(raw)
            except ValueError:
                raise ValueError(f"{label} 必须是 YYYY-MM-DD: {raw!r}") from None
    if not (lot.get("target_pct", 0) > 0 or lot.get("stop_pct", 0) > 0 or lot.get("remind_date")):
        raise ValueError("止盈% / 止损% / 到期日 至少设置一项 (否则无监控点)")


def normalize_lot(lot: dict) -> dict:
    """补全默认字段, 返回规范化后的批次 (不校验)。"""
    d = dict(lot)
    d["symbol"] = (d.get("symbol") or "").strip()
    d.setdefault("qty", 0)
    d.setdefault("cost_price", 0)
    d.setdefault("buy_date", None)
    d.setdefault("target_pct", 0)
    d.setdefault("stop_pct", 0)
    d.setdefault("remind_date", None)
    d.setdefault("lead_days", 1)
    d.setdefault("created_at", _now_iso())
    return d


# ── 持久化 ─────────────────────────────────────────────
def load_all(data_dir: Path) -> list[dict]:
    """读取全部批次。损坏的文件被跳过。"""
    if isinstance(data_dir, TenantDocuments):
        return [normalize_lot(lot) for lot in data_dir.values("lots")]
    out: list[dict] = []
    for f in sorted(_dir(data_dir).glob("lot_*.json")):
        try:
            out.append(normalize_lot(json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:
            logger.warning("lot load failed %s: %s", f.name, e)
    return out


def save_one(data_dir: Path, lot: dict) -> None:
    if isinstance(data_dir, TenantDocuments):
        validate_lot(lot)
        data_dir.mutate("lots", lot["id"], lambda old: {
            **lot, "created_at": old["created_at"] if old and old.get("created_at") else lot.get("created_at", _now_iso()),
        })
        return
    p = _path(data_dir, lot["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(lot, ensure_ascii=False, indent=2))


def delete_one(data_dir: Path, lot_id: str) -> bool:
    if isinstance(data_dir, TenantDocuments):
        return data_dir.delete("lots", lot_id)
    p = _path(data_dir, lot_id)
    if p.exists():
        p.unlink()
        return True
    return False


# 批次元数据与每日序号; 旧 lot 没有 batch_id, 由页面展示为“历史股票”。
def _batch_day() -> str:
    from app.market_time import cn_today

    return cn_today().strftime("%Y%m%d")


def _batch_path(data_dir: Path) -> Path:
    return _dir(data_dir) / "_batches.json"


def _batch_state(data_dir: Path) -> dict:
    path = _batch_path(data_dir)
    if not path.exists():
        return {"batches": {}, "sequences": {}}
    # 序号文件损坏时拒绝创建, 不能从 01 重启导致编号复用。
    return json.loads(path.read_text(encoding="utf-8"))


def load_batches(data_dir: Path) -> list[dict]:
    rows = (data_dir.values("lot_batches") if isinstance(data_dir, TenantDocuments)
            else list(_batch_state(data_dir)["batches"].values()))
    return sorted(rows, key=lambda row: row["number"], reverse=True)


def next_batch_number(data_dir: Path) -> str | None:
    day = _batch_day()
    seq = (data_dir.get("lot_batch_sequences", day, 0) if isinstance(data_dir, TenantDocuments)
           else _batch_state(data_dir)["sequences"].get(day, 0))
    return f"{day}{seq + 1:02d}" if seq < 99 else None


def save_batch(data_dir: Path, batch: dict) -> dict:
    """调用方持有写事务/锁; 失败时批次、序号与股票一起回滚。"""
    number = next_batch_number(data_dir)
    if number is None:
        raise ValueError("当天已创建99个批次, 请次日再创建")
    saved = {**batch, "number": number, "id": number, "created_at": _now_iso()}
    saved["name"] = saved["name"] or f"持仓批次 {number}"
    if isinstance(data_dir, TenantDocuments):
        data_dir.put("lot_batch_sequences", number[:8], int(number[8:]))
        data_dir.put("lot_batches", saved["id"], saved)
    else:
        state = _batch_state(data_dir)
        state["sequences"][number[:8]] = int(number[8:])
        state["batches"][saved["id"]] = saved
        atomic_write_text(_batch_path(data_dir), json.dumps(state, ensure_ascii=False))
    return saved


def delete_batch(data_dir: Path, batch_id: str) -> None:
    """调用方持有写事务/锁; 只删元数据, 保留每日序号防止编号复用。"""
    if isinstance(data_dir, TenantDocuments):
        data_dir.delete("lot_batches", batch_id)
    else:
        state = _batch_state(data_dir)
        state["batches"].pop(batch_id, None)
        atomic_write_text(_batch_path(data_dir), json.dumps(state, ensure_ascii=False))


# ── 批次 → 监控规则 (纯映射) ───────────────────────────
def lot_to_rules(lot: dict) -> tuple[dict | None, dict | None]:
    """批次 → (price 止盈止损规则, date 到期规则); 无对应监控点时返回 None。

    纯映射不做 I/O; 规则 id 派生自批次 id ({lot_id}_p/_d), 保证稳定可级联。
    """
    symbol = lot["symbol"]
    lot_id = lot["id"]
    cost = float(lot["cost_price"])
    target = float(lot.get("target_pct", 0))
    stop = float(lot.get("stop_pct", 0))
    qty = float(lot.get("qty", 0) or 0)
    qty_text = f" · {qty:g}股" if qty > 0 else ""

    conds: list[dict] = []
    if target > 0:
        conds.append({"field": "close", "op": ">=", "value": round(cost * (1 + target / 100), 4)})
    if stop > 0:
        conds.append({"field": "close", "op": "<=", "value": round(cost * (1 - stop / 100), 4)})
    price_rule = None
    if conds:
        msg = f"批次止盈止损 · 成本{cost:g}"
        if target > 0:
            msg += f" · 止盈{target:g}%"
        if stop > 0:
            msg += f" · 止损{stop:g}%"
        msg += qty_text
        cond_text = MonitorRuleEngine._format_conditions_text({"logic": "or"}, conds)
        if cond_text:
            msg += f" · {cond_text}"
        price_rule = {
            "id": f"{lot_id}_p",
            "name": f"批次止盈止损 · {symbol}",
            "type": "price",
            "asset_type": "stock",
            "scope": "symbols",
            "symbols": [symbol],
            "conditions": conds,
            "logic": "or",
            "cooldown_seconds": 86400,
            "severity": "warn",
            "message": msg,
            "enabled": True,
            "lot_id": lot_id,
        }

    date_rule = None
    if lot.get("remind_date"):
        lead = int(lot.get("lead_days", 1))
        date_rule = {
            "id": f"{lot_id}_d",
            "name": f"批次到期 · {symbol}",
            "type": "date",
            "asset_type": "stock",
            "scope": "symbols",
            "symbols": [symbol],
            "remind_date": lot["remind_date"],
            "lead_days": lead,
            "cooldown_seconds": 86400,
            "severity": "info",
            # 提前天数由引擎 evaluate_date_rules 统一追加, 这里只放静态部分
            "message": f"批次到期提醒 · {lot['remind_date']}{qty_text}",
            "enabled": True,
            "lot_id": lot_id,
        }
    return price_rule, date_rule
