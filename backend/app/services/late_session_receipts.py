"""在归档边界保留真实首份子步骤证据, 与最近一次展示指标分开。"""
from copy import deepcopy


def _actual(step):
    """正常执行且有计数与两端时钟才算真实回执; 状态字符串本身不足为证。"""
    return (step.get("state") in ("success", "rejected")
            and step.get("processed") is not None
            and bool(step.get("started_at")) and bool(step.get("finished_at")))


def preserve_first_receipts(current, previous=None):
    """仅修改current, 按同日/启动分钟/对象/参数/计划时刻保留首份回执。

    正常首份证据使用深拷贝; 旧归档已正常执行却未保存first_receipt时写None,
    表示最早证据未知, 不能把后续计算冒充首次。失败/等待不造正常首份证据。
    """
    previous = previous or {}
    for signal, quality in (current.get("entry_data_quality") or {}).items():
        progress = quality.get("progress") or {}
        if (progress.get("schema") != "late_session_progress_v1"
                or progress.get("as_of") != current.get("as_of")):
            continue
        prior = ((previous.get("entry_data_quality") or {}).get(signal) or {}).get("progress") or {}
        same_scope = (previous.get("as_of") == current.get("as_of")
                      and all(prior.get(key) == progress.get(key) for key in
                              ("schema", "as_of", "bar_at", "cohort", "params")))
        for key, step in progress.get("steps", {}).items():
            old = (prior.get("steps") or {}).get(key) or {}
            same_step = same_scope and old.get("scheduled_at") == step.get("scheduled_at")
            step.pop("first_receipt", None)
            if same_step and "first_receipt" in old:
                step["first_receipt"] = deepcopy(old["first_receipt"])
            elif same_step and _actual(old):
                # 旧归档可能多次覆盖, 现存正常值不足以证明它就是首次执行。
                step["first_receipt"] = None
            elif _actual(step):
                step["first_receipt"] = deepcopy(step)
