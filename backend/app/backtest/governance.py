"""Quality gates for formal stock research, without inventing historical status."""
from datetime import date

import numpy as np
import polars as pl

from app.backtest.costs import COST_MODEL_VERSION, SINGLE_SIDED_START
from app.backtest.matrix import MarketDataMatrix

STATUS_FIELDS = frozenset({"instrument_status_known", "is_risk_warning", "is_listed"})


def status_quality(
    data: pl.DataFrame | MarketDataMatrix, start: date, end: date,
) -> dict:
    if isinstance(data, pl.DataFrame):
        frame = data.filter(pl.col("date").is_between(start, end))
        total = frame.height
        known = np.ones(total, dtype=bool)
        for field in STATUS_FIELDS:
            if field not in frame.columns:
                known[:] = False
            else:
                values = frame[field].cast(pl.Float64).to_numpy()
                known &= np.isfinite(values)
                if field == "instrument_status_known":
                    known &= values >= 0.5
        unknown = int((~known).sum())
    else:
        times = np.array([start.isoformat() <= label[:10] <= end.isoformat() for label in data.timestamp_labels])
        valid = data.tradable[times]
        total = int(valid.sum())
        known = np.ones(valid.shape, dtype=bool)
        for field in STATUS_FIELDS:
            values = data.fields.get(field)
            if values is None:
                known[:] = False
            else:
                selected = values[times]
                known &= np.isfinite(selected)
                if field == "instrument_status_known":
                    known &= selected >= 0.5
        unknown = int((valid & ~known).sum())
    return {
        "observations": total,
        "unknown_status_observations": unknown,
        "status_coverage": (total - unknown) / total if total else None,
        "historical_status_complete": bool(total and not unknown),
    }


def research_preflight(config, provenance: dict, quality: dict | None = None) -> list[str]:
    """Warnings remain visible in exploratory runs; strict runs fail closed."""
    issues = []
    if not provenance.get("release_consistent"):
        issues.append("当前数据尚未纳入一致的数据发布版本")
    if config.asset_type == "stock":
        if config.stamp_tax_pct is not None and config.stamp_tax_pct <= 0:
            issues.append("股票卖出印花税为零")
        if config.stamp_tax_pct is None and config.start < SINGLE_SIDED_START:
            issues.append("历史印花税仅覆盖 2008-09-19 起的区间")
        if quality is not None and not quality["historical_status_complete"]:
            issues.append(f"历史证券状态不足: {quality['unknown_status_observations']} 条观测缺少可验证的 ST/上市状态")
    provenance["research_governance"] = {
        "strict": config.strict_research,
        "cost_model_version": COST_MODEL_VERSION,
        "status_quality": quality,
        "issues": issues,
        # This gate covers status and cost; it does not certify survivorship or
        # immutable input bytes (those require the research snapshot stage).
        "status_and_cost_gate_passed": bool(quality is not None and not issues),
    }
    return issues
