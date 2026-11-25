"""按源码元数据绑定执行排序: 当前S03为A/B资金, 历史冻结声明分别兼容。

只复制执行快照, 不写回归档; 五维成员与selection_rank在本层不重新计算。
"""
from __future__ import annotations

import math

POLICY = "s03_selection_atr_desc_v1"
FUND_POLICY = "s03_ab_fund_v1"
FIVE_DIMENSION_ROWS = "s03_five_dimension_rows_v1"


def atr_key(row):
    """历史ATR口径的键: 有效非负比例降序, 同值代码升序, 未知值最后。"""
    value = row.get("atr14_previous_close")
    valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(value) and value >= 0)
    return (-float(value) if valid else math.inf, str(row.get("symbol", "")))


def for_strategy(selection, meta):
    """在私有快照绑定受支持的源码声明, 拒绝沿用归档中的其他排序标记。

    当前资金声明保留五维选名单顺序, 买入排序由entry_groups.match完成;
    显式历史ATR声明只调整行顺序, 不改变归档成员、原排名或分数。
    """
    if selection is None:
        return None
    copied = {key: value for key, value in selection.items() if key != "selection_ordering"}
    policy = meta.get("selection_ordering")
    if policy in (POLICY, FUND_POLICY):
        copied["selection_ordering"] = policy
    if policy == FUND_POLICY and meta.get("selection_projection") == FIVE_DIMENSION_ROWS:
        # 不将源策略专用字段带入本次五维结果; 原冻结输入继续保留。
        copied["rows"] = [{key: value for key, value in row.items() if not key.startswith("atr")}
                          for row in selection["rows"]]
    elif policy == POLICY:
        copied["rows"] = sorted(selection["rows"], key=atr_key)
    return copied
