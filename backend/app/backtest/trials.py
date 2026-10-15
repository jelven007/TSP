"""Research provenance attached to the existing optimization/job result."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date

import numpy as np

from app.backtest.stats_v2 import deflated_sharpe_psr, expected_max_sharpe


def config_snapshot(config) -> dict:
    """Detach nested configuration and encode dates for persisted job results."""
    return json.loads(json.dumps(asdict(config), default=lambda v: v.isoformat() if isinstance(v, date) else v,
                                 sort_keys=True, ensure_ascii=False, allow_nan=False))


def family_record(config, kind: str) -> dict:
    snapshot = config_snapshot(config)
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"schema_version": 1, "family_id": digest, "kind": kind, "config": snapshot}


def selection_correction(rows: list[dict], *, objective: str, complete: bool) -> dict:
    """DSR for a completed Sharpe search over actual daily portfolio returns.

    Scope is this grid only. Raw trial count uses the independent-trial
    approximation; correlated parameter trials and earlier searches are not
    estimated or silently presented as independent evidence.
    """
    out = {
        "scope": "current_grid", "method": "deflated_sharpe_psr",
        "trial_count_basis": "actual_completed_grid",
        "dependence_assumption": "independent_trial_approximation",
        "return_basis": "daily_portfolio", "annualized": False,
        "n_trials": len(rows), "probability": None, "expected_max_sharpe": None,
    }

    def unavailable(status: str) -> dict:
        return {**out, "status": status}

    if not complete or any(row.get("status") != "succeeded" for row in rows):
        return unavailable("incomplete_search")
    if objective != "sharpe":
        return unavailable("objective_not_sharpe")
    if len(rows) < 2:
        return unavailable("insufficient_trials")
    moments = [row.get("stats", {}).get("return_moments", {}) for row in rows]
    if any(m.get("status") != "available" or m.get("basis") != "daily_portfolio" for m in moments):
        return unavailable("insufficient_return_samples")
    if len({m["n_obs"] for m in moments}) != 1:
        return unavailable("inconsistent_return_samples")
    sharpes = [m["sharpe"] for m in moments]
    variance = float(np.var(sharpes, ddof=1))
    if not np.isfinite(variance) or variance <= 0:
        return unavailable("degenerate_trial_variance")
    threshold = expected_max_sharpe(len(rows), variance)
    # Use the selected row and its unrounded, nonannualized Sharpe and moments.
    best = moments[0]
    probability = deflated_sharpe_psr(
        best["sharpe"], best["n_obs"], best["skewness"], best["kurtosis"], threshold,
    )
    if probability is None or not np.isfinite(probability):
        return unavailable("degenerate_return_moments")
    return {
        **out, "status": "available", "probability": probability,
        "expected_max_sharpe": threshold, "variance_sharpes": variance, "n_obs": best["n_obs"],
    }
