"""盘后资格的唯一时钟入口, 供调度、修复期限和归档计划共同引用。

窗口属于完成日线的源日, 资格归属由调用方解析为下一交易日。
window_for/window_text仅提供时钟, 不证明数据已定版或任务已完成。
"""
from datetime import time

SELECTION_HOUR = 21
SELECTION_TIME = time(SELECTION_HOUR)
SELECTION_TIME_TEXT = "21:00"
S02_ID = "custom_mvxxcp88"
S03_ID = "custom_mvxxcs03"
S05_ID = "custom_mvxxcs05"


def selection_group(strategy_id):
    """Dependent strategies have independent receipts inside their own windows."""
    return strategy_id if strategy_id in (S03_ID, S05_ID) else None


def job_key(target, strategy_id=None):
    suffix = {S03_ID: ":s03", S05_ID: ":s05"}.get(strategy_id, "")
    return str(target) + suffix


def window_for(strategy_id=None):
    """北京时间窗口含结束整秒: 普通策略21~22点, S03及S05预选22~23点。"""
    return (time(22), time(23)) if strategy_id in (S03_ID, S05_ID) else (SELECTION_TIME, time(22))


def window_text(strategy_id=None):
    """输出规范 HH:MM:SS, 供监控计划与归档使用同一窗口。"""
    return tuple(value.strftime("%H:%M:%S") for value in window_for(strategy_id))
