"""六周期 K 线修正; 全量复用原管道/周期同步, 遗漏模式按记录键补采。

典型场景: 昨天没看盘 / 服务挂了一天,本地日K缺了若干天。

全量模式不重复盘后管道逻辑, 直接调用 daily_pipeline.run_now(),
通过 override_start_date/override_end_date 参数使用用户指定日期区间,
读取已有维表股票池, 其余 (A股日K / 除权因子 / enriched / 指数 / ETF / 错误兜底) 全部原样复用。
遗漏模式独立检测实际缺少的记录, 复用原Provider与仓库写入契约。

全量落盘是 merge-upsert (按 symbol+date 去重 keep="last"), 新数据覆盖旧值,
不会产生重复。日K/指数/ETF 族无需先删分区; 股票 enriched 例外 — 增量重算
只算 enriched 里不存在的日期, "分区已存在但内容是盘中快照"的场景由管道内
的 prune_enriched_partitions 先删坏分区再重算 (run_now 内处理, 此处无需关心)。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from app.market_time import cn_today
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def run_repair_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    start_date: date,
    end_date: date | None = None,
    on_progress: Callable | None = None,
    quote_service=None,
    *,
    period: str = "1d",
    mode: str = "full",
) -> dict:
    """修正 / 补全数据, 日期范围由用户指定; 旧调用默认全量日K。

    通过 run_now 的日期覆盖参数统一日K/除权/指数的拉取范围;
    未填写结束日期时到今天, 复用既有盘后管道。

    Args:
        repo:          数据仓库
        capset:        权限集
        start_date:    用户选定的起始日期
        end_date:      两种模式的结束日期(包含当天); 缺省今天。
        on_progress:   进度回调
        period:        六周期之一; 5m/10m/15m只读取本地1m, 周K只读取本地日K。
        mode:          full重拉选定区间; missing只补缺少的证券/时间记录, 不覆盖已有值。
        quote_service: 实时行情服务; 只在其真正写盘/删分区的临界区暂停轮询,
                       取数与计算阶段保持实时行情正常运行。

    Returns:
        run_now() 的完整结果 dict。
    """
    today = cn_today()
    if start_date > today:
        return {"error": "起始日期不能晚于今天"}
    range_end = end_date if end_date is not None else today
    if range_end < start_date:
        return {"error": "结束日期不能早于起始日期"}
    if range_end > today:
        return {"error": "结束日期不能晚于今天"}

    from app.jobs.daily_pipeline import run_now
    from app.services import instrument_sync, kline_periods
    from app.services.realtime_guard import paused_quotes

    if period not in kline_periods.TABLES:
        return {"error": "不支持的K线周期"}
    if mode not in ("full", "missing"):
        return {"error": "不支持的数据获取方式"}

    with paused_quotes(quote_service):
        try:
            if mode == "missing":
                from app.services.kline_gaps import sync_missing
                return sync_missing(repo, period, start_date, range_end, on_progress=on_progress)
            if period != "1d":
                snapshot = instrument_sync.load_current_stock_pool(repo.store.data_dir, require_complete=True)
                # 周K以周一入库, 周中起始日期也必须包含所在周的整根K线。
                period_start = start_date - timedelta(days=start_date.weekday()) if period == "1w" else start_date
                result = kline_periods.sync_period(
                    repo, period, datetime.combine(period_start, time.min),
                    datetime.combine(range_end, time.max), symbols=list(snapshot.symbols),
                    on_progress=on_progress,
                )
                if result["rows"] == 0:
                    result["error"] = (
                        "选定区间没有可计算的本地日K, 请先修正日K后重试"
                        if period in kline_periods.LOCAL_DAILY_PERIODS
                        else "选定区间没有可计算的本地1分K, 请先修正1分K后重试"
                        if period in kline_periods.LOCAL_MINUTE_PERIODS
                        else f"数据源未返回选定区间的{kline_periods.LABELS[period]}数据"
                    )
                return result
            return run_now(
                repo, capset, on_progress=on_progress, override_start_date=start_date,
                **({"override_end_date": end_date} if end_date is not None else {}),
            )
        finally:
            # 修正会重写今日 enriched 分区。不重建内存缓存的话, 行情/选股器会继续
            # 返回修复前的盘中旧快照 (与盘后管道 _pipeline_then_refresh 对齐:
            # 即使阶段软失败, 已落盘的数据也要刷进内存)。
            try:
                repo.refresh_cache()
            except Exception:
                logger.warning("repair_daily: refresh cache failed", exc_info=True)
            # 行情已重写但非交易时段没有 quotes_updated, 前端不会失效行情查询。
            # 主动广播一次走既有 SSE 链路 (平台 marker → 网关 → 前端 invalidation),
            # 避免用户必须手动刷新才能看到修正后的数据。
            if quote_service is not None:
                try:
                    quote_service._broadcast_quote_updated()
                except Exception:
                    logger.warning("repair_daily: broadcast quote update failed", exc_info=True)
