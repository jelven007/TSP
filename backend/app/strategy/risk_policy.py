"""Pure price policy shared by historical matching and forward paper execution."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RiskPolicy:
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    trailing_take_profit_activate_pct: float | None = None
    trailing_take_profit_drawdown_pct: float | None = None
    max_hold_days: int | None = None

    @classmethod
    def from_mapping(cls, values: dict) -> RiskPolicy:
        parsed = {}
        for key in cls.__dataclass_fields__:
            value = values.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"风控参数 {key} 必须是有限数值")
            if key == "max_hold_days":
                if value <= 0 or int(value) != value:
                    raise ValueError("max_hold_days 必须是正整数")
                parsed[key] = int(value)
            else:
                parsed[key] = abs(float(value))
        return cls(**parsed)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_pct(value, minimum: float, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        value = abs(float(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, minimum), maximum)


def strategy_policy(strategy, overrides: dict) -> RiskPolicy:
    values = {}
    fields = {
        "stop_loss": ("stop_loss_pct", None),
        "take_profit": ("take_profit_pct", (.01, 5.0)),
        "trailing_stop": ("trailing_stop_pct", (.005, .5)),
        "trailing_take_profit_activate": ("trailing_take_profit_activate_pct", (.01, 2.0)),
        "trailing_take_profit_drawdown": ("trailing_take_profit_drawdown_pct", (.005, .5)),
        "max_hold_days": ("max_hold_days", None),
    }
    for source, (target, bounds) in fields.items():
        value = overrides[source] if source in overrides else getattr(strategy, source, None)
        values[target] = normalize_pct(value, *bounds) if bounds else value
    policy = RiskPolicy.from_mapping(values)
    activate, drawdown = policy.trailing_take_profit_activate_pct, policy.trailing_take_profit_drawdown_pct
    if activate is not None and drawdown is not None and drawdown > activate:
        raise ValueError(f"移动止盈回撤({drawdown:.0%})不能大于激活涨幅({activate:.0%}), 请调整后再试")
    return policy


def valid_price(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (float, int))
            and math.isfinite(value) and value > 0)


def price_exit(policy, entry: float, peak: float, opening: float,
               low: float, high: float) -> tuple[str | None, float | None]:
    """Evaluate using the peak known BEFORE this observation/bar.

    Highest protective line wins; gaps use the actual open. Historical OHLC
    assumes protection precedes profit when both are touched in one bar.
    Forward callers pass an observed raw price for all three price arguments.
    """
    if not valid_price(entry):
        return None, None
    lines = []
    if policy.stop_loss_pct is not None:
        lines.append((entry * (1 - abs(policy.stop_loss_pct)), "stop_loss"))
    if policy.trailing_stop_pct is not None and valid_price(peak):
        lines.append((peak * (1 - abs(policy.trailing_stop_pct)), "trailing_stop"))
    activate = policy.trailing_take_profit_activate_pct
    drawdown = policy.trailing_take_profit_drawdown_pct
    if (activate is not None and drawdown is not None and peak > entry
            and peak / entry - 1 >= abs(activate)):
        lines.append((peak * (1 - abs(drawdown)), "trailing_take_profit"))
    lines = [(line, reason) for line, reason in lines if valid_price(line)]
    if lines:
        line, reason = max(lines, key=lambda item: item[0])
        if valid_price(opening) and opening <= line:
            return reason, opening
        if valid_price(low) and low <= line:
            return reason, line
    if policy.take_profit_pct is not None:
        line = entry * (1 + abs(policy.take_profit_pct))
        if valid_price(opening) and opening >= line:
            return "take_profit", opening
        if valid_price(high) and valid_price(line) and high >= line:
            return "take_profit", line
    return None, None
