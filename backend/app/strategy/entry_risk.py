"""Opt-in entry controls; all prices in one consistent adjustment scale.

ATR and structural low belong to the confirmed SIGNAL bar, including for
two-stage entries. This is not the earlier pattern bar's structural stop.
Risk sizing caps planned price loss, not gap/T+1 losses or execution costs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.strategy.risk_policy import valid_price

ENTRY_RISK_VERSION = "signal_reference_risk_v1"


@dataclass(frozen=True)
class EntryRiskPolicy:
    max_entry_premium_pct: float | None = None
    stop_atr_multiple: float | None = None
    stop_signal_low: bool = False
    risk_per_trade_pct: float | None = None

    @classmethod
    def from_mapping(cls, values=None) -> EntryRiskPolicy:
        if values is None:
            return cls()
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("入场风险配置包含未知参数或格式无效")
        result = cls(**values)
        if not isinstance(result.stop_signal_low, bool):
            raise ValueError("冻结信号日低点必须为开启或关闭")
        for key, maximum, label in (
            ("max_entry_premium_pct", 1., "成交溢价上限必须大于0且不超过100%"),
            ("stop_atr_multiple", 10., "信号日ATR止损倍数必须大于0且不超过10"),
            ("risk_per_trade_pct", .05, "单笔计划风险必须大于0且不超过权益的5%"),
        ):
            value = getattr(result, key)
            if value is not None and (not valid_price(value) or value > maximum):
                raise ValueError(label)
        return result

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def enabled(self) -> bool:
        return (self.max_entry_premium_pct is not None or self.stop_atr_multiple is not None
                or self.stop_signal_low or self.risk_per_trade_pct is not None)

    def freeze(self, entry: float, *, close=None, low=None, atr=None,
               fixed_stop_pct=None) -> tuple[float | None, str | None]:
        """Return a frozen protective line or a deterministic rejection code."""
        if not self.enabled:
            return None, None
        if not valid_price(entry):
            return None, "buy_invalid_price"
        if self.max_entry_premium_pct is not None:
            if not valid_price(close):
                return None, "buy_missing_risk_reference"
            if entry > close * (1 + self.max_entry_premium_pct) + 1e-7:
                return None, "buy_entry_premium"
        lines = []
        if self.stop_atr_multiple is not None:
            if not valid_price(atr):
                return None, "buy_missing_risk_reference"
            lines.append(entry - self.stop_atr_multiple * atr)
        if self.stop_signal_low:
            if not valid_price(low):
                return None, "buy_missing_risk_reference"
            lines.append(low)
        if any(not valid_price(line) or line >= entry for line in lines):
            return None, "buy_invalid_stop_reference"
        frozen = max(lines) if lines else None
        if self.risk_per_trade_pct is not None and self.protective_line(entry, frozen, fixed_stop_pct) is None:
            return None, "buy_missing_risk_reference"
        return frozen, None

    @staticmethod
    def protective_line(entry, frozen, fixed_stop_pct):
        lines = [frozen] if valid_price(frozen) else []
        if fixed_stop_pct is not None:
            line = entry * (1 - abs(fixed_stop_pct))
            if valid_price(line) and line < entry:
                lines.append(line)
        return max(lines) if lines else None

    def cap_notional(self, equity, entry, frozen, fixed_stop_pct) -> float:
        if self.risk_per_trade_pct is None:
            return float("inf")
        line = self.protective_line(entry, frozen, fixed_stop_pct)
        if line is None or not valid_price(equity) or line >= entry:
            return 0.
        return equity * self.risk_per_trade_pct * entry / (entry - line)


def reference_from_row(row: dict) -> dict:
    """Freeze forward execution references in the signal day's RAW scale."""
    close, raw = row.get("close"), row.get("raw_close")
    if not valid_price(close) or not valid_price(raw):
        return {}
    scale = raw / close
    return {
        "close": raw,
        "low": row["low"] * scale if valid_price(row.get("low")) else None,
        "atr": row["atr_14"] * scale if valid_price(row.get("atr_14")) else None,
    }
