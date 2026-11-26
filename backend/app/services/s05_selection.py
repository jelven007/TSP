"""Freeze S02's source archive and run S05's independent four-dimension scorer."""
from __future__ import annotations

from app.market_time import as_cn_datetime
from app.services import s05_scoring
from app.services.s05_scoring import POLICY, WEIGHTS
from app.services.s05_scoring import freeze_selection as freeze_selection
from app.services.selection_clock import S05_ID
from app.strategy.s02_173004552 import freeze_market_route

ROUTED_POLICY = POLICY + "_route_v2"
NUMERIC_POLICY = POLICY + "_branch_v3"
COMMON_POLICY = POLICY + "_common_v1"
COMMON_BRANCH_POLICY = POLICY + "_common_branch_v1"


def freeze_source(snapshot, source, target, *, historical_replay=None):
    """Share S03's strict S02 archive clock, identity and digest contract."""
    if historical_replay is not None:
        import hashlib
        import json
        from datetime import datetime

        from app.services.daily_selections import valid_selection
        from app.services.s05_task_replay import validate_replay
        from app.services.selection_clock import S02_ID

        validate_replay(historical_replay, target, [S05_ID])
        if (not valid_selection(snapshot, target)
                or snapshot.get("source_date") != str(source) or not snapshot.get("identity")):
            raise ValueError("S05历史补算S02归档日期、身份或有效证据不匹配")
        try:
            scheduled = as_cn_datetime(datetime.fromisoformat(snapshot["scheduled_for"]))
            as_cn_datetime(datetime.fromisoformat(snapshot["generated_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("S05历史补算S02归档缺少原计划或生成时间") from exc
        if scheduled.date() != source:
            raise ValueError("S05历史补算S02原计划日期不匹配")
        frozen = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
        encoded = json.dumps(frozen, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return {"strategy_id": S02_ID, "snapshot": frozen,
                "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "historical_replay": json.loads(json.dumps(historical_replay))}
    from app.services.s03_selection import freeze_source as validate

    try:
        return validate(snapshot, source, target)
    except ValueError as exc:
        raise ValueError(str(exc).replace("S03", "S05")) from exc


def compute_selection(service, source, target, dependency, now, *, thresholds=None, common=False,
                      historical_replay=None):
    """Validate frozen input, retain routed entry fields and actual observation."""
    from app.services.daily_pool_schedule import scheduled_for
    from app.services.selection_clock import window_for

    if not dependency:
        raise ValueError("等待S02的21:00成功预选归档")
    validated = freeze_source(dependency.get("snapshot"), source, target,
                              historical_replay=historical_replay)
    if dependency != validated:
        raise ValueError("S05冻结S02依赖摘要不匹配")
    snapshot = validated["snapshot"]
    observed = as_cn_datetime(now)
    if snapshot["rows"] or thresholds is not None:
        ranked = s05_scoring.compute_selection(service.repo, snapshot, source, target, observed,
                                               include_entry_route=thresholds is not None and not common)
    else:
        coverage = {
            "candidate_total": 0, "scorable_total": 0, "ranked_total": 0,
            "invalid_symbols": {}, "missing_components": {}}
        ranked = {**snapshot, "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
                  "score_coverage": coverage, "coverage": {
                      **(snapshot.get("coverage") or {}), **coverage, "schema": POLICY, "selected_total": 0},
                  "score_provenance": {"scoring_implementation": s05_scoring.IMPLEMENTATION,
                                       "observed_cutoff": observed.isoformat()}}
    # The S02 identity belongs exclusively to source_selection. DailySelections
    # binds this new snapshot to the saved S05 definition when committing it.
    ranked.pop("identity", None)
    if thresholds is None or common:
        ranked = {key: value for key, value in ranked.items()
                  if key not in ("entry_routing", "entry_branch", "entry_route", "branch_code")}
    result = {**ranked, "as_of": str(target), "source_date": str(source),
              "generated_at": observed.isoformat(),
              "scheduled_for": scheduled_for(source, window_for(S05_ID)[0].strftime("%H:%M")),
              "source_selection": validated}
    if thresholds is None or common:
        result["entry_execution"] = "common_template_v1"
    if historical_replay is not None:
        result["manual_replay"] = historical_replay
    return (freeze_market_route(result, target, thresholds, observed=observed)
            if thresholds is not None else result)
