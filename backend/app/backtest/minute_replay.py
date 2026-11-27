"""分钟策略回测回放器 — 逐交易日回放 filter_minute_history 产生入场信号。

与实盘选股 (ScreenerService 1m context) 走同一条 StrategyEngine.run 执行路径,
消除回测/实盘偏差。语义铁律:

- 分钟侧: 传入当日全量分钟分区, 策略函数自身因果 (第 m 根只用 <=m 的K线);
- 日线侧: T 日的日线条件窗口只含 T-1 及更早的完成态日K — 与实盘盘中行为一致
  (当日成形K不进窗口), 杜绝未来函数;
- 成交侧: 信号实际可用后的第一根完整分钟开始五分钟容量撮合;
- 按交易日精确对日: 缺分钟分区的日子显式跳过, 不做"回退最近分区" (那是实盘语义)。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

import polars as pl

from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine

# 日线面板列: 基础行情 + 涨停/炸板信号 (策略日线窗口契约) + 基础过滤/展示列。
# raw_close 用于涨停价计算 (分钟价是未复权真实价, 涨停规则定义在原始价上)。
MINUTE_DAILY_PANEL_COLUMNS = frozenset({
    "open", "high", "low", "close", "volume", "amount",
    "raw_close", "raw_high", "raw_low",
    "quote_ts", "quote_time_basis",
    "turnover_rate",
    "signal_limit_up", "signal_limit_down", "signal_broken_limit_up",
})
MINUTE_INSTRUMENT_COLUMNS = frozenset({"name", "total_shares", "float_shares"})


def minute_replay_feature_plan(
    daily_bars: int, basic_filter: dict | None = None, overrides: dict | None = None,
):
    """分钟回测的日线面板加载计划。

    execution_backend 用 polars_expr 走"按需计算信号"路径: enriched 分区只落
    基础列, 涨停/炸板信号由 load_panel_for_backtest 的 compute_limit_signals
    按 signal_columns 需求现算 (matrix_native 路径会跳过通用信号计算)。
    """
    # 函数级导入规避与 strategy.py 的循环依赖 (strategy 顶层导入本模块)。
    from app.backtest.strategy import ResolvedFeaturePlan
    from app.strategy import overlay

    dependencies = overlay.dependencies(overlay.effective_conditions(overrides))
    from app.indicators.pipeline import INDICATOR_COLUMNS
    from app.strategy.basic_filter import fields
    from app.strategy.scoring import scoring_dependencies, scoring_warmup_bars

    dependencies |= fields(basic_filter or {})
    scoring = (overrides or {}).get("scoring") or {}
    dependencies |= scoring_dependencies(scoring)
    # Optional overnight triggers reuse the ordinary daily signal dependency graph.
    from app.indicators.pipeline import LIMIT_SIGNAL_OUTPUTS, get_signal_dependencies
    names = {n for key in ("entry_signals", "exit_signals")
             for n in (overrides or {}).get(key, []) if isinstance(n, str) and not n.startswith("csgi_")}
    graph = get_signal_dependencies()
    unknown = names - set(graph) - set(LIMIT_SIGNAL_OUTPUTS)
    if unknown:
        raise ValueError("策略引用了不存在的信号: " + ", ".join(sorted(unknown)))
    for name in names:
        dependencies |= set(graph.get(name, ()))

    return ResolvedFeaturePlan(
        base_columns=MINUTE_DAILY_PANEL_COLUMNS | (dependencies - set(INDICATOR_COLUMNS)),
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(dependencies & set(INDICATOR_COLUMNS)),
        signal_columns=frozenset({
            "signal_limit_up", "signal_limit_down", "signal_broken_limit_up",
        } | names),
        matrix_columns=frozenset(),
        instrument_columns=MINUTE_INSTRUMENT_COLUMNS,
        warmup_bars=max(daily_bars, 60 if names else 1, scoring_warmup_bars(scoring),
                        overlay.warmup_bars(overlay.effective_conditions(overrides))),
        full_feature_fallback=False,
        execution_backend="polars_expr",
    )


def minute_panel_start(start: date, daily_bars: int) -> date:
    """日线面板加载起点: 覆盖首个回测日的 daily_bars 交易日窗口。

    N 个交易日约需 N*2 自然日 (周末/节假日), 再留 warmup 余量。
    """
    calendar_days = max(daily_bars, 1) * 2 + 30
    return start - timedelta(days=calendar_days)


def _trigger_hhmm(value) -> str:
    """从 last_datetime 提取北京时间 "HH:MM" 触发分钟。

    分区 naive datetime 遵循分钟库契约, 已是北京时间墙钟。仅 tz-aware
    值需要转换到北京时间。
    """
    from app.market_time import CN_TZ

    if hasattr(value, "astimezone"):
        if value.tzinfo is not None:
            value = value.astimezone(CN_TZ)
        return value.strftime("%H:%M")
    text = str(value or "")
    if len(text) >= 16 and text[13] == ":":
        return text[11:16]
    return text[-5:] if text else ""


def _scalar_limit_up_price(prev_close: float, limit_pct: float) -> float:
    """与 polars_limit_price 同口径的标量涨停价 (整数分半进位)。"""
    cents = int(prev_close * 100 + 0.5)
    numerator = round((1 + limit_pct) * 100)
    return ((cents * numerator + 50) // 100) / 100


def _signal_clock(value) -> str:
    """确认时间保留非零秒; 旧整分钟显示不变。"""
    from app.market_time import CN_TZ

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(CN_TZ)
        return value.strftime("%H:%M:%S" if value.second or value.microsecond else "%H:%M")
    return _trigger_hhmm(value)


def _executable_minute(value: datetime) -> datetime:
    """分钟成交事件向上对齐, 禁止14:31:30取14:31开盘。"""
    minute = value.replace(second=0, microsecond=0)
    return minute + timedelta(minutes=1) if value > minute else minute


@dataclass
class MinuteReplayHit:
    """盘中信号及其首个可执行分钟, 价格只作矩阵侧风险探针。"""

    trade_date: date
    symbol: str
    entry_window_start: datetime
    prior_close: float
    # 已按当日 复权close/原始close 比例折算到复权价系的入场价, 与日线出场价同尺度。
    entry_price: float
    trigger_time: str  # "HH:MM" 成交分钟; 兼容旧触发分钟收盘成交
    score: float = 0.0
    signal_time: str | None = None
    raw_entry_price: float | None = None
    entry_window_end: datetime | None = None
    entry_branch: str | None = None
    signal_score: float | None = None
    selection_rank: int | None = None


@dataclass
class MinuteReplayResult:
    hits: list[MinuteReplayHit] = field(default_factory=list)
    skipped_days: list[date] = field(default_factory=list)
    replayed_days: int = 0
    strategy_matches: int = 0
    buy_limit_up: int = 0
    buy_no_next_minute: int = 0
    elapsed_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    selection_provenance: list[dict] = field(default_factory=list)


def _s05_research_observation(repo, source, strict):
    """Bind ordinary research to retained inputs and strict research to S05's clock."""
    from app.market_time import CN_TZ, as_cn_datetime
    from app.services.selection_clock import S05_ID, window_for

    root = Path(repo.store.data_dir)
    public, research = root / ".public_release.json", root / ".research_snapshot.json"
    try:
        if public.is_file():
            manifest = json.loads(public.read_text())
            frozen_at = datetime.fromtimestamp(manifest["published_at"], UTC)
        elif research.is_file():
            manifest = json.loads(research.read_text())
            frozen_at = datetime.fromisoformat(manifest["created_at"])
        else:
            raise ValueError("S05回测需要冻结的公共行情或研究输入快照")
        identity = manifest["id"]
        if not isinstance(identity, str) or not identity:
            raise ValueError("S05冻结输入缺少身份")
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("S05冻结输入身份或观察时点无效") from exc
    cutoff = (datetime.combine(source, window_for(S05_ID)[0], CN_TZ)
              if strict else as_cn_datetime(frozen_at))
    return cutoff, {"input_snapshot_id": identity, "observation_policy": (
        "source_day_observed" if strict else "retained_research_snapshot")}


def requires_s03_scoring(strategy_engine, strategy, overrides):
    """Enabled A/B gates need five dimensions; old frozen signals use their original path."""
    from app.services.selection_clock import S03_ID
    from app.strategy.s01_signals import stored_definitions

    selected = set(overrides.get("entry_signals", strategy.entry_signals) or [])
    return strategy.meta["id"] == S03_ID and any(
        "csgi_" + sig["id"] in selected and sig.get("enabled") is not False
        and (sig.get("params") or {}).get("entry_groups") is not None
        for sig in stored_definitions(strategy_engine))


def requires_full_pool(strategy_engine, strategy, overrides) -> bool:
    from app.strategy import custom_signals
    from app.strategy.late_session_strength import TEMPLATE
    from app.strategy.s02_173004552 import enabled as routed_s02
    if routed_s02(strategy):
        return True

    requested = set(overrides.get("entry_signals", strategy.entry_signals) or []) | set(strategy.required_features)
    if not any(name.startswith("csgi_") for name in requested):
        return False
    storage = strategy_engine._user_data_dir()
    if storage is None:
        return False
    return any(sig.get("template") == TEMPLATE
               and custom_signals.intraday_column_name(sig["id"]) in requested
               for sig in custom_signals.load_intraday_all(storage))


class MinuteSignalReplayer:
    """逐交易日回放分钟策略, 产出与实盘选股同源的入场命中。"""

    def __init__(self, engine, strategy_engine: StrategyEngine) -> None:
        # engine: BacktestEngine — 只用其 repo (分钟分区读取)。
        self.engine = engine
        self.strategy_engine = strategy_engine

    def replay(
        self,
        strategy: StrategyDef,
        *,
        panel: pl.DataFrame,
        start: date,
        end: date,
        params: dict,
        overrides: dict,
        pool: list[str] | None = None,
        symbols: list[str] | None = None,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event=None,
        strict_research: bool = False,
        s05_selection_archive: dict | None = None,
    ) -> MinuteReplayResult:
        t0 = time.perf_counter()
        result = MinuteReplayResult()
        from app.strategy import s02_173004552 as routed
        from app.strategy.late_session_strength import execution_end
        from app.strategy.s01_signals import stored_definitions
        routed_s02 = routed.enabled(strategy)
        s05_scoring = strategy.meta.get("id") == routed.SID
        archived_s05 = s05_scoring and not routed_s02 and s05_selection_archive is not None
        requested_entries = set(overrides.get("entry_signals", strategy.entry_signals) or [])
        definitions = stored_definitions(self.strategy_engine)
        market_thresholds = (routed.market_thresholds(definitions, configured_only=routed_s02)
                             if s05_scoring and not archived_s05 else None)
        deadline_signals = {"csgi_" + sig["id"]: sig for sig in definitions
                            if sig.get("template") == "late_session_strength"
                            and "csgi_" + sig["id"] in requested_entries}
        repo = self.engine.repo
        if panel.is_empty():
            return result

        full_pool = requires_full_pool(self.strategy_engine, strategy, overrides)
        s03_scoring = requires_s03_scoring(self.strategy_engine, strategy, overrides)
        universe = (panel.get_column("symbol").unique().to_list()
                    if full_pool or not symbols else symbols)
        daily_bars = self.strategy_engine.minute_daily_history_bars(
            [strategy.meta["id"]], overrides_map={strategy.meta["id"]: overrides},
        )
        signal_sections = (
            strategy.execution_backend == "minute_filter"
            and strategy.meta.get("result_sections") == "signals"
            and not strategy.meta.get("minute_execution")
        )
        if signal_sections:
            daily_bars = max(daily_bars, 1)
        if s03_scoring:
            from app.services.selection_clock import S02_ID
            daily_bars = max(daily_bars, 65, self.strategy_engine.minute_daily_history_bars([S02_ID]))
        from app.strategy.basic_filter import uses_previous
        bf = {**(strategy.basic_filter or {}), **(overrides.get("basic_filter") or {})}
        if uses_previous(bf):
            daily_bars = max(daily_bars, 2)
        # 面板交易日序列 (升序) — 日线窗口切片与缺分区日判定的基准。
        panel_dates = panel.get_column("date").unique().sort().to_list()
        date_to_window: dict[date, tuple[date, date]] = {}
        for i, day in enumerate(panel_dates):
            window_start = panel_dates[max(0, i - daily_bars)] if daily_bars > 0 else day
            date_to_window[day] = (window_start, day)

        # 逐分区日回放: 只回放 [start, end] 内有分钟分区的交易日。
        minute_days = repo.list_minute_dates(start, end, "stock")
        minute_day_set = set(minute_days)
        replay_days = [day for day in panel_dates if start <= day <= end]
        result.skipped_days = [day for day in replay_days if day not in minute_day_set]
        total = len(minute_days)

        # T-1 原始收盘与 T 日复权因子查表。
        prev_raw_close: dict[str, float] = {}
        adj_factor: dict[str, float] = {}
        for i, day in enumerate(minute_days):
            if cancel_event is not None and cancel_event.is_set():
                break
            if progress_cb is not None:
                progress_cb({
                    "day": i + 1,
                    "total": max(total, 1),
                    "date": str(day),
                })

            history = repo.get_minute_by_dates(universe, [day], "stock")
            if history.is_empty():
                result.skipped_days.append(day)
                continue

            # 日线窗口: 截至 T-1 的完成态日K (index of last panel date < day)。
            prior = [d for d in panel_dates if d < day]
            if not prior:
                # 面板起点之前的分区日 (窗口数据不足), 策略按数据不足自然不命中。
                daily_history = pl.DataFrame()
                current = pl.DataFrame()
            else:
                last_prior = prior[-1]
                window_start, _ = date_to_window[last_prior]
                daily_history = panel.filter(
                    (pl.col("date") >= window_start) & (pl.col("date") <= last_prior)
                ) if daily_bars > 0 else pl.DataFrame()
                current = panel.filter(pl.col("date") == last_prior)

                prior_prices = current
                if "raw_close" not in prior_prices.columns:
                    prior_prices = prior_prices.with_columns(
                        pl.col("close").alias("raw_close")
                    )
                prev_raw_close.clear()
                prev_raw_close.update({
                    str(symbol): float(raw_close)
                    for symbol, raw_close in prior_prices.select(
                        "symbol", "raw_close"
                    ).iter_rows()
                    if raw_close is not None
                })

                # T 日复权因子查表。
                day_rows = panel.filter(pl.col("date") == day).select(
                    "symbol", "close", "raw_close",
                )
                adj_factor.clear()
                adj_factor.update(_adj_factors(day_rows))
                # Security eligibility is known at the start of T; price/volume
                # features still come exclusively from completed T-1 daily bars.
                eligibility_columns = [
                    name for name in (
                        "is_risk_warning", "instrument_status_known",
                        "is_listed", "listing_age_days",
                    ) if name in panel.columns
                ]
                if eligibility_columns:
                    current = current.drop(eligibility_columns).join(
                        panel.filter(pl.col("date") == day).select(
                            "symbol", *eligibility_columns,
                        ),
                        on="symbol", how="left",
                    )

            from app.instrument_suspension import pool_evidence
            from app.strategy.late_session_strength import pool_cutoff

            observed_at = pool_cutoff(self.strategy_engine, [strategy.meta["id"]],
                                      {strategy.meta["id"]: overrides}, day,
                                      datetime.combine(day, clock_time(15) if full_pool else clock_time.min))
            pool_history = current if not current.is_empty() else None
            if not current.is_empty() and "float_shares" in panel.columns:
                # Capital is effective on T; prices and volume remain completed T-1 inputs.
                capital = panel.filter(pl.col("date") == day).select("symbol", "float_shares")
                pool_history = current.select(
                    "symbol", *[name for name in ("name", "is_risk_warning",
                    "instrument_status_known", "is_listed", "listing_age_days") if name in current.columns],
                ).join(capital, on="symbol", how="left").with_columns(pl.lit(day).alias("date"))
            context = StrategyDataContext(
                asset_type="stock",
                timeframe="1m",
                as_of=day,
                current=current if not current.is_empty() else None,
                # Each signal enforces its own completed-minute cutoff. The
                # earliest pool publication clock must not remove later bars.
                history=history,
                daily_history=daily_history if not daily_history.is_empty() else None,
                pool_history=pool_history,
                pool_universe=pool_evidence(repo, day, observed_at=observed_at),
            )
            try:
                from app.services.daily_close_validation import validate_daily_close
                from app.strategy.basic_filter import previous_session

                source_day = previous_session(day)
                reference_errors = validate_daily_close(
                    repo, source_day, rows=panel.filter(pl.col("date") == source_day),
                    check_closing_minute=False, **({"isolate_symbols": True} if routed_s02 else {}),
                ) or {}
                if archived_s05:
                    archived_day = s05_selection_archive.get(str(day))
                    if archived_day is None:
                        raise ValueError("缺少S05冻结预选归档")
                    selection = archived_day["snapshot"]
                    routed.frozen_daily_branch(selection, day)
                    result.selection_provenance.append({
                        "as_of": str(day), "source_date": str(source_day),
                        "entry_branch": selection["entry_branch"], "branch_code": selection["branch_code"],
                        "coverage": selection.get("score_coverage") or {},
                        "scoring_provenance": selection.get("score_provenance") or {},
                        "archive": archived_day["evidence"], "signal_symbols": [],
                        "warnings": [*selection.get("warnings", []), *(
                            ["S05使用历史补算预选, 不代表盘后原时点已执行"]
                            if archived_day["evidence"].get("manual_replay") else [])],
                    })
                    context = replace(context, selection_snapshots={routed.SID: selection})
                elif signal_sections:
                    from app.strategy.signal_sections import select_rows
                    from app.strategy.suspension import annotate

                    selection_current = daily_history.filter(pl.col("date") == source_day)
                    if selection_current.is_empty():
                        raise ValueError(f"盘后预选缺少{source_day}完整行情")
                    if routed_s02:
                        selection_current = selection_current.filter(~pl.col("symbol").is_in(list(reference_errors)))
                        if selection_current.is_empty():
                            raise ValueError("S02隔离前日异常后无有效预选日线")
                    selection_current = annotate(
                        selection_current, as_of=source_day, evidence=pool_evidence(
                            repo, source_day, observed_at=datetime.combine(source_day, clock_time(16))),
                    ).with_columns(pl.lit(None, dtype=pl.Float64).alias("change_pct"))
                    selection_context = replace(
                        context, current=selection_current, history=None,
                        pool_history=None, pool_universe=None,
                    )
                    if s03_scoring:
                        from app.services.s03_selection import replay_selection
                        selection = replay_selection(
                            self.strategy_engine, selection_context, repo.store.data_dir,
                            source_day, overrides, strict=strict_research, repo=repo)
                        result.selection_provenance.append({
                            "as_of": selection["as_of"], "source_date": selection["source_date"],
                            "source_sha256": selection["source_selection"]["sha256"],
                            "coverage": selection["coverage"],
                            "scoring_provenance": selection["scoring_provenance"],
                            "warnings": selection["warnings"],
                        })
                    else:
                        if routed_s02:
                            from app.strategy.overnight_runtime import annotate_prior_ex_dates
                            selection_context = replace(selection_context, daily_history=annotate_prior_ex_dates(
                                daily_history.filter(~pl.col("symbol").is_in(list(reference_errors))), repo))
                        selected = select_rows(
                            self.strategy_engine, strategy, selection_context,
                            overrides={**overrides, "basic_filter": {
                                **overrides.get("basic_filter", {}),
                                "data_reference": "previous_trade_day",
                            }}, pool=None if s05_scoring else pool,
                        )
                        selection = {"as_of": str(day), "source_date": str(source_day), "rows": selected}
                        if s05_scoring:
                            from app.market_time import CN_TZ
                            from app.services.s05_scoring import compute_selection
                            from app.services.selection_clock import SELECTION_TIME
                            cutoff = datetime.combine(source_day, SELECTION_TIME, CN_TZ)
                            observation = {}
                            if not routed_s02:
                                cutoff, observation = _s05_research_observation(repo, source_day, strict_research)
                            selection = (compute_selection(repo, selection, source_day, day, cutoff)
                                         if routed_s02 else compute_selection(
                                             repo, selection, source_day, day, cutoff, include_entry_route=False))
                            if not routed_s02:
                                selection = {**selection, "entry_execution": "common_template_v1",
                                    "score_provenance": {**selection["score_provenance"], **observation},
                                    "warnings": [*selection.get("warnings", []), *([] if strict_research else [
                                        "S05研究重建使用冻结快照中的历史数据; 后来采集或修订的数据不证明当时可见, 非历史实盘归档"])]}
                            if market_thresholds is not None:
                                selection = routed.freeze_market_route(selection, day, market_thresholds, observed=cutoff)
                            result.selection_provenance.append({
                                "as_of": str(day), "source_date": str(source_day),
                                "entry_branch": selection["entry_branch"],
                                **({"branch_code": selection["branch_code"]} if not routed_s02 else {}),
                                "coverage": selection["score_coverage"],
                                "scoring_provenance": selection["score_provenance"],
                                "warnings": selection.get("warnings", []),
                            })
                            # Replay evaluates only the fixed completed bar and uses the same branch as live.
                            if routed_s02:
                                context = replace(context, independent_entry_phase="stocks")
                    context = replace(context, selection_snapshots={strategy.meta["id"]: selection})
                run_overrides = overrides
                if s05_scoring and not routed_s02:
                    branch = routed.frozen_daily_branch(selection, day)
                    if branch is None:
                        result.replayed_days += 1
                        continue
                    name = "csgi_" + routed.ENTRY_IDS[branch]
                    if archived_s05 and (name not in requested_entries or name not in deadline_signals
                                         or not deadline_signals[name].get("enabled")):
                        raise ValueError("S05冻结分支缺少已启用的入场信号")
                    run_overrides = {**overrides, "entry_signals": [name] if name in requested_entries else []}
                run_result = self.strategy_engine.run(
                    strategy.meta.get("id", ""),
                    context,
                    pool,
                    params,
                    run_overrides,
                )
            except ValueError as exc:
                # 单日执行失败 (如窗口缺列) 记为跳过, 不中断整个回放。
                result.skipped_days.append(day)
                result.errors.append(f"{day}: {exc}")
                continue

            if run_result.entry_filter_error:
                result.errors.append(f"{day}: {run_result.entry_filter_error}")
            section_errors = run_result.section_errors or {}
            result.errors.extend(
                f"{day}: {message}" for message in dict.fromkeys(
                    section_errors[key] for key in ("select", "buy") if section_errors.get(key))
            )
            if full_pool and not routed_s02:
                # 入场回放保持旧兼容; 新现金账本消费诊断, 不把未知广度当成成功零信号。
                from app.strategy import custom_signals
                from app.strategy.late_session_strength import (
                    TEMPLATE,
                    condition_inputs,
                    evaluate,
                    previous_closes,
                )

                if run_result.daily_stock_pool_error:
                    result.errors.append(f"{day}: {run_result.daily_stock_pool_error}")
                diagnostic_overrides = run_overrides if archived_s05 else overrides
                requested = (set(diagnostic_overrides.get("entry_signals", strategy.entry_signals) or [])
                             | set(strategy.required_features))
                for sig in custom_signals.load_intraday_all(self.strategy_engine._user_data_dir()):
                    if sig.get("template") == TEMPLATE and custom_signals.intraday_column_name(sig["id"]) in requested:
                        evaluated = evaluate(
                            history, sig, day=day,
                            pool_symbols=(run_result.daily_stock_pool or {}).get("symbols"),
                            prev_close=previous_closes(context),
                            selection=(context.selection_snapshots or {}).get(strategy.meta["id"]),
                            **condition_inputs(context, sig),
                        )
                        result.errors.extend(f"{day}: {msg}" for msg in evaluated.diagnostics
                                             if f"{day}: {msg}" not in result.errors)
            result.replayed_days += 1
            result.strategy_matches += len(run_result.rows)
            entries = overrides.get("entry_signals", strategy.entry_signals) or []
            if any(str(name).startswith("csgi_") for name in entries):
                triggered = {hit["symbol"] for hit in run_result.entry_signal_hits}
                run_result.rows = [row for row in run_result.rows if row.get("symbol") in triggered]
            if symbols:
                run_result.rows = [row for row in run_result.rows if row.get("symbol") in symbols]
            rankings = {}
            if archived_s05:
                from app.services.s05_signal_scores import buy_scores

                ranks = {row["symbol"]: row.get("s02_rank") for row in selection["rows"]}
                for row in buy_scores({"as_of": str(day), "rows": run_result.rows}, selection):
                    symbol = row["symbol"]
                    score, rank = row["signal_score"], ranks.get(symbol)
                    if score is None or type(rank) is not int or rank < 1:
                        result.errors.append(f"{day} {symbol} 缺少S05分支排序证据")
                        continue
                    rankings[symbol] = (score, rank)
                result.selection_provenance[-1]["signal_symbols"] = sorted(rankings)
            for row in run_result.rows:
                symbol = row.get("symbol")
                close = row.get("close")
                if (not symbol or close is None or float(close) <= 0
                        or (archived_s05 and symbol not in rankings)):
                    continue
                raw_close = float(close)
                trigger = row.get("last_datetime")
                available_at = row.get("entry_evaluated_at") or trigger
                signal_time = _signal_clock(available_at)
                if not isinstance(trigger, datetime):
                    result.buy_no_next_minute += 1
                    continue
                available = available_at if isinstance(available_at, datetime) else trigger
                entry_window_start = _executable_minute(
                    max(trigger + timedelta(minutes=1), available)
                )
                hit_signals = {name for hit in run_result.entry_signal_hits if hit["symbol"] == symbol
                               for name in hit.get("signals", [])}
                deadlines = [execution_end(sig, day) for name, sig in deadline_signals.items()
                             if name in hit_signals]
                entry_window_end = min(deadlines) if deadlines else None
                if routed_s02:
                    definition = next((sig for name, sig in deadline_signals.items()
                                       if name == "csgi_" + routed.ENTRY_IDS.get(
                                           row.get("entry_branch"), "")), None)
                    try:
                        if definition is None:
                            raise ValueError("S05回放缺少入场分支的冻结定义")
                        entry_window_start, entry_window_end = routed.entry_window(trigger, signal=definition)
                    except ValueError as exc:
                        result.errors.append(f"{day}: {exc}")
                        result.buy_no_next_minute += 1
                        continue
                if entry_window_end is not None and entry_window_start > entry_window_end:
                    result.buy_no_next_minute += 1
                    continue
                if entry_window_start.date() != day:
                    result.buy_no_next_minute += 1
                    continue
                if strategy.meta.get("minute_entry_fill") == "next_minute_open":
                    from app.backtest.entry_execution import expected_trading_minutes

                    window = expected_trading_minutes(
                        entry_window_start, count=240 if entry_window_end is not None else 5)
                    if entry_window_end is not None:
                        window = tuple(at for at in window if at <= entry_window_end)
                    executable = history.filter(
                        (pl.col("symbol") == symbol) & pl.col("datetime").is_in(window)
                        & pl.col("open").is_finite() & (pl.col("open") > 0)).sort("datetime")
                    if executable.is_empty():
                        result.buy_no_next_minute += 1
                        continue
                    raw_close = float(executable["open"][0])
                    if not math.isfinite(raw_close) or raw_close <= 0:
                        result.buy_no_next_minute += 1
                        continue
                prior_close = prev_raw_close.get(str(symbol))
                if prior_close is None or not math.isfinite(prior_close) or prior_close <= 0:
                    result.buy_no_next_minute += 1
                    continue
                trigger_time = _trigger_hhmm(entry_window_start)
                result.hits.append(MinuteReplayHit(
                    trade_date=day,
                    symbol=str(symbol),
                    entry_window_start=entry_window_start,
                    prior_close=prior_close,
                    entry_price=raw_close * adj_factor.get(str(symbol), 1.0),
                    trigger_time=trigger_time,
                    score=float(run_result.scores.get(str(symbol), 0.0) or 0.0),
                    signal_time=signal_time,
                    raw_entry_price=raw_close,
                    entry_window_end=entry_window_end,
                    entry_branch=(row.get("entry_branch") if routed_s02 else selection["entry_branch"])
                    if s05_scoring else None,
                    signal_score=rankings[symbol][0] if archived_s05 else None,
                    selection_rank=rankings[symbol][1] if archived_s05 else None,
                ))

        result.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result

def _adj_factors(day_rows: pl.DataFrame) -> dict[str, float]:
    """T 日 复权close/原始close 比例: 把分钟原始价折算到复权价系。"""
    factors: dict[str, float] = {}
    if day_rows.is_empty() or "raw_close" not in day_rows.columns:
        return factors
    for symbol, close, raw_close in day_rows.select("symbol", "close", "raw_close").iter_rows():
        if close and raw_close and float(raw_close) > 0:
            factors[str(symbol)] = float(close) / float(raw_close)
    return factors
