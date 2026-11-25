"""Worker请求权威输入并绑定Compute上下文, 等待输入时只生成阶段回执。

两端计算使用同一清单; 完整池身份不缩小, 实际帧限定为已验证可用成员。
当前策略规则分别见docs/reference/s01.md及docs/reference/s02.md。
"""
from __future__ import annotations

import hashlib
import os
import sys
from copy import deepcopy
from datetime import date, datetime
from functools import cache
from pathlib import Path

import polars as pl

from app.market_time import cn_now
from app.services.strategy_input_manifest import (
    S01_ID,
    S02_ID,
    bind_manifest,
    common_key,
    digest,
    validate_manifest,
)

SID = S02_ID


def requires_authority(payload, sid):
    """Require the installed authority for the two bound strategies only."""
    return bool(payload.get("s02_inputs_required") and sid in (S01_ID, S02_ID))


@cache
def algorithm_sha256():
    """摘要覆盖计算链源码字节及数值依赖; 注释变化也改变计算身份。

    每进程缓存一次。Platform、Worker和Compute必须兼容同一摘要;
    仅重载策略文件不会刷新它, 注释修改也不能分端安装后继续共用旧清单。
    """
    app = Path(__file__).resolve().parents[1]
    paths = (
        "factors/atr.py", "strategy/late_session_strength.py", "strategy/market_data.py",
        "factors/range_amount.py", "strategy/s01_signals.py",
        "strategy/engine.py", "strategy/candidate_order.py", "strategy/entry_groups.py",
        "services/strategy_monitor_state.py", "services/strategy_monitor_tasks.py",
        "services/strategy_input_manifest.py", "services/strategy_input_source.py",
        "services/strategy_input_binding.py",
        "services/daily_close_validation.py", "services/minute_write_order.py",
        "indicators/pipeline.py", "market_time.py",
    )
    return digest({
        "sources": {path: hashlib.sha256((app / path).read_bytes()).hexdigest() for path in paths},
        "numerical_dependencies": {
            "python_major": sys.version_info[0], "implementation": sys.implementation.name,
            "float_radix": sys.float_info.radix, "float_mantissa": sys.float_info.mant_dig,
            "polars": pl.__version__,
        },
        "comparison_epsilon": 1e-10,
    })


def execution_environment():
    """记录真实解释器/Polars环境; 环境时钟不参与输入语义摘要。"""
    return {"python": list(sys.version_info[:3]), "implementation": sys.implementation.name,
            "polars": pl.__version__, "algorithm_sha256": algorithm_sha256()}


def input_request(payload, sid, action):
    """从当前任务定义提取当日启动标签、精确昨收日、完整池及冻结预选。"""
    from app.strategy.basic_filter import previous_session
    day = cn_now().date()
    window = next(w for w in payload["strategy_task_windows"][sid]["buy"]
                  if w["signal"]["id"] == action["signal_id"])
    identity = payload["daily_identities"][sid]
    from app.services.strategy_input_manifest import normalize_request
    return normalize_request({
        "strategy_id": sid, "as_of": str(day), "reference_date": str(previous_session(day)),
        "bar_at": datetime.combine(day, datetime.fromisoformat(f"{day}T{window['start']}").time()).isoformat(),
        "definition_sha256": digest({key: identity[key] for key in (
            "strategy_version", "params_json", "overrides_json", "asset_type", "timeframe", "runtime_identity")}),
        "algorithm_sha256": algorithm_sha256(),
        "pool": deepcopy(payload["daily_pool_snapshots"].get(sid) or {}),
        "selection": deepcopy(payload["selection_snapshots"].get(sid) or {}),
        "signal": deepcopy(window["signal"]), "retry_until": action["retry_until"],
    })


def accept(payload, request, phase, response):
    """先验证阶段/共同身份, 再让本轮任务使用权威池、预选和清单副本。"""
    frozen = bind_manifest(response["manifest"], request, phase)
    sid = request["strategy_id"]
    payload.setdefault("strategy_input_manifests", {}).setdefault(sid, {})[phase] = deepcopy(response["manifest"])
    payload["daily_pool_snapshots"][sid] = deepcopy(frozen["pool"])
    payload["selection_snapshots"][sid] = deepcopy(frozen["selection"])


def prepare_inputs(payload, market):
    """仅在publisher/receiver模式请求S02输入; 缺同步留状态, 不回退本地结果。"""
    if os.environ.get("TSP_S02_INPUT_AUTHORITY") not in ("publisher", "receiver"):
        return
    payload["s02_inputs_required"] = True
    for sid in payload.get("strategy_task_ids", []):
        if not requires_authority(payload, sid):
            continue
        for action in payload["strategy_task_plans"][sid]["actions"]:
            phase = action["phase"]
            if action["kind"] != "buy" or phase not in ("stocks", "breadth"):
                continue
            try:
                request = input_request(payload, sid, action)
                response = market.s02_input(request, phase)
                if response["state"] == "ready":
                    accept(payload, request, phase, response)
                status = {key: deepcopy(value) for key, value in response.items() if key != "manifest"}
                status["key"] = common_key(request)
            except Exception as exc:
                # 本地请求/绑定异常现有契约统一等待; 显式服务状态原样交给defer_input。
                status = {"state": "waiting_input", "reason": f"输入未同步: {exc}"}
            payload.setdefault("s02_input_status", {}).setdefault(sid, {})[phase] = status


def defer_input(payload, sid, phase, now):
    """不占Compute生成等待/失败回执; 截止及正常门控仍由统一任务状态层处理。"""
    from app.multitenancy.monitor_compute import failed_phase_results
    from app.services.strategy_monitor_state import receipt
    from app.services.strategy_monitor_tasks import apply_independent_buy
    status = payload["s02_input_status"][sid][phase]
    failed = status["state"] in ("invalid_input", "no_valid_input")
    errors = [status.get("reason", "输入证据异常")] if failed else []
    scoped = deepcopy(payload)
    scoped["strategy_task_ids"] = [sid]
    scoped["strategy_task_plans"][sid]["actions"] = [
        a for a in payload["strategy_task_plans"][sid]["actions"]
        if a["kind"] == "buy" and a["phase"] == phase]
    daily = failed_phase_results(scoped, errors[0] if errors else "", now)[sid]
    for action in scoped["strategy_task_plans"][sid]["actions"]:
        signal = daily["monitor_tasks"]["buy"]["signals"][action["signal_id"]]
        prior = ((payload.get("strategy_task_receipts", {}).get(sid) or {}).get("tasks", {})
                 .get("buy", {}).get("signals", {}).get(action["signal_id"], {}).get(phase, {}))
        step = receipt(prior, action, now, errors=errors, complete=False,
                       details={"input_state": status["state"], "input_key": status.get("key"),
                                "input_reason": status.get("reason"), "coverage": status.get("coverage", {}),
                                "input_wait_started_at": prior.get("input_wait_started_at") or now.isoformat()})
        if not failed and not step["terminal"]:
            step["state"] = "waiting_input"
        signal[phase] = step
    daily = apply_independent_buy(daily, payload["strategy_task_windows"][sid], now)
    return {"events": [], "execution_events": [], "state": deepcopy(payload["state"]), "results": {sid: daily}}


def frozen_context(manifest):
    """重验算法及原始清单, 用冻结帧构建实时上下文, 不另取市场数据。

    只过滤实际计算帧; 完整池和逐股不可用原因仍传给内核及结果诊断。
    """
    from app.strategy.engine import StrategyDataContext
    validate_manifest(manifest)
    if manifest["input"]["algorithm_sha256"] != algorithm_sha256():
        raise ValueError("冻结输入与Compute实际加载算法不一致")
    if manifest["state"] != "ready":
        raise ValueError("输入清单尚未就绪")
    frozen, sid = manifest["input"], manifest["input"]["strategy_id"]
    history = pl.DataFrame(frozen["minutes"], infer_schema_length=None)
    if history.is_empty():
        history = pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime, "close": pl.Float64})
    else:
        history = history.with_columns(pl.col("datetime").str.to_datetime())
    history = history.filter(pl.col("symbol").is_in(manifest["usable_symbols"]))
    target = datetime.fromisoformat(frozen["bar_at"])
    current = history.filter(pl.col("datetime") == target)
    selected = deepcopy(frozen["selection"])
    context = StrategyDataContext(
        "stock", "1m", date.fromisoformat(frozen["as_of"]), current=current, history=history,
        observed_at=cn_now(), completed_through=target, realtime=True,
        selection_snapshots={sid: selected}, daily_pool_snapshots={sid: deepcopy(frozen["pool"])},
        source_input_sha256=manifest["input_sha256"],
        unavailable_input_symbols=deepcopy(manifest["invalid_symbols"]))
    return context
