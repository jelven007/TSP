"""生成完整范围与冻结预选的采集提示; 优先采集不改变策略资格或信号成员。"""
import time

from app.services.daily_selections import DailySelections
from app.strategy.daily_pool import build_pool


def build_plan(engine, service, storage, ids, day, *, overrides, current):
    """完整池继续采集, 冻结预选子集另走受监督进程; 不改变任何策略资格。

    跨策略只合并采集代码并去重, selected 限于各策略池的并集;
    计划序号服务采集新旧判断, 不作为策略定义或成功计算身份。
    这里只生成计划; 采集器按owner接收并续期, 计划本身不证明分钟已经可用。
    """
    symbols, selected = set(), set()
    context = service.build_daily_pool_context(day, current=current) if ids else None
    for sid in ids:
        pool = build_pool(engine, sid, context, overrides=overrides.get(sid))
        symbols.update(pool["symbols"])
        frozen = DailySelections(storage).read(sid, day)
        if frozen:
            selected.update(row["symbol"] for row in frozen["rows"])
    return {"day": str(day), "sequence": time.time_ns(),
            "symbols": sorted(symbols), "selected": sorted(selected & symbols)}
