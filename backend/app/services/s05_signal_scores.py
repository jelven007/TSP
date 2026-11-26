"""S05 daily display scores. Execution scores and archived signals stay intact."""
from datetime import date, datetime, timedelta
from math import isfinite

from app.market_time import as_cn_datetime

RULES = {1: "return_5m", 2: "fund_tech", 3: "four_dimension"}
MINUTE_NAMESPACE = "s05_signal_score_minutes"


def number(value):
    return type(value) in (int, float) and isfinite(value)


def minute_key(row, day):
    try:
        value = row.get("last_datetime") or row.get("datetime")
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        stamp = as_cn_datetime(stamp).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
    if stamp.date() != day or stamp.second or stamp.microsecond:
        return None
    return row["symbol"], stamp


def minute_returns(rows, history, day):
    """Use the completed trigger minute and exactly five minutes earlier."""
    if history is None or not {"symbol", "datetime", "close"} <= set(history.columns):
        return {}
    import polars as pl

    keys = {key for row in rows if (key := minute_key(row, day)) is not None}
    if not keys:
        return {}
    earlier = timedelta(minutes=5)
    needed = keys | {(symbol, stamp - earlier) for symbol, stamp in keys}
    moments = {stamp for _, stamp in needed}
    dt = pl.col("datetime")
    if history.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    # Match the unadjusted minute price used for the entry trigger when available.
    prices = {(symbol, stamp): price for symbol, stamp, price in history.select(
        "symbol", dt, "raw_close" if "raw_close" in history.columns else "close",
    ).filter(pl.col("datetime").is_in(list(moments))).iter_rows() if (symbol, stamp) in needed}
    result = {}
    for symbol, stamp in keys:
        current, previous = prices.get((symbol, stamp)), prices.get((symbol, stamp - earlier))
        if number(current) and current > 0 and number(previous) and previous > 0:
            result[f"{symbol}:{stamp.isoformat()}"] = (current / previous - 1) * 100
    return result


def buy_scores(result, selection, minute_evidence=None):
    day = result["as_of"]
    # A score belongs to the selection frozen with the buy. Use the same-date
    # selection slot only when the buy writer did not embed a numeric branch.
    frozen = result.get("selection_snapshot") or {}
    if frozen.get("as_of") != day or type(frozen.get("branch_code")) is not int:
        frozen = selection or {}
    branch = frozen.get("branch_code") if frozen.get("as_of") == day else None
    rule = RULES.get(branch) if type(branch) is int else None
    selected = {row["symbol"]: row for row in frozen.get("rows", [])}
    out = []
    for row in (result.get("sections") or {}).get("buy", result.get("rows", [])):
        inputs = selected.get(row["symbol"], {})
        value = None
        if rule == "return_5m":
            value = row.get("s05_return_5m_pct")
            if not number(value) and row.get("entry_sort_by") == rule:
                value = row.get("entry_sort_value")
            if not number(value) and (key := minute_key(row, date.fromisoformat(day))):
                value = (minute_evidence or {}).get(f"{key[0]}:{key[1].isoformat()}")
        elif rule == "fund_tech":
            fund, tech = inputs.get("s02_fund_score"), inputs.get("s02_tech_score")
            if number(fund) and number(tech):
                value = (7 * fund + 3 * tech) / 10
        elif rule == "four_dimension":
            value = inputs.get("s02_score")
        out.append({**row, "signal_score": value if number(value) else None, "signal_score_rule": rule})
    return out


def apply_to_view(store, conn, result, original_buy, selection):
    """Project once on write; never read bars or run a strategy on GET."""
    from app.services.daily_selections import selection_record
    from app.services.selection_clock import S05_ID
    from app.services.strategy_day_views import _get, _key
    from app.services.strategy_results import NAMESPACE
    from app.strategy.basic_filter import previous_session

    day = result["as_of"]
    key = _key(S05_ID, day, "stock", "1m")
    sections = result["sections"]
    sections["buy"] = buy_scores(original_buy, selection, _get(store, conn, MINUTE_NAMESPACE, key))
    if not sections["sell"]:
        return
    previous = str(previous_session(date.fromisoformat(day)))
    prior_key = _key(S05_ID, previous, "stock", "1m")

    def archived(record_id):
        if store.tenant:
            row = conn.execute("SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                               (NAMESPACE, record_id)).fetchone()
            return row["value"] if row else None
        import json

        row = conn.execute("SELECT value FROM strategy_results WHERE id=?", (record_id,)).fetchone()
        return json.loads(row["value"]) if row else None

    snap = (selection_record(S05_ID, previous, archived) or {}).get("result", {}).get("selection_snapshot")
    evidence = _get(store, conn, MINUTE_NAMESPACE, prior_key)
    by_record = {}
    for record_id in {row.get("entry_record_id") for row in sections["sell"] if row.get("entry_date") == previous}:
        saved = archived(record_id) if isinstance(record_id, str) else None
        summary = (saved or {}).get("summary", {})
        if summary.get("strategy_id") == S05_ID and summary.get("as_of") == previous:
            by_record[record_id] = {row["symbol"]: row for row in buy_scores(saved["result"], snap, evidence)}
    sections["sell"] = [
        {**row, **{key: prior.get(key) for key in ("signal_score", "signal_score_rule")}}
        for row in sections["sell"]
        for prior in [by_record.get(row.get("entry_record_id"), {}).get(row["symbol"], {})
                      if row.get("entry_date") == previous else {}]
    ]
