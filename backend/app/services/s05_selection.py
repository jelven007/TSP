"""Freeze S02's source archive and run S05's independent four-dimension scorer."""
from __future__ import annotations

from datetime import date, datetime

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


def nightly_entry_symbols(snapshot, target, *, archived_at_ms=None):
    """Return only the live buy cohort from the previous session's evening archive."""
    from app.services.daily_selections import valid_selection
    from app.services.selection_clock import window_for

    target = date.fromisoformat(str(target))
    error = f"等待{target}的S05前一交易日23:00前正式盘后预选"
    if not valid_selection(snapshot, target, strategy_id=S05_ID) or not snapshot.get("identity"):
        raise ValueError(error)
    dependency = snapshot.get("source_selection") or {}
    if (snapshot.get("manual_replay") or not isinstance(dependency, dict)
            or dependency.get("historical_replay")):
        raise ValueError(error)
    try:
        source = date.fromisoformat(snapshot["source_date"])
        generated = datetime.fromisoformat(snapshot["generated_at"])
        if generated.tzinfo is None:
            raise ValueError
        generated = as_cn_datetime(generated)
        start, stop = (datetime.combine(source, clock, generated.tzinfo) for clock in window_for(S05_ID))
        if not start <= generated < stop:
            raise ValueError
        if archived_at_ms is not None:
            saved_at = datetime.fromtimestamp(archived_at_ms / 1000, tz=generated.tzinfo)
            if not generated <= saved_at < stop:
                raise ValueError
        symbols = [row["symbol"] for row in snapshot["rows"]]
        if not all(isinstance(symbol, str) and symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise ValueError(error) from None
    return symbols


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
