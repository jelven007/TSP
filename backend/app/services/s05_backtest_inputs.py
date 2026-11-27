"""Freeze the saved S05 selection inputs for a new system backtest."""
from datetime import date, timedelta

from app.market_time import published_trading_day
from app.services.daily_selections import DailySelections, selection_record
from app.services.s05_selection import COMMON_BRANCH_POLICY, freeze_source
from app.services.selection_clock import S05_ID
from app.strategy.version import digest


def freeze_backtest_selections(session, start, end, *, strict=False):
    """Bind the exact selection slot and its status projection, never archived buys."""
    from app.services.custom_status_refresh import selection

    store = DailySelections(session).results
    frozen = {}
    missing = []
    day, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    while day <= end:
        trading = published_trading_day(day)
        if trading is None:
            raise ValueError(f"{day} 缺少已确认的交易日历")
        if trading:
            saved = selection_record(S05_ID, day, lambda key: store.detail(S05_ID, key),
                                     policy=COMMON_BRANCH_POLICY)
            if saved is None:
                missing.append(str(day))
            else:
                snapshot = saved["result"]["selection_snapshot"]
                source = snapshot.get("source_selection") or {}
                manual = snapshot.get("manual_replay")
                if strict and manual:
                    raise ValueError(f"{day} S05预选为历史补算, 不能证明盘后原时点可见")
                if (snapshot.get("entry_execution") != "common_template_v1"
                        or not snapshot.get("identity") or not source.get("sha256")
                        or source != freeze_source(
                            source.get("snapshot"), date.fromisoformat(snapshot["source_date"]), day,
                            historical_replay=source.get("historical_replay"))):
                    raise ValueError(f"{day} S05预选归档的S02来源摘要不匹配")
                effective = selection(store, S05_ID, snapshot)
                status = effective.get("status_refresh") or {}
                frozen[str(day)] = {
                    # The S02 rows certify the source digest above; only S05 rows enter minute replay.
                    "snapshot": {k: v for k, v in effective.items()
                                 if k not in {"source_selection", "status_refresh"}},
                    "evidence": {
                        "record_id": saved["record_id"], "selection_sha256": digest(snapshot),
                        "source_strategy_id": source["strategy_id"], "source_sha256": source["sha256"],
                        "source_candidate_count": len(source["snapshot"]["rows"]),
                        "status_digest": status.get("evidence_digest"),
                        "manual_replay": bool(manual), "selected_symbols": [
                            row["symbol"] for row in effective["rows"]],
                    },
                }
        day += timedelta(days=1)
    if missing:
        raise ValueError(f"S05缺少当前口径的盘后预选归档 ({len(missing)}日): {', '.join(missing[:8])}")
    return frozen
