"""共享定时分钟入场: 固定启动数据, 逐股隔离, 两阶段由调用方汇合。

模板ID沿用late_session_strength, 早盘/尾盘时钟来自保存信号。
结束限制同份输入修复及提交, 成功零命中不会随后续分钟重选。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time as clock
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import CN_TZ
from app.services.kline_periods import aggregate_minute_period

TEMPLATE = "late_session_strength"

# 本模块由 S01、S02 及其他分钟策略复用; 各策略保存值只在对应 reference 页维护。
# 模板默认值不证明策略保存值。计算阶段不取上游、不写业务库; 配置适配沿现有入口读取。

# 计算身份中的日期统一编码为ISO字符串, 保证内存date与JSON恢复值可持久化。
def _identity_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# 出现这些键、entry_groups或显式null时使用可选条件契约; null关闭对应条件。
ENTRY_METRIC_KEYS = {"return_5m_min_pct", "return_15m_min_pct", "position_min_pct", "gap_max_pct"}
OPTIONAL_KEYS = {"amount_min_100m", "turnover_min_pct", "amplitude_max_pct",
                 "volume_ratio_min", "end_time"} | ENTRY_METRIC_KEYS
# 参数单位: change/pool_up/turnover/amplitude为百分数值, amount为亿元,
# volume_ratio为倍数, MA数量为整数; evaluation_delay_minutes在存储中仍使用分钟。
# 缺省继承模板, 显式null关闭条件; 已保存定义由parameters按其输入契约解析。
DEFAULT_PARAMS = {
    "time": "14:30:00", "change_min_pct": None, "change_max_pct": None,
    "pool_up_min_pct": 60.0, "ma5_1m_count": 7, "ma5_5m_count": 4,
    "evaluation_delay_minutes": 0.5,
    **dict.fromkeys(OPTIONAL_KEYS),
}


# 识别输入契约, 不通过策略名称或稳定ID判断, 供S02及其他共享模板调用。
def optional_definition(sig: dict) -> bool:
    """按参数结构识别可空条件契约, 保留冻结六字段定义的原读取路径。"""
    raw = sig.get("params") or {}
    return isinstance(raw, dict) and ("entry_groups" in raw or bool(OPTIONAL_KEYS & raw.keys())
                                     or any(value is None for value in raw.values()))


def parameters(sig: dict) -> dict:
    """校验并解析保存值; 显式null不回填默认, 缺延迟键保留冻结定义的零延迟。"""
    # 先拒绝未知键和非法分组, 再合并默认; 保留显式null, 不能在重开时补回条件。
    raw = sig.get("params") or {}
    if not isinstance(raw, dict) or set(raw) - (set(DEFAULT_PARAMS) | {"entry_groups", "s05_entry"}):
        raise ValueError("尾盘走强参数格式错误或包含未知参数")
    if "s05_entry" in raw:
        from app.strategy.s02_173004552 import ENTRY_IDS, entry_parameters

        branch = next((branch for branch, sid in ENTRY_IDS.items()
                       if sid == sig.get("id")), None)
        if branch is None:
            raise ValueError("仅S05分支信号支持专用入场参数")
        entry_parameters(branch, raw["s05_entry"])
    from app.strategy.entry_groups import validate

    validate(raw.get("entry_groups"))
    params = {**DEFAULT_PARAMS, **raw}
    # 冻结的六字段定义保持原时点; 缺延迟键按0处理, 不自动迁移已冻结输入。
    delay = raw.get("evaluation_delay_minutes", 0)
    if not (delay is None and optional_definition(sig)) and (isinstance(delay, bool) or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or (delay != 0 and not (0.5 <= delay <= 15 and delay * 60 % 30 == 0))):
        raise ValueError("尾盘走强确认延迟须为30~900秒且为30秒的整数倍; 0秒仅兼容旧定义")
    params["evaluation_delay_minutes"] = delay
    # 输入声明可空条件时使用该契约, 不根据策略名称选择解析分支。
    if optional_definition(sig):
        return _optional_parameters(params, raw)
    # 冻结旧输入未声明可空契约时, 保留其原有缺省涨幅边界。
    params["change_min_pct"] = raw.get("change_min_pct", .4)
    params["change_max_pct"] = raw.get("change_max_pct", 4.0)
    latest = (datetime.combine(date.min, time(15)) - timedelta(minutes=delay)).time()
    try:
        at = time.fromisoformat(params["time"])
        if (at.strftime("%H:%M:%S") != params["time"] or at.tzinfo
                or (delay == 0 and at.second)):
            raise ValueError
        if not ((time(9, 30) <= at <= time(11, 30)) or (time(13) <= at <= latest)):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(
            f"尾盘走强启动时间须为北京时间09:30:00~11:30:00、13:00:00~{latest}; "
            "延迟确认定义支持秒, 无延迟旧定义秒数须为 00"
        ) from None
    for key in ("change_min_pct", "change_max_pct", "pool_up_min_pct"):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("涨幅与上涨占比须为有限数值")
    if not 0 <= params["change_min_pct"] <= params["change_max_pct"] <= 100:
        raise ValueError("涨幅范围须满足 0 <= 下限 <= 上限 <= 100%")
    if not 0 <= params["pool_up_min_pct"] <= 100:
        raise ValueError("股票池上涨占比须在 0~100%")
    for key, maximum in (("ma5_1m_count", 120), ("ma5_5m_count", 24)):
        if type(params[key]) is not int or not 2 <= params[key] <= maximum:
            raise ValueError(f"{key} 须为 2~{maximum} 的整数")
    return params


def _optional_parameters(params: dict, raw: dict) -> dict:
    """可空阈值逐项校验; 启动加延迟不得越过保存结束时间或当日收盘。"""
    # 起止时间使用北京时间墙钟; 开始/结束可空, 确认延迟可空或为30秒的整数倍。
    delay = raw.get("evaluation_delay_minutes", 0)
    params["evaluation_delay_minutes"] = delay
    for key in ("time", "end_time"):
        value = params[key]
        if value is None:
            continue
        try:
            at = time.fromisoformat(value)
            if (at.tzinfo or at.strftime("%H:%M:%S") != value
                    or not ((time(9, 30) <= at <= time(11, 30)) or (time(13) <= at <= time(15)))):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{key} 须为北京时间09:30:00~11:30:00、13:00:00~15:00:00") from None
    if params["time"] is not None:
        # 确认必须在当日收盘前; 已设置结束时间时, 不得早于确认时间。
        confirmation = datetime.combine(date.min, time.fromisoformat(params["time"])) + timedelta(minutes=delay or 0)
        if confirmation.date() != date.min or confirmation.time() > time(15):
            raise ValueError("确认时间不得晚于15:00:00")
        if params["end_time"] is not None and params["end_time"] < str(confirmation.time()):
            raise ValueError("结束时间不得早于确认时间")
    for key in ("pool_up_min_pct", "amount_min_100m", "turnover_min_pct",
                "amplitude_max_pct", "change_min_pct", "change_max_pct", "volume_ratio_min"):
        value = params[key]
        minimum = -10.0 if key == "change_min_pct" else .1
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not minimum <= value <= 100):
            raise ValueError(f"{key} 须为{minimum}~100.0或为空")
    for key in ENTRY_METRIC_KEYS:
        value = params[key]
        minimum = -10 if key in ("return_5m_min_pct", "return_15m_min_pct") else .1
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                  or not minimum <= value <= 100):
            raise ValueError(f"{key} 须为{minimum}~100.0或为空")
    if (params["change_min_pct"] is not None and params["change_max_pct"] is not None
            and params["change_min_pct"] > params["change_max_pct"]):
        raise ValueError("当日涨幅下限不得大于上限")
    # 数量1表示只要求一个有效MA5值; 数量N表示检查N-1次严格递增。
    for key in ("ma5_1m_count", "ma5_5m_count"):
        if params[key] is not None and (type(params[key]) is not int or not 1 <= params[key] <= 100):
            raise ValueError(f"{key} 须为1~100的整数或为空")
    return params


def evaluation_time(sig: dict, day: date, *, completed_through: datetime | None = None) -> datetime | None:
    """最早确认时点; 修复可在有效窗口继续, 不把它解释为实际完成时间。"""
    params = parameters(sig)
    # 固定启动时刻加保存延迟; 启动为空时由调用方已完成分钟推动计算。
    if params["time"] is None:
        at = completed_through
    else:
        at = datetime.combine(day, time.fromisoformat(params["time"]))
    return at + timedelta(minutes=params["evaluation_delay_minutes"] or 0) if at is not None else None


def execution_end(sig: dict, day: date) -> datetime:
    """保存结束时钟同时约束确认后的成交执行; 实时新入场还有独立截止守卫。"""
    # 模板未设结束时间时返回收盘; 实时新入场另受14:55守卫约束。
    return datetime.combine(day, time.fromisoformat(parameters(sig)["end_time"] or "15:00:00"))


def bar_time(sig: dict, day: date, *, completed_through: datetime | None = None) -> datetime | None:
    """信号的启动分钟数据标签, 确认延迟不改变此截止。"""
    value = parameters(sig)["time"]
    # 等待确认只改变可计算时间, 不改变启动数据标签、累计范围或MA右端点。
    if value is None:
        if completed_through is None:
            return None
        if completed_through.tzinfo:
            completed_through = completed_through.astimezone(CN_TZ).replace(tzinfo=None)
        return completed_through.replace(second=0, microsecond=0)
    at = time.fromisoformat(value).replace(second=0, microsecond=0)
    return datetime.combine(day, at)


def pool_cutoff(engine, strategy_ids, overrides_map, day: date, fallback: datetime) -> datetime:
    """以相关信号最早观察时点限制证券状态证据, 避免使用更晚公告缩小完整池。"""
    # 股票范围的公告观察时刻取最早相关评估时刻, 避免用之后发布的状态缩小当时完整池。
    from app.strategy import custom_signals

    getter = getattr(engine, "_user_data_dir", None)
    storage = getter() if callable(getter) else None
    if storage is None or not callable(getattr(engine, "get", None)):
        return fallback
    requested = set()
    for sid in strategy_ids:
        strategy = engine.get(sid)
        override = (overrides_map or {}).get(sid) or {}
        requested.update(override.get("entry_signals", strategy.entry_signals) or [])
        requested.update(strategy.required_features)
    times = [evaluation_time(sig, day)
             for sig in custom_signals.load_intraday_all(storage)
             if sig.get("template") == TEMPLATE and sig.get("enabled") is not False
             and custom_signals.intraday_column_name(sig["id"]) in requested]
    return min([fallback, *(at for at in times if at is not None)])


def pending_evaluation_time(engine, strategy, overrides, context) -> datetime | None:
    """返回仍未到达的最晚已选确认时点, 供调用方区分等待与正常零命中。"""
    # 找出仍未到确认时刻的已选信号; 供引擎显示等待, 不把未确认解释为成功零命中。
    observed = context.observed_at or context.completed_through
    storage = engine._user_data_dir()
    if observed is None or storage is None:
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    from app.strategy import custom_signals

    selected = set(engine._effective_signals(
        overrides or {}, "entry_signals", strategy.entry_signals,
    ))
    latest = context.completed_through
    if latest is None and context.history is not None and "datetime" in context.history.columns:
        dt = pl.col("datetime")
        if context.history.schema["datetime"].time_zone:
            dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
        latest = context.history.select(dt.alias("datetime")).filter(
            (pl.col("datetime").dt.date() == context.as_of)
            & (pl.col("datetime") <= observed))["datetime"].max()
    if latest is not None and latest.tzinfo:
        latest = latest.astimezone(CN_TZ).replace(tzinfo=None)
    pending = []
    for sig in custom_signals.load_intraday_all(storage):
        if (sig.get("template") != TEMPLATE or sig.get("enabled") is False
                or custom_signals.intraday_column_name(sig["id"]) not in selected):
            continue
        available = evaluation_time(sig, context.as_of, completed_through=latest)
        if available is not None and observed < available:
            pending.append(available)
    return max(pending, default=None)


def entry_deadline(engine, strategy, overrides, context, *, expired_only=True) -> datetime | None:
    """返回策略交易日14:55与保存结束时钟的较早边界; 严格超过才算过期。"""
    # 按交易日取14:55与保存结束时间的较早者; 严格大于才过期, 整秒仍允许提交。
    # expired_only=False供调用方查询边界, True只返回当前已经超时的边界。
    storage = engine._user_data_dir()
    observed = context.observed_at or context.completed_through
    if storage is None or observed is None:
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    from app.strategy import custom_signals

    selected = set(engine._effective_signals(overrides or {}, "entry_signals", strategy.entry_signals))
    deadlines = []
    for sig in custom_signals.load_intraday_all(storage):
        if (sig.get("template") != TEMPLATE or sig.get("enabled") is False
                or custom_signals.intraday_column_name(sig["id"]) not in selected):
            continue
        end = datetime.combine(context.as_of, time(14, 55))
        configured = parameters(sig)["end_time"]
        if configured is not None:
            end = min(end, datetime.combine(context.as_of, time.fromisoformat(configured)))
        if not expired_only or observed > end:
            deadlines.append(end)
    return min(deadlines, default=None)


@dataclass
class Evaluation:
    """单次模板计算事实; diagnostics表示输入/计算问题, warnings保留局部异常。

    stage区分等待、正常拒绝、确认及过期; 空symbols本身不证明成功零命中。
    progress为本次子步骤证据, 与外层任务尝试历史及最终发布状态分别保存。
    """

    # symbols为本次计算通过的股票; 独立个股阶段仍是内部候选, 必须由join_buy决定发布。
    # candidates是双MA之前的数值过滤集合, 不能直接当作最终买名单。
    symbols: list[str] = field(default_factory=list)
    pool_up_pct: float | None = None
    diagnostics: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    stage: str = "waiting_start"
    # total保存完整池, usable是当前阶段有效成员; 缺行情不能改变完整池身份。
    pool_total: int = 0
    pool_usable: int = 0
    missing_pct: float = 0.0
    breadth_policy: str = "available_members_v1"
    ma_skipped_symbols: list[str] = field(default_factory=list)
    known_positive: int = 0
    missing_symbols: list[str] = field(default_factory=list)
    # 上下界仅描述缺失成员的不确定性; 实际通过判断使用有效分母pool_up_pct。
    pool_up_pct_lower: float | None = None
    pool_up_pct_upper: float | None = None
    warnings: list[str] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    # entry_matches及错误只服务显式保存的A/B分组, 不为未配置分组的策略补条件。
    entry_matches: dict = field(default_factory=dict)
    entry_group_errors: dict = field(default_factory=dict)


def progress_plan(sig: dict, day: date, observed: datetime | None = None,
                  *, diagnostic_symbols: list[str] | None = None) -> dict:
    """生成四步计划; 仅真实回执填执行事实, 不能从浏览器或服务端时钟推断已完成。"""
    # 先建立待计算计划; 只有真实计算回执才填写开始、结束、计数和耗时。
    # diagnostic_symbols限制页面诊断计数的对象, 不改变全池广度的计算范围。
    params = parameters(sig)
    start = datetime.combine(day, time.fromisoformat(params["time"])) if params["time"] else None
    prefilter = start + timedelta(seconds=15) if start and (
        optional_definition(sig) or params["evaluation_delay_minutes"]) else start
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    return {
        "schema": "late_session_progress_v1", "as_of": str(day),
        "cohort": "pool" if diagnostic_symbols is None else "frozen_selection",
        "observed_at": observed.isoformat() if observed is not None else None,
        "bar_at": bar_time(sig, day).isoformat() if params["time"] else None, "params": params,
        "steps": {key: {
            "state": "waiting", "scheduled_at": planned.isoformat() if planned else None,
            "started_at": None, "finished_at": None, "elapsed_ms": None,
            "processed": None, "total": None, "passed": None, "missing": None,
            "reason": "等待计算回执",
        } for key, planned in (
            ("breadth", prefilter), ("change", evaluation_time(sig, day) if optional_definition(sig) else prefilter),
            ("ma1", evaluation_time(sig, day)), ("ma5", evaluation_time(sig, day)),
        )},
    }


# 墙钟记录审计时间, 单调时钟测量耗时; 两者不能互相替代。
def _started() -> tuple[str, float]:
    return datetime.now(CN_TZ).replace(tzinfo=None).isoformat(), clock.perf_counter()


# 写入阶段计算事实; values可进一步将状态标为failed/rejected并保存覆盖计数。
def _receipt(step: dict, started: tuple[str, float], **values) -> None:
    step.update(
        state="success", reason=None, started_at=started[0],
        finished_at=datetime.now(CN_TZ).replace(tzinfo=None).isoformat(),
        elapsed_ms=round((clock.perf_counter() - started[1]) * 1000, 3),
    )
    step.update(values)


# 缺基础输入时标记前置阶段失败, 未执行的MA阶段跳过; 显式关闭的条件继续保留skipped。
def failed_progress(progress: dict, reason: str) -> dict:
    """原地标注未完成子步骤, 保留显式skipped; 不伪造开始/结束时间或计数。"""
    for key, step in progress["steps"].items():
        if step["state"] == "skipped":
            continue
        step.update(state="failed" if key in ("breadth", "change") else "skipped",
                    reason=reason)
    return progress


def _ready_symbols(frame: pl.DataFrame, symbols: set[str], end: datetime, count: int) -> set[str]:
    """确认基础一分钟窗口完整有效; 这是MA依赖, 不是全天分钟根数门槛。"""
    # 基础一分钟窗口须恰有count根、时标不重复、左右端匹配且收盘价有效。
    # 调用方已限制交易时段; 后续MA内核继续验证对应周期的相邻时标间距。
    start = end - timedelta(minutes=count - 1)
    window = frame.filter(pl.col("symbol").is_in(sorted(symbols))
                          & pl.col("datetime").is_between(start, end))
    ready = window.group_by("symbol").agg(
        ((pl.len() == count) & (pl.col("datetime").n_unique() == count)
         & (pl.col("datetime").min() == start) & (pl.col("datetime").max() == end)
         & (pl.col("close").is_finite() & (pl.col("close") > 0)).fill_null(False).all()
         ).alias("_ready"))
    return set(ready.filter(pl.col("_ready"))["symbol"].to_list())


def _rising_symbols(frame: pl.DataFrame, end: datetime, count: int, step: int) -> set[str]:
    """N 个 MA5 需 N+4 根连续完成 K; 不跨午休, 不吞掉缺口。"""
    # step为1或5分钟。N个MA5共需N+4根该周期K, 时间跨度为(N+3)*step。
    # 五分钟输入已聚合且以桶末标时; 其底层1m窗口完整性由调用方先验证。
    start = end - timedelta(minutes=(count + 3) * step)
    if not ((time(9, 31) <= start.time() <= end.time() <= time(11, 30))
            or (time(13, 1) <= start.time() <= end.time() <= time(15))):
        return set()
    window = frame.filter(pl.col("datetime").is_between(start, end)).sort("symbol", "datetime")
    if window.is_empty():
        return set()
    # 每股独立滚动均值; 最初4根用于预热, 恰好留下count个有效MA5值。
    window = window.with_columns(pl.col("close").rolling_mean(5).over("symbol").alias("_ma"))
    stats = window.group_by("symbol").agg(
        ((pl.len() == count + 4)
         & (pl.col("datetime").n_unique() == count + 4)
         & (pl.col("datetime").min() == start) & (pl.col("datetime").max() == end)
         & (pl.col("datetime").diff().drop_nulls().min() == timedelta(minutes=step))
         & (pl.col("datetime").diff().drop_nulls().max() == timedelta(minutes=step))
         & (pl.col("close").is_finite() & (pl.col("close") > 0)).fill_null(False).all()
         & (pl.col("_ma").count() == count)
         # 严格递增且增量大于数值噪声容差; 持平不会通过。
         & (pl.col("_ma").diff().drop_nulls() > 1e-10).all()).alias("_rising"))
    return set(stats.filter(pl.col("_rising"))["symbol"].to_list())


# 冻结六字段定义仍由evaluate按输入结构路由到此处; 不是当前S02的计算分支。
def _evaluate_legacy(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None,
    diagnostic_symbols: list[str] | None = None,
) -> Evaluation:
    params = parameters(sig)
    result = Evaluation()
    result.progress = progress_plan(sig, day, completed_through,
                                    diagnostic_symbols=diagnostic_symbols)
    steps = result.progress["steps"]
    at = bar_time(sig, day)
    cutoff = None
    if completed_through is not None:
        cutoff = completed_through
        if cutoff.tzinfo:
            cutoff = cutoff.astimezone(CN_TZ).replace(tzinfo=None)
        start = datetime.combine(day, time.fromisoformat(params["time"]))
        prefilter_at = start + timedelta(seconds=15) if params["evaluation_delay_minutes"] else start
        if cutoff < prefilter_at:
            return result
    if sig.get("enabled") is False:
        for step in steps.values():
            step.update(state="skipped", reason="信号已停用")
        return result
    result.stage = "waiting_data"
    pool = sorted(set(pool_symbols or []))
    result.pool_total = len(pool)
    if not pool:
        result.diagnostics.append("尾盘走强缺少完整的当日股票池")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    if minute_df is None or not {"symbol", "datetime", "close"}.issubset(minute_df.columns):
        result.diagnostics.append("尾盘走强缺少分钟行情")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    breadth_started = _started()
    dt = pl.col("datetime")
    if minute_df.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    frame = minute_df.with_columns(dt.alias("datetime")).filter(
        pl.col("symbol").is_in(pool) & (pl.col("datetime").dt.date() == day)
        & (pl.col("datetime") <= at)
        & (pl.col("datetime").dt.time().is_between(time(9, 31), time(11, 30))
           | pl.col("datetime").dt.time().is_between(time(13, 1), time(15))))
    quotes = frame.filter(pl.col("datetime") == at)
    quotes_by_symbol = quotes.partition_by("symbol", as_dict=True)
    returns: dict[str, float] = {}
    unknown = []
    for symbol in pool:
        quote = quotes_by_symbol.get((symbol,))
        previous = prev_close.get(symbol)
        if (quote is None or quote.height != 1 or previous is None or not math.isfinite(previous) or previous <= 0
                or quote["close"][0] is None or not math.isfinite(quote["close"][0])
                or quote["close"][0] <= 0):
            unknown.append(symbol)
        else:
            returns[symbol] = (quote["close"][0] / previous - 1) * 100
    result.known_positive = sum(value > 0 for value in returns.values())
    result.missing_symbols = unknown
    result.pool_usable = len(returns)
    result.missing_pct = 100 * len(unknown) / len(pool)
    result.pool_up_pct_lower = 100 * result.known_positive / len(pool)
    result.pool_up_pct_upper = 100 * (result.known_positive + len(unknown)) / len(pool)
    if unknown:
        result.warnings.append(
            f"尾盘走强: 剔除 {len(unknown)}/{len(pool)} 只缺少有效指定分钟价格或昨收的股票, "
            f"按剩余 {len(returns)} 只计算上涨占比")
    if not returns:
        result.diagnostics.append("尾盘走强: 当日股票池剔除缺失数据后无有效股票, 无法计算上涨占比")
        failed_progress(result.progress, result.diagnostics[-1])
        _receipt(steps["breadth"], breadth_started, state="failed",
                 reason=result.diagnostics[-1], total=len(pool), processed=len(pool),
                 passed=0, missing=len(unknown), up_pct=None)
        return result
    # 缺有效价格/昨收仅退出上涨占比的分母, 保留完整池及逐股缺失记录。
    result.pool_up_pct = 100 * result.known_positive / len(returns)
    _receipt(steps["breadth"], breadth_started, total=len(pool), processed=len(pool),
             passed=result.known_positive, missing=len(unknown), usable=len(returns),
             up_pct=result.pool_up_pct, lower_pct=result.pool_up_pct_lower)
    change_started = _started()
    candidates = {symbol for symbol, value in returns.items()
                  if params["change_min_pct"] - 1e-10 <= value <= params["change_max_pct"] + 1e-10}
    cohort = set(pool) if diagnostic_symbols is None else set(diagnostic_symbols) & set(pool)
    _receipt(steps["change"], change_started, total=len(cohort), processed=len(cohort),
             passed=len(candidates & cohort), missing=len(cohort - returns.keys()),
             pool_passed=len(candidates))
    if result.pool_up_pct + 1e-10 < params["pool_up_min_pct"]:
        result.stage = "breadth_rejected"
        steps["breadth"].update(state="rejected", reason="上涨占比未达下限")
        for key in ("ma1", "ma5"):
            steps[key].update(state="skipped", reason="上涨占比未达下限")
        return result
    result.candidates = sorted(candidates)
    if cutoff is not None and cutoff < evaluation_time(sig, day):
        result.stage = "waiting_confirmation"
        return result
    result.stage = "confirmed"
    # 只保留所需MA时间窗口; 冻结契约在聚合前检查重复, 不改变当前可选契约的去重方式。
    minutes = max(params["ma5_1m_count"] + 4, (params["ma5_5m_count"] + 4)*5 + 4)
    frame = frame.filter(pl.col("datetime") > at - timedelta(minutes=minutes))
    end5 = at.replace(minute=(at.minute//5)*5)
    ma1_started = _started()
    ready1 = _ready_symbols(frame, candidates, at, params["ma5_1m_count"] + 4)
    unique = frame.group_by("symbol").agg((pl.len() == pl.col("datetime").n_unique()).alias("_unique"))
    unique_symbols = set(unique.filter(pl.col("_unique"))["symbol"].to_list())
    one_all = _rising_symbols(frame.filter(pl.col("symbol").is_in(sorted(ready1 & unique_symbols))),
                              at, params["ma5_1m_count"], 1)
    _receipt(steps["ma1"], ma1_started, total=len(candidates & cohort),
             processed=len(candidates & cohort), passed=len(one_all & cohort),
             missing=len((candidates - ready1) & cohort))
    ma5_started = _started()
    ready5 = _ready_symbols(frame, ready1, end5, (params["ma5_5m_count"] + 4) * 5)
    result.ma_skipped_symbols = sorted((candidates - ready1) | (ready1 - ready5))
    if result.ma_skipped_symbols:
        result.warnings.append(
            f"尾盘走强: 跳过 {len(result.ma_skipped_symbols)} 只缺少完整MA5分钟窗口的股票, 其余继续计算")
    # 保留两个完整窗口的交集, 同时分别记录每个阶段的淘汰数量。
    one = one_all & ready5
    frame = frame.filter(pl.col("symbol").is_in(sorted(ready1 & ready5 & unique_symbols)))
    if one:
        # 五分钟桶的5根基础收盘价都须有效, 只检查桶末价不足以证明完整。
        valid = frame.filter(pl.col("symbol").is_in(sorted(one))
                             & pl.col("close").is_finite() & (pl.col("close") > 0))
        five = aggregate_minute_period(valid, "5m", complete_only=True)
        result.symbols = sorted(one & _rising_symbols(five, end5, params["ma5_5m_count"], 5))
    if one_all:
        _receipt(steps["ma5"], ma5_started, total=len(one_all & cohort),
                 processed=len(one_all & cohort), passed=len(set(result.symbols) & cohort),
                 missing=len((one_all - ready5) & cohort))
    else:
        steps["ma5"].update(state="skipped", reason="1分钟MA5无通过股票")
    return result


# 仅当所启用的条件依赖昨收时声明依赖, 避免已关闭条件仍阻塞输入准备。
def needs_previous_close(sig: dict) -> bool:
    """只声明已启用涨幅、广度、振幅或分组所需的精确昨收依赖。"""
    from app.strategy.entry_groups import needs_previous

    params = parameters(sig)
    return needs_previous(params.get("entry_groups")) or any(params[key] is not None for key in (
        "pool_up_min_pct", "change_min_pct", "change_max_pct", "amplitude_max_pct", "gap_max_pct"))


def selection_closes(selection: dict) -> dict[str, float]:
    """读取冻结预选的有效原始昨收, 拒绝布尔/非有限/非正值, 不重新读全池日线。"""
    return {row["symbol"]: row["reference_close"] for row in selection["rows"]
            if isinstance(row.get("reference_close"), (int, float))
            and not isinstance(row["reference_close"], bool)
            and math.isfinite(row["reference_close"]) and row["reference_close"] > 0}


def condition_inputs(context, sig: dict) -> dict:
    """仅为启用的换手率、量比准备股本或前五日均量, 不执行行情IO。

    消费调用方的时点帧; 单股无有效股本/均量时不生成该键, 留给数值内核
    判断缺依赖。精确交易日解析或帧运算异常向上抛出, 由调用方记录输入失败。
    """
    # 消费调用方提供的时点快照, 不访问Provider; 只准备已启用条件的专用依赖。
    params = parameters(sig)
    inputs = {}
    sources = [source for source in (context.daily_history, context.pool_history, context.current)
               if source is not None and not source.is_empty()]
    if params["turnover_min_pct"] is not None:
        shares = {}
        for source in [*sources, context.pool_universe]:
            if source is None or "symbol" not in source.columns:
                continue
            if "date" in source.columns:
                source = source.filter(pl.col("date") == context.as_of)
            if "float_shares" not in source.columns:
                if not {"volume", "turnover_rate"} <= set(source.columns):
                    continue
                # 用同一日线行的量和换手率恢复当日有效股本; volume为手、换手率为百分数。
                # volume*100股/手*100/turnover_rate得到股数, 不借日线量额作盘中分子。
                source = source.with_columns(
                    pl.when((pl.col("volume") > 0) & (pl.col("turnover_rate") > 0))
                    .then(pl.col("volume") * 10000 / pl.col("turnover_rate")).alias("float_shares"))
            for row in source.iter_rows(named=True):
                value = row["float_shares"]
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    shares[str(row["symbol"])] = value
        inputs["float_shares"] = shares
    if params["volume_ratio_min"] is not None:
        # 量比只使用精确前5交易日的完整成交量; 不能以最近5条非空记录替代。
        from app.strategy.basic_filter import previous_session

        expected = []
        prior = context.as_of
        for _ in range(5):
            prior = previous_session(prior)
            expected.append(prior)
        parts = []
        for source in sources:
            if not {"symbol", "date", "volume"}.issubset(source.columns):
                continue
            prior_rows = source.filter(pl.col("date").is_in(expected))
            rejected = prior_rows.group_by("symbol", "date").len().filter(pl.col("len") != 1)
            if not rejected.is_empty():
                prior_rows = prior_rows.join(rejected.select("symbol", "date"), on=["symbol", "date"], how="anti")
            if "quote_ts" in prior_rows.columns or "quote_time_basis" in prior_rows.columns:
                # 源快照须已在当日收盘后定版; 无快照时钟的标准日线批次沿原契约消费。
                stamp = pl.col("quote_ts").cast(pl.Int64, strict=False) if "quote_ts" in prior_rows.columns else pl.lit(None)
                basis = pl.col("quote_time_basis") if "quote_time_basis" in prior_rows.columns else pl.lit(None)
                opening = pl.col("date").cast(pl.Datetime).dt.replace_time_zone("Asia/Shanghai").dt.timestamp("ms")
                batch = stamp.is_null() & basis.is_null()
                finalized = (basis == "source_snapshot") & stamp.is_between(
                    opening + 15 * 3600000, opening + 24 * 3600000 - 1)
                prior_rows = prior_rows.filter((batch | finalized).fill_null(False))
            parts.append(prior_rows.select("symbol", "date", pl.col("volume").cast(pl.Float64)))
        means = {}
        if parts:
            daily = pl.concat(parts, how="vertical_relaxed")
            # 不同上下文里的同日相同值可合并; 冲突、重复或缺日不能伪装成完整5日均量。
            stats = daily.group_by("symbol", "date").agg(
                pl.col("volume").n_unique().alias("_distinct"),
                pl.col("volume").first().alias("volume"),
            ).group_by("symbol").agg(
                ((pl.len() == 5) & (pl.col("_distinct") == 1).all()
                 & (pl.col("volume").is_finite() & (pl.col("volume") >= 0)).fill_null(False).all()).alias("_valid"),
                pl.col("volume").mean().alias("_mean"),
            )
            means = {row["symbol"]: row["_mean"] for row in stats.filter(
                pl.col("_valid") & (pl.col("_mean") > 0)).iter_rows(named=True)}
        # 该映射仅在保存了量比下限时产生; 保留共享模板能力不代表S02启用了量比。
        inputs["previous_volume_mean"] = means
    return inputs


# 共享单次内核按启用条件计算。独立广度与个股用breadth_only/skip_breadth限制职责,
# observe_breadth允许空下限计算真实比例; 这些开关不自行授权发布买信息。
def _evaluate_optional(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None, diagnostic_symbols: list[str] | None = None,
    float_shares: dict[str, float] | None = None,
    previous_volume_mean: dict[str, float] | None = None,
    breadth_only: bool = False, skip_breadth: bool = False, observe_breadth: bool = False,
    selection: dict | None = None,
) -> Evaluation:
    """顺序计算数值交集及两项MA; 部分缺输入跳过, 全部缺必要输入返回失败诊断。

    breadth_only只做完整池广度; skip_breadth下个股输入来自冻结预选。
    change/ma1/ma5是同一次个股计算的诊断步骤, 不是三个独立冻结点。
    本函数不保存task_state; 正常不达阈值与必要字段/MA窗口全缺分别返回
    无命中或diagnostics。帧运算异常交外层attempt/独立阶段转失败回执。
    """
    params = parameters(sig)
    result = Evaluation(progress=progress_plan(sig, day, completed_through,
                                               diagnostic_symbols=diagnostic_symbols))
    steps = result.progress["steps"]
    # 条件为空既不筛选, 也不要求它专用的字段; 回执显式显示该条件未生效。
    for key, enabled in (("breadth", params["pool_up_min_pct"] is not None or observe_breadth),
                         ("change", any(params[name] is not None for name in (
                             "change_min_pct", "change_max_pct", "amount_min_100m",
                             "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min",
                             *ENTRY_METRIC_KEYS))),
                         ("ma1", params["ma5_1m_count"] is not None),
                         ("ma5", params["ma5_5m_count"] is not None)):
        if not enabled:
            steps[key].update(state="skipped", reason="参数为空, 不生效")
    if sig.get("enabled") is False:
        for step in steps.values():
            step.update(state="skipped", reason="信号已停用")
        return result
    observed = completed_through
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    result.stage = "waiting_data"
    pool = sorted(set(pool_symbols or []))
    result.pool_total = len(pool)
    # 广度使用完整池; 个股使用冻结预选子集, 不要求广度先成功。
    stock_pool = (sorted({row["symbol"] for row in selection["rows"]} & set(pool))
                  if skip_breadth and selection is not None else pool)
    if pool and skip_breadth and selection is not None and not stock_pool:
        result.stage = "confirmed"
        at = bar_time(sig, day, completed_through=observed)
        result.progress["bar_at"] = at.isoformat() if at is not None else None
        for key in ("change", "ma1", "ma5"):
            steps[key].update(state="skipped", reason="冻结预选为空", total=0, processed=0, passed=0)
        return result
    if not pool or minute_df is None or not {"symbol", "datetime", "close"}.issubset(minute_df.columns):
        result.diagnostics.append("尾盘走强缺少完整股票池或分钟行情")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    dt = pl.col("datetime")
    if minute_df.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    frame = minute_df.with_columns(dt.alias("datetime")).filter(
        pl.col("symbol").is_in(stock_pool) & (pl.col("datetime").dt.date() == day)
        & ((pl.col("datetime").dt.time().is_between(time(9, 31), time(11, 30)))
           | (pl.col("datetime").dt.time().is_between(time(13, 1), time(15)))))
    if observed is not None:
        # 只消费调用方确认已完成的时间上界, 过滤同日尚未完成的分钟。
        frame = frame.filter(pl.col("datetime") <= observed)
    latest = frame["datetime"].max() if not frame.is_empty() else None
    at = bar_time(sig, day, completed_through=latest)
    if at is None:
        return result
    check_at = evaluation_time(sig, day, completed_through=at)
    result.progress["bar_at"] = at.isoformat()
    if observed is not None and params["time"] and observed < datetime.combine(day, time.fromisoformat(params["time"])):
        result.stage = "waiting_start"
        return result
    if params["end_time"] is not None and (observed or check_at).time() > time.fromisoformat(params["end_time"]):
        result.stage = "expired"
        return result
    # 固定启动数据; 同股同分钟取最后一行, 累计量额不重复计算, 也不补造缺口。
    frame = frame.filter(pl.col("datetime") <= at).unique(
        ["symbol", "datetime"], keep="last", maintain_order=True).sort("symbol", "datetime")
    quotes = frame.filter(pl.col("datetime") == at)
    prices = {row["symbol"]: row["close"] for row in quotes.iter_rows(named=True)
              if isinstance(row["close"], (int, float)) and math.isfinite(row["close"]) and row["close"] > 0}
    # 缺启动价格者不进入候选; 有价格但缺有效昨收者不能通过已启用的涨幅条件。
    returns = {symbol: (price / prev_close[symbol] - 1) * 100 for symbol, price in prices.items()
               if isinstance(prev_close.get(symbol), (int, float))
               and math.isfinite(prev_close[symbol]) and prev_close[symbol] > 0}
    # returns为百分数值; 广度以价格和昨收均有效者为分母, 个股先按有效价格建立候选。
    calculate_breadth = (params["pool_up_min_pct"] is not None or observe_breadth) and not skip_breadth
    valid = set(returns) if calculate_breadth else set(prices)
    result.pool_usable = len(valid)
    result.missing_symbols = sorted(set(pool) - valid)
    result.missing_pct = 100 * len(result.missing_symbols) / len(pool)
    if not valid:
        result.diagnostics.append("尾盘走强完整池中无有效股票")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    if calculate_breadth:
        # 上涨严格>0; 分母是有效成员数, 完整池和缺失成员仍单独保存。
        started = _started()
        result.known_positive = sum(value > 0 for value in returns.values())
        result.pool_up_pct = 100 * result.known_positive / len(returns)
        result.pool_up_pct_lower = 100 * result.known_positive / len(pool)
        result.pool_up_pct_upper = 100 * (result.known_positive + len(result.missing_symbols)) / len(pool)
        _receipt(steps["breadth"], started, total=len(pool), processed=len(pool),
                 usable=len(returns), passed=result.known_positive,
                 missing=len(result.missing_symbols), up_pct=result.pool_up_pct)
        if params["pool_up_min_pct"] is None:
            steps["breadth"]["reason"] = "仅展示, 不参与买入筛选"
        elif result.pool_up_pct + 1e-10 < params["pool_up_min_pct"]:
            result.stage = "breadth_rejected"
            steps["breadth"].update(state="rejected", reason="上涨占比未达下限")
            return result
    if breadth_only:
        result.stage = "waiting_confirmation"
        return result
    started = _started()
    candidates = set(prices)
    # 各数值条件取交集, 涨幅边界含等号; 缺昨收者不能通过已启用的涨幅条件。
    for key, minimum in (("change_min_pct", True), ("change_max_pct", False)):
        if params[key] is not None:
            candidates &= {symbol for symbol, value in returns.items()
                           if (value + 1e-10 >= params[key] if minimum else value <= params[key] + 1e-10)}
    missing = (set(prices) - set(returns)) if any(
        params[key] is not None for key in ("change_min_pct", "change_max_pct")) else set()
    fields = set()
    # 按启用条件声明最少字段; 未启用的量额条件不读取其专用输入。
    if params["amount_min_100m"] is not None:
        fields.add("amount")
    if params["turnover_min_pct"] is not None or params["volume_ratio_min"] is not None:
        fields.add("volume")
    if params["amplitude_max_pct"] is not None or params["position_min_pct"] is not None:
        fields.update(("high", "low"))
    aggregates = {}
    if fields <= set(frame.columns) and fields:
        # 累计范围仅到启动分钟, 量额求和、价格取高低极值, 任一所用字段无效则标未知。
        expressions = [(pl.col(key).is_finite() & (pl.col(key) >= 0)).fill_null(False).all().alias(f"valid_{key}")
                       for key in fields]
        expressions += [pl.col(key).sum().alias(key) for key in fields & {"amount", "volume"}]
        if "high" in fields:
            expressions += [pl.col("high").max().alias("high"), pl.col("low").min().alias("low"),
                            ((pl.col("high") >= pl.col("low")) & (pl.col("low") > 0)).fill_null(False).all().alias("_ohlc")]
        aggregates = {row["symbol"]: row for row in frame.group_by("symbol").agg(expressions).iter_rows(named=True)}
    from app.market_time import trading_minutes_elapsed_from_dt

    bases = {}
    for length in (5, 15):
        key = f"return_{length}m_min_pct"
        if params[key] is not None:
            bases[key] = {row["symbol"]: row["close"] for row in
                          frame.filter(pl.col("datetime") == at - timedelta(minutes=length)).iter_rows(named=True)}
    openings = {}
    if params["gap_max_pct"] is not None and "open" in frame.columns:
        openings = {row["symbol"]: row["open"] for row in
                    frame.filter(pl.col("datetime") == datetime.combine(day, time(9, 31))).iter_rows(named=True)}
    condition_candidates = set(candidates)
    for symbol in list(candidates):
        row = aggregates.get(symbol, {})
        values = {}
        for key, reference in bases.items():
            base = reference.get(symbol)
            values[key] = ((prices[symbol] / base - 1) * 100
                           if type(base) in (int, float) and math.isfinite(base) and base > 0 else None)
        if params["position_min_pct"] is not None:
            upper, lower = row.get("high"), row.get("low")
            valid_range = (row.get("valid_high") and row.get("valid_low") and row.get("_ohlc")
                           and lower <= prices[symbol] <= upper)
            values["position_min_pct"] = (
                ((prices[symbol] - lower) / (upper - lower) * 100 if upper > lower else 50)
                if valid_range else None)
        if params["gap_max_pct"] is not None:
            opening, previous = openings.get(symbol), prev_close.get(symbol)
            values["gap_max_pct"] = (
                (opening / previous - 1) * 100
                if all(type(value) in (int, float) and math.isfinite(value) and value > 0
                       for value in (opening, previous)) else None)
        if params["amount_min_100m"] is not None:
            values["amount_min_100m"] = row.get("amount", 0) / 1e8 if row.get("valid_amount") else None
        if params["turnover_min_pct"] is not None:
            # volume是手, 乘100换股, 再乘100转百分数; 股本是当日有效流通股数。
            shares = (float_shares or {}).get(symbol)
            values["turnover_min_pct"] = (row["volume"] * 10000 / shares
                                         if row.get("valid_volume") and shares and math.isfinite(shares) and shares > 0 else None)
        if params["amplitude_max_pct"] is not None:
            previous = prev_close.get(symbol)
            values["amplitude_max_pct"] = (
                (row["high"] - row["low"]) / previous * 100
                if row.get("valid_high") and row.get("valid_low") and row.get("_ohlc")
                and previous and math.isfinite(previous) and previous > 0 else None)
        if params["volume_ratio_min"] is not None:
            # 已成交量按实际交易分钟折算240分钟全日量, 再与准确前5日均量比较。
            mean = (previous_volume_mean or {}).get(symbol)
            elapsed = trading_minutes_elapsed_from_dt(at)
            values["volume_ratio_min"] = (row["volume"] * 240 / elapsed / mean
                                         if row.get("valid_volume") and mean and math.isfinite(mean)
                                         and mean > 0 and elapsed > 0 else None)
        for key, value in values.items():
            if value is None or not math.isfinite(value):
                missing.add(symbol)
                candidates.discard(symbol)
            elif (value > params[key] + 1e-10 if key in ("amplitude_max_pct", "gap_max_pct")
                  else value + 1e-10 < params[key]):
                candidates.discard(symbol)
    cohort = set(stock_pool) if diagnostic_symbols is None else set(diagnostic_symbols) & set(stock_pool)
    if steps["change"]["state"] != "skipped":
        _receipt(steps["change"], started, total=len(cohort), processed=len(cohort),
                 passed=len(candidates & cohort), missing=len(missing & cohort), pool_passed=len(candidates))
    if set(prices) <= missing or (condition_candidates and condition_candidates <= missing):
        # 全部有效价格成员或数值候选缺启用依赖是输入失败; 阈值外正常淘汰不进入此分支。
        result.diagnostics.append("尾盘走强: 全部有效价格成员缺少启用的个股条件数据")
        steps["change"].update(state="failed", reason=result.diagnostics[-1])
        return result
    result.candidates = sorted(candidates)
    if observed is not None and observed < check_at:
        result.stage = "waiting_confirmation"
        return result
    result.stage = "confirmed"
    # 先1m再5m, 后一项只消费前一项通过者; 重试stocks会从数值过滤重新执行。
    for key, step_key, period in (("ma5_1m_count", "ma1", 1), ("ma5_5m_count", "ma5", 5)):
        count = params[key]
        if count is None:
            continue
        started = _started()
        end = at.replace(minute=(at.minute // period) * period)
        # 周期为5时先确认(N+4)*5根完整1m数据再聚合, 不用不完整桶计算MA。
        ready = _ready_symbols(frame, candidates, end, (count + 4) * period)
        selected = frame.filter(pl.col("symbol").is_in(sorted(ready)))
        if period == 5 and not selected.is_empty():
            selected = aggregate_minute_period(selected, "5m", complete_only=True)
        rising = _rising_symbols(selected, end, count, period) if not selected.is_empty() else set()
        result.ma_skipped_symbols.extend(sorted(candidates - ready))
        _receipt(steps[step_key], started, total=len(candidates & cohort), processed=len(candidates & cohort),
                 passed=len(rising & cohort), missing=len((candidates - ready) & cohort))
        if candidates and not ready:
            # 部分缺窗可逐股跳过; 全部候选都缺窗则整侧失败, 交回状态层修复重试。
            result.stage = "waiting_data"
            result.diagnostics.append(f"尾盘走强: 全部候选缺少{period}分钟MA5完整窗口")
            steps[step_key].update(state="failed", reason=result.diagnostics[-1])
            return result
        # 有完整窗口但未递增是正常拒绝; 部分缺窗的股票不会在本次结果中自动补回。
        candidates &= rising
    if params.get("entry_groups") is not None:
        # 分组只消费显式保存定义及对应预选, 不将共享分组条件加入普通尾盘策略。
        from app.strategy.entry_groups import match

        matches, errors, coverage = match(params["entry_groups"], selection, frame,
            day=day, at=at, candidates=candidates, previous=prev_close)
        result.entry_matches, result.entry_group_errors = matches, errors
        result.progress["entry_groups"] = coverage
        if coverage["total"] and not coverage["usable"]:
            result.stage = "waiting_data"
            result.diagnostics.append("A/B入场筛选: 全部候选缺少可计算的分组数据")
            return result
        result.symbols = list(matches)
        if errors:
            result.warnings.append(f"A/B入场筛选: {len(errors)}只股票存在分组数据缺失, 详见异常记录")
    else:
        result.symbols = sorted(candidates)
    return result


def _evaluate_independent(minute_df, sig, *, day, pool_symbols, prev_close,
                          completed_through, diagnostic_symbols, float_shares,
                          previous_volume_mean, task_state, input_error, selection, phase,
                          startup_publication_complete=False, unavailable_symbols=None,
                          source_input_sha256=None):
    """按阶段身份分别冻结正常终态; 失败留给监控状态层修复重试。

    breadth 身份含完整池, stocks 身份含目标日冻结预选; 两者各自持有
    task_state.independent_v1 下的状态。固定启动分钟尚未到齐时不冻结该侧;
    单股异常仍隔离, 个股内部步骤失败时从数值交集重新计算。
    state只在内存修改; 仅计算身份改变时清掉该侧缓存。
    终态复用不依赖行情发布世代, 持久化、最终汇合与新事件去重由所有者负责。
    """
    if phase not in ("breadth", "stocks"):
        raise ValueError("未知独立入场计算阶段")
    params = parameters(sig)
    observed = completed_through
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    progress = progress_plan(sig, day, observed, diagnostic_symbols=diagnostic_symbols)
    progress["phase"] = phase
    other = ("breadth",) if phase == "stocks" else ("change", "ma1", "ma5")
    for key in other:
        progress["steps"][key].update(state="skipped", reason="由另一独立阶段计算")
    # 个股绑定冻结预选, 广度绑定完整池; 不将全池输入故障传播为预选身份变化。
    from app.strategy.entry_groups import ORDERING_POLICY

    identity_input = selection if phase == "stocks" else sorted(set(pool_symbols or []))
    identity = hashlib.sha256(json.dumps(
        ["independent_v1", phase, str(day), sig.get("id"), params, identity_input,
         *([source_input_sha256] if source_input_sha256 else []),
         None if params["time"] else str(bar_time(sig, day, completed_through=observed)),
         *(["breadth_observation_v1"] if phase == "breadth" and params["pool_up_min_pct"] is None else []),
         *([ORDERING_POLICY] if params.get("entry_groups") else [])],
        sort_keys=True, ensure_ascii=False, default=_identity_date).encode()).hexdigest()
    owner = task_state if task_state is not None else {}
    owner["as_of"] = str(day)
    state = owner.setdefault("independent_v1", {}).setdefault(phase, {})
    if state.get("identity") != identity:
        state.clear()
        state.update(identity=identity, as_of=str(day), params=params, signal_id=sig.get("id"))
    # 正常通过、正常拒绝及真实过期都复用本侧回执; 可修复失败不写终态。
    # 普通行情发布世代不在身份中; 启用权威输入时绑定的是不可变清单语义摘要。
    # 该清单首次ready后不改写, 更新行情不能重开已有正常终态。
    if state.get("terminal"):
        return Evaluation(**copy.deepcopy(state["terminal"]))
    result = Evaluation(progress=progress)
    start = datetime.combine(day, time.fromisoformat(params["time"])) if params["time"] else None
    # 广度在启动后15秒开始, 个股在保存确认时点开始; 输入数据仍固定为启动分钟。
    ready = (start + timedelta(seconds=15) if phase == "breadth" and start else
             evaluation_time(sig, day))
    if observed is not None and ready is not None and observed < ready:
        result.stage = "waiting_breadth" if phase == "breadth" else "waiting_confirmation"
        return result
    end = min(execution_end(sig, day), datetime.combine(day, time(14, 55)))
    try:
        if observed is not None and (observed > end or (phase == "breadth" and observed >= end)):
            result.stage = "expired"
            raise ValueError("信号有效时段已结束")
        if input_error:
            raise ValueError(input_error)
        if phase == "stocks":
            from app.strategy.basic_filter import previous_session
            if (selection is None or selection.get("as_of") != str(day)
                    or selection.get("source_date") != str(previous_session(day))
                    or not isinstance(selection.get("rows"), list)):
                raise ValueError(f"缺少{day}有效冻结预选, 个股任务等待")
            pool_symbols = sorted({row["symbol"] for row in selection["rows"]})
            prev_close = selection_closes(selection)
            if not pool_symbols:
                # 成功空预选可正常冻结; 缺预选在上方报输入错误, 不走此零结果分支。
                result.stage = "conditions_rejected"
                for key in ("change", "ma1", "ma5"):
                    progress["steps"][key].update(state="skipped", reason="冻结预选为空",
                                                  total=0, processed=0, passed=0)
                state["terminal"] = asdict(result)
                return result
        result = _evaluate_optional(
            minute_df, sig, day=day, pool_symbols=pool_symbols, prev_close=prev_close,
            completed_through=observed, diagnostic_symbols=diagnostic_symbols,
            float_shares=float_shares, previous_volume_mean=previous_volume_mean,
            breadth_only=phase == "breadth", skip_breadth=phase == "stocks",
            observe_breadth=phase == "breadth", selection=selection)
        result.progress["phase"] = phase
        for key in other:
            result.progress["steps"][key] = copy.deepcopy(progress["steps"][key])
        if not result.diagnostics and params["time"]:
            # 公共分钟发布可分批到达; 行缺失不能被正常零候选固化。
            # 已到达但价格/昨收异常仍由原逐股隔离处理, 不要求全池数值有效。
            dt = pl.col("datetime")
            if minute_df.schema["datetime"].time_zone:
                dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
            received = set(minute_df.filter(
                dt == bar_time(sig, day))["symbol"])
            absent = sorted(set(pool_symbols or []) - received - set(unavailable_symbols or {}))
            # 兼容非权威输入: 全量完成发布可允许广度隔离缺行。
            # 当前S02权威路径先验证完整响应证明, 再传unavailable_symbols;
            # 普通采集完成时间不足以通过清单门禁, 不在这里补造缺分钟。
            if absent and not (phase == "breadth" and startup_publication_complete):
                result.stage = "waiting_data"
                result.diagnostics = [f"{params['time']}启动分钟尚未到齐: 缺{len(absent)}只股票"]
                result.progress["startup_missing_symbols"] = absent
                failed_progress(result.progress, result.diagnostics[0])
        if not result.diagnostics and (phase == "breadth" or result.stage == "confirmed"):
            if phase == "stocks":
                result.stage = "confirmed" if result.symbols else "conditions_rejected"
                result.progress["confirmed_at"] = observed.isoformat() if observed else None
            # 正常完成才冻结; 共享内核保留逐股缺窗兼容行为。
            # 当前S02在调用前已通过全部必要窗口/不可用证明门禁。
            state["terminal"] = asdict(result)
    except Exception as exc:
        result.diagnostics = [str(exc)]
        result.stage = "expired" if result.stage == "expired" else "waiting_data"
        failed_progress(result.progress, str(exc))
        if result.stage == "expired":
            state["terminal"] = asdict(result)
    if result.diagnostics:
        result.progress.update(failure_reason="; ".join(result.diagnostics), phase=phase)
    return result


def evaluate(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None, diagnostic_symbols: list[str] | None = None,
    float_shares: dict[str, float] | None = None,
    previous_volume_mean: dict[str, float] | None = None,
    task_state: dict | None = None,
    input_error: str | None = None,
    breadth_only: bool = False,
    selection: dict | None = None,
    independent_phase: str | None = None,
    historical: bool = False,
    startup_publication_complete: bool = False,
    unavailable_symbols: dict | None = None,
    source_input_sha256: str | None = None,
) -> Evaluation:
    """按保存契约和显式阶段派发; 计算使用北京时间已完成分钟前缀。

    historical 仅供显式历史回放: 观测取已完成时刻与保存确认时刻的较早值,
    固定信号仍读启动分钟, 确认前输入不提前完成。普通实时调用不启用此
    标志, 盘后仍过期。startup_publication_complete只由可信广度入口根据绑定的
    普通分钟采集完成时钟提供, 允许缺行按有效成员隔离, 不接受策略参数覆盖。
    task_state 仅在内存更新, 所有者负责连同回执持久化。
    """
    # 调用方负责将task_state与最终名单一起持久化; 本函数不自行写业务库或提交委托。
    if not optional_definition(sig):
        return _evaluate_legacy(
            minute_df, sig, day=day, pool_symbols=pool_symbols, prev_close=prev_close,
            completed_through=completed_through, diagnostic_symbols=diagnostic_symbols)
    if historical and completed_through is not None:
        # 明确历史回放的盘后时间只证明输入可用, 固定信号按保存确认时刻评估;
        # 不能推进尚未完成的前缀, 也不能借用后续分钟价格。
        observed = completed_through
        if observed.tzinfo:
            observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
        confirmation = evaluation_time(sig, day)
        if confirmation is not None:
            completed_through = min(observed, confirmation)
    if independent_phase is not None:
        return _evaluate_independent(
            minute_df, sig, day=day, pool_symbols=pool_symbols, prev_close=prev_close,
            completed_through=completed_through, diagnostic_symbols=diagnostic_symbols,
            float_shares=float_shares, previous_volume_mean=previous_volume_mean,
            task_state=task_state, input_error=input_error, selection=selection, phase=independent_phase,
            startup_publication_complete=startup_publication_complete,
            unavailable_symbols=unavailable_symbols, source_input_sha256=source_input_sha256)
    params = parameters(sig)
    state = task_state if task_state is not None else {}
    if (input_error and pool_symbols is None and state.get("as_of") == str(day)
            and state.get("params") == params and state.get("signal_id") == sig.get("id")):
        # 读取输入失败不能证明股票范围变更, 继续保留同日同信号已经冻结的完整池。
        pool_symbols = state.get("pool_symbols")
    observed = completed_through
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    progress = progress_plan(sig, day, observed, diagnostic_symbols=diagnostic_symbols)
    for key, enabled in (("breadth", params["pool_up_min_pct"] is not None),
                         ("change", any(params[name] is not None for name in (
                             "change_min_pct", "change_max_pct", "amount_min_100m",
                             "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min",
                             *ENTRY_METRIC_KEYS))),
                         ("ma1", params["ma5_1m_count"] is not None),
                         ("ma5", params["ma5_5m_count"] is not None)):
        if not enabled:
            progress["steps"][key].update(state="skipped", reason="参数为空, 不生效")
    result = Evaluation(progress=progress, pool_total=len(set(pool_symbols or [])))
    if sig.get("enabled") is False:
        for step in progress["steps"].values():
            step.update(state="skipped", reason="信号已停用")
        return result
    start = datetime.combine(day, time.fromisoformat(params["time"])) if params["time"] else None
    breadth_at = start + timedelta(seconds=15) if start else None
    confirm_at = evaluation_time(sig, day)
    # 未声明独立阶段的可空定义保持串行回执; 当前S02从上方独立阶段入口返回。
    inputs = dict(day=day, pool_symbols=pool_symbols, prev_close=prev_close,
                  completed_through=observed, diagnostic_symbols=diagnostic_symbols,
                  float_shares=float_shares, previous_volume_mean=previous_volume_mean,
                  selection=selection)
    # 状态身份包含日期、信号参数、完整池及必要分组快照; 身份变化时才清掉前次阶段。
    from app.strategy.entry_groups import ORDERING_POLICY

    identity = hashlib.sha256(json.dumps(
        [str(day), sig.get("id"), params, sorted(set(pool_symbols or [])),
         None if start else str(bar_time(sig, day, completed_through=observed)),
         selection if params.get("entry_groups") is not None else None,
         *([ORDERING_POLICY] if params.get("entry_groups") else [])],
        sort_keys=True, ensure_ascii=False, default=_identity_date).encode()).hexdigest()
    if state.get("identity") != identity:
        state.clear()
        state.update(identity=identity, as_of=str(day), params=params,
                     signal_id=sig.get("id"), pool_symbols=sorted(set(pool_symbols or [])))
    end = min(execution_end(sig, day), datetime.combine(day, time(14, 55)))
    # 只恢复旧的提前确认超时回执; 正常通过/拒绝和窗口外过期仍保持原终态。
    terminal = state.get("terminal") or {}
    if (terminal.get("stage") == "expired"
            and terminal.get("diagnostics") == ["上涨占比在确认时点前未计算成功"]
            and observed is not None and start is not None and start <= observed < end):
        state.pop("terminal", None)
    if state.get("terminal"):
        # 重启、行情更新及重复调用直接返回已保存终态, 不能重算成功零命中。
        return Evaluation(**copy.deepcopy(state["terminal"]))
    if start and observed is not None and observed < start:
        result.stage = "waiting_start"
        return result
    if breadth_at and observed is not None and observed < breadth_at and params["pool_up_min_pct"] is not None:
        result.stage = "waiting_breadth"
        return result
    if observed is not None and observed > end:
        # 严格超过结束时刻才过期; 任务调度/提交层还分别校验自身的边界。
        result.stage = "expired"
        result.diagnostics = ["信号有效时段已结束"]
        failed_progress(progress, result.diagnostics[0])
        if not state.get("terminal"):
            state["terminal"] = asdict(result)
        return result
    # 单次计算异常转换成可追踪的输入失败, 修复动作由外层监控任务服务负责。
    def attempt(**phase):
        try:
            if input_error:
                raise ValueError(input_error)
            phase_inputs = ({**inputs, "prev_close": selection_closes(selection)}
                            if phase.get("skip_breadth") and selection is not None
                            and params.get("entry_groups") is None else inputs)
            return _evaluate_optional(minute_df, sig, **phase, **phase_inputs)
        except Exception as exc:
            failed = Evaluation(progress=copy.deepcopy(progress), stage="waiting_data",
                                pool_total=result.pool_total, diagnostics=[str(exc)])
            failed_progress(failed.progress, str(exc))
            return failed

    breadth = None
    if params["pool_up_min_pct"] is not None:
        if state.get("breadth"):
            # 成功广度沿同份启动输入冻结, 个股重试不重新计算它。
            breadth = Evaluation(**copy.deepcopy(state["breadth"]))
        else:
            breadth = attempt(breadth_only=True)
            if not breadth.diagnostics:
                state["breadth"] = asdict(breadth)
            else:
                # 确认时点不是修复截止; 广度失败在有效买入窗口内继续由外层重试。
                state["breadth_failed"] = True
                state["retry_until"] = end.isoformat()
                state["retry_phase"] = "breadth"
        if breadth.diagnostics or breadth.stage == "breadth_rejected":
            if breadth.diagnostics:
                breadth.progress.update(failure_reason="; ".join(breadth.diagnostics), phase="breadth")
            if breadth.stage == "breadth_rejected":
                # 条件正常未通过是最终业务结果, 不请求补数来改变它。
                state["terminal"] = asdict(breadth)
            return breadth
    if breadth_only:
        return breadth or result
    if confirm_at and observed is not None and observed < confirm_at:
        result = breadth or result
        result.stage = "waiting_confirmation"
        return result
    result = attempt(skip_breadth=True)
    if breadth is not None:
        # 合并冻结广度事实, 避免个股阶段重新构造的进度覆盖已成功广度。
        for name in ("pool_up_pct", "pool_up_pct_lower", "pool_up_pct_upper", "known_positive",
                     "pool_usable", "missing_pct", "missing_symbols"):
            setattr(result, name, copy.deepcopy(getattr(breadth, name)))
        result.progress["steps"]["breadth"] = copy.deepcopy(breadth.progress["steps"]["breadth"])
    if result.diagnostics:
        result.progress.update(failure_reason="; ".join(result.diagnostics), phase="stocks")
        state["retry_until"] = end.isoformat() if end else None
        state["retry_phase"] = "stocks"
    elif result.stage == "confirmed":
        # 有信号与正常零信号都写终态; 后续任务可以据此确认次日卖名单是否为空。
        result.stage = "confirmed" if result.symbols else "conditions_rejected"
        result.progress["confirmed_at"] = observed.isoformat() if observed else (
            confirm_at.isoformat() if confirm_at else result.progress["bar_at"])
        state["terminal"] = asdict(result)
        state.pop("retry_phase", None)
        state.pop("retry_until", None)
    return result


def next_task_time(sig: dict, day: date, observed: datetime, state: dict) -> datetime | None:
    """兼容串行模板的下一阶段唤醒, 不依赖公共报价序号是否更新。

    返回最早可检查时刻, 不承诺实际执行时间; 独立阶段调度使用监控状态层plan。
    """
    # 根据未完成阶段给出下一唤醒时刻, 不依赖公共行情序号是否又增加。
    if not optional_definition(sig) or sig.get("enabled") is False:
        return None
    params = parameters(sig)
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    if params["time"] is None:
        return None
    if state.get("as_of") != str(day) or state.get("params") != params:
        state = {}
    if state.get("terminal"):
        return None
    end = datetime.combine(day, time(14, 55))
    if params["end_time"] is not None:
        end = min(end, datetime.combine(day, time.fromisoformat(params["end_time"])))
    if observed > end:
        return None
    start = datetime.combine(day, time.fromisoformat(params["time"]))
    planned = (start + timedelta(seconds=15)
               if params["pool_up_min_pct"] is not None and not state.get("breadth")
               else evaluation_time(sig, day))
    return max(observed, planned) if planned is not None else observed


def previous_closes(
    context, *, exact_session: bool = True, isolate_symbols: bool = False,
) -> dict[str, float]:
    """从上下文读取信号日前的原始昨收映射, 不执行IO或读取当日收盘。

    默认要求精确T-1并校验定版, 缺整份参考抛ValueError。隔离模式将单股
    定版/来源冲突写context.reference_errors并排除对应映射, 保留完整池身份;
    exact_session=False仅供已有兼容路径读取最近可用历史参考。
    """
    # exact_session=True使用已发布日历的精确T-1; isolate_symbols=True隔离单股冲突。
    previous = None
    if exact_session:
        from app.strategy.basic_filter import previous_session

        previous = previous_session(context.as_of)
    parts = []
    for source in (context.current, context.daily_history, context.pool_history):
        if source is None or source.is_empty() or not {"symbol", "date"}.issubset(source.columns):
            continue
        column = "raw_close" if "raw_close" in source.columns else "close"
        if column not in source.columns:
            continue
        prior = source.filter(pl.col("date") < context.as_of)
        if exact_session:
            prior = prior.filter(pl.col("date") == previous)
            if not prior.is_empty():
                from app.services.daily_close_validation import require_final_daily_rows

                rejected = require_final_daily_rows(prior, previous, isolate_symbols=isolate_symbols)
                # 定版校验产生的逐股错误进入统一参考诊断, 不缩小完整股票范围。
                if rejected:
                    context.reference_errors.update(rejected)
        elif {"volume", "amount"}.issubset(prior.columns):
            prior = prior.filter(
                (pl.col("volume") > 0) | (pl.col("amount") > 0)
                | (pl.col("volume").is_null() & pl.col("amount").is_null()))
        parts.append(prior.select("symbol", "date", pl.col(column).cast(pl.Float64, strict=False).alias("raw_close")))
    if not parts:
        if exact_session:
            raise ValueError(f"{previous}缺少准确前交易日定版昨收")
        return {}
    prior = pl.concat(parts, how="vertical_relaxed").filter(
        pl.col("raw_close").is_finite() & (pl.col("raw_close") > 0)).sort("date", maintain_order=True)
    if exact_session and prior.is_empty():
        raise ValueError(f"{previous}缺少准确前交易日定版昨收")
    if exact_session:
        # 多个上下文中的同股同价允许共存; 不同有效价格不能静默取最后一份。
        conflicting = prior.group_by("symbol").agg(pl.col("raw_close").n_unique().alias("_prices")).filter(pl.col("_prices") > 1)
        if not conflicting.is_empty():
            if not isolate_symbols:
                raise ValueError(f"{previous} {conflicting['symbol'][0]}昨收来源不一致")
            context.reference_errors.update({
                symbol: f"{previous} {symbol}昨收来源不一致" for symbol in conflicting["symbol"]
            })
    if isolate_symbols:
        prior = prior.filter(~pl.col("symbol").is_in(list(context.reference_errors)))
    return {str(row["symbol"]): row["raw_close"]
            for row in prior.unique("symbol", keep="last").iter_rows(named=True)}
