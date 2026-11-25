"""Readiness is an observed per-symbol window, never a collection wall clock."""
import importlib
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta

import polars as pl
import pytest

AT = datetime(2026, 11, 24, 14, 30)
SYMBOLS = ["600001.SH", "600002.SH"]


def module():
    name = "app.services.strategy_input_manifest"
    assert importlib.util.find_spec(name), "validated per-symbol input manifest is missing"
    return importlib.import_module(name)


def request():
    return {
        "strategy_id": "custom_mvxxcp88", "as_of": "2026-11-24",
        "reference_date": "2026-11-23", "bar_at": AT.isoformat(),
        "definition_sha256": "a" * 64, "algorithm_sha256": "b" * 64,
        "pool": {"symbols": SYMBOLS, "definition_version": "strategy_scope_v4",
                 "status_evidence": {"version": "one"}},
        "selection": {"as_of": "2026-11-24", "source_date": "2026-11-23",
                      "rows": [{"symbol": s, "reference_close": 10., "atr_rank": i + 1,
                                "atr14": .5, "score": 100. - i}
                               for i, s in enumerate(SYMBOLS)]},
        "signal": {"id": "late_session_strength", "params": {
            "time": "14:30:00", "ma5_1m_count": 7, "ma5_5m_count": 4,
            "amount_min_100m": None, "turnover_min_pct": None,
            "amplitude_max_pct": None, "volume_ratio_min": None}},
    }


def minutes(symbols=SYMBOLS, count=40):
    return pl.DataFrame([
        {"symbol": s, "datetime": AT - timedelta(minutes=i), "close": 10. + i / 100}
        for s in symbols for i in range(count)
    ])


def build(frame, *, phase="stocks", value=None, proof=None, errors=None):
    from app.services.strategy_input_source import expected_dates, verify_source
    req = value or request()
    raw = pl.DataFrame([
        {"symbol": row["symbol"], "date": day, "open": 10., "high": 11., "low": 9., "close": 10.}
        for row in req["selection"]["rows"] for day in expected_dates(req)
        if row.get("reference_close") is not None
    ], schema={"symbol": pl.String, "date": pl.Date, "open": pl.Float64,
               "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64})
    evidence, source_errors = verify_source(req, raw, factors)
    evidence["reference_daily"] = [{"symbol": s, "date": req["reference_date"], "close": 10.}
                                 for s in SYMBOLS]
    evidence["reference_errors"] = {**source_errors, **(errors or {})}
    return module().build_manifest(
        req, phase=phase, minutes=frame,
        references={s: 10. for s in SYMBOLS}, reference_errors=evidence["reference_errors"],
        coverage=proof or {}, source_evidence=evidence,
        published_at="2026-11-24T14:30:40+08:00",
    )


def test_new_completion_wall_clock_with_old_bar_is_waiting_input():
    value = build(minutes().filter(pl.col("datetime") < AT))
    assert value["state"] == "waiting_input"
    assert value["coverage"]["600001.SH"]["state"] == "pending"
    assert AT.isoformat() in value["coverage"]["600001.SH"]["missing_minutes"]


def test_partially_missing_ma_window_cannot_freeze_success():
    value = build(minutes().filter(
        ~((pl.col("symbol") == SYMBOLS[1]) & (pl.col("datetime") == AT - timedelta(minutes=25)))))
    assert value["state"] == "waiting_input"
    assert value["coverage"][SYMBOLS[0]]["state"] == "available"
    assert value["coverage"][SYMBOLS[1]]["state"] == "pending"


def test_repeated_network_errors_do_not_prove_symbol_unavailable():
    value = build(minutes([SYMBOLS[0]]), proof={SYMBOLS[1]: {
        "state": "failed", "attempts": 100, "completed_through": AT.isoformat(),
        "required_from": (AT - timedelta(minutes=39)).isoformat(), "request_seq": 4}})
    assert value["state"] == "waiting_input"
    assert value["coverage"][SYMBOLS[1]]["state"] == "pending"


def test_completed_bounded_full_requests_isolate_absent_symbol_and_keep_full_pool():
    value = build(minutes([SYMBOLS[0]]), proof={SYMBOLS[1]: {
        "minute": [{"state": "succeeded", "request_seq": seq,
                    "day": "2026-11-24", "symbol": SYMBOLS[1],
                    "completed_through": AT.isoformat(),
                    "required_from": (AT - timedelta(minutes=39)).isoformat()}
                   for seq in (3, 4)]}})
    assert value["state"] == "ready"
    assert value["coverage"][SYMBOLS[1]]["state"] == "unavailable"
    assert value["input"]["pool"]["symbols"] == SYMBOLS
    assert value["usable_symbols"] == [SYMBOLS[0]]
    assert value["invalid_symbols"][SYMBOLS[1]]["reason"]


def test_breadth_needs_target_only_but_all_invalid_cannot_be_successful_zero():
    value = build(minutes(count=1), phase="breadth", errors={s: "bad exact previous close" for s in SYMBOLS})
    assert value["state"] == "no_valid_input"
    assert not value["usable_symbols"]
    assert len(value["invalid_symbols"]) == 2


def test_successful_empty_selection_is_ready_without_minutes():
    req = request()
    req["selection"]["rows"] = []
    assert build(minutes([]), value=req)["state"] == "ready"


def test_duplicate_symbol_minute_consumes_last_request_and_no_future_bar():
    frame = pl.concat([minutes(), pl.DataFrame([
        {"symbol": SYMBOLS[0], "datetime": AT, "close": 12.34},
        {"symbol": SYMBOLS[0], "datetime": AT + timedelta(minutes=1), "close": 99.}])])
    value = build(frame)
    rows = value["input"]["minutes"]
    assert len(rows) == 80
    assert next(r for r in rows if r["symbol"] == SYMBOLS[0] and r["datetime"] == AT.isoformat())["close"] == 12.34
    assert all(r["datetime"] <= AT.isoformat() for r in rows)


def test_source_precision_and_semantic_identity_are_preserved_across_environments():
    first = build(minutes())
    second = deepcopy(first)
    second["published_at"] = "2026-11-24T14:32:00+08:00"
    second["environment"] = "local"
    assert module().validate_manifest(second) == first["input_sha256"]
    second["input"]["minutes"][0]["close"] += 1e-12
    with pytest.raises(ValueError, match="摘要"):
        module().validate_manifest(second)


def test_definition_rank_and_adjustments_are_part_of_input_identity():
    first = build(minutes())
    req = request()
    req["selection"]["rows"][0]["atr_rank"] = 2
    assert build(minutes(), value=req)["input_sha256"] != first["input_sha256"]
    changed = deepcopy(first)
    changed["input"]["source_evidence"]["adjustments_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="摘要"):
        module().validate_manifest(changed)


def test_required_windows_reject_unfinished_target_and_foreign_day():
    req = request()
    req["bar_at"] = "2026-11-25T14:30:00"
    with pytest.raises(ValueError, match="日期"):
        build(minutes(), value=req)


def test_minute_receipt_cannot_certify_a_missing_daily_reference():
    req = request()
    req["selection"]["rows"][1]["reference_close"] = None
    value = build(minutes(), value=req, proof={SYMBOLS[1]: {
        "minute": [{"state": "succeeded", "request_seq": seq,
                    "day": "2026-11-24", "symbol": SYMBOLS[1],
                    "required_from": (AT - timedelta(minutes=39)).isoformat(),
                    "completed_through": AT.isoformat()} for seq in (3, 4)]}})
    assert value["coverage"][SYMBOLS[1]]["state"] == "pending"


@pytest.mark.parametrize("field,bad", [("close", True), ("amount", -1.), ("volume", float("inf"))])
def test_enabled_dependency_invalid_values_are_isolated(field, bad):
    req = request()
    req["signal"]["params"]["ma5_1m_count"] = None
    req["signal"]["params"]["ma5_5m_count"] = None
    req["signal"]["params"]["amount_min_100m"] = 1. if field == "amount" else None
    req["signal"]["params"]["volume_ratio_min"] = 1. if field == "volume" else None
    frame = minutes(count=1)
    if field == "close":
        frame = frame.with_columns(pl.lit(True).alias("close"))
    else:
        frame = frame.with_columns(pl.lit(bad).alias(field))
    value = build(frame, value=req)
    assert value["coverage"][SYMBOLS[0]]["state"] == "invalid"
    assert value["state"] == "no_valid_input"


def test_coverage_cannot_be_forged_by_rehashing_missing_input():
    value = build(minutes([SYMBOLS[0]]))
    value["state"] = "ready"
    value["coverage"][SYMBOLS[1]]["state"] = "available"
    value["usable_symbols"] = SYMBOLS
    value["input_sha256"] = module().digest({key: value[key] for key in module().SEMANTIC_FIELDS})
    with pytest.raises(ValueError, match="覆盖"):
        module().validate_manifest(value)


def test_missing_atr_window_requires_daily_proof_for_each_missing_day():
    req = request()
    from test_strategy_input_source import data, verify
    req, raw, factors = data()
    symbol = SYMBOLS[1]
    missing = raw["date"][0]
    evidence, errors = verify(
        req, raw.filter(~((pl.col("symbol") == symbol) & (pl.col("date") == missing))), factors)
    evidence["reference_daily"] = raw.filter(pl.col("date") == raw["date"].max()).to_dicts()
    value = module().build_manifest(
        req, phase="stocks", minutes=minutes(), references={s: 10. for s in SYMBOLS},
        reference_errors=errors, coverage={}, source_evidence=evidence, published_at=AT.isoformat())
    assert value["coverage"][symbol]["state"] == "pending"
    assert value["coverage"][symbol]["missing_daily_dates"] == [str(missing)]
    proof = {symbol: {"daily": [{
        "symbol": symbol, "day": str(missing), "state": "succeeded", "request_seq": seq,
        "required_from": f"{missing}T00:00:00", "completed_through": f"{missing}T23:59:59.999999",
    } for seq in (10, 11)]}}
    value = module().build_manifest(
        req, phase="stocks", minutes=minutes(), references={s: 10. for s in SYMBOLS},
        reference_errors=errors, coverage=proof, source_evidence=evidence, published_at=AT.isoformat())
    assert value["state"] == "ready"
    assert value["coverage"][symbol]["state"] == "unavailable"


def test_receiver_rejects_hash_only_source_evidence():
    value = build(minutes())
    value["input"]["source_evidence"] = {"raw_daily_sha256": "c" * 64}
    value["input_sha256"] = module().digest({key: value[key] for key in module().SEMANTIC_FIELDS})
    with pytest.raises(ValueError, match="原始"):
        module().validate_manifest(value)


def test_breadth_cannot_seal_an_incomplete_common_atr_baseline():
    from test_strategy_input_source import data, verify
    req, raw, factors = data()
    symbol = SYMBOLS[1]
    evidence, errors = verify(req, raw.filter(
        ~((pl.col("symbol") == symbol) & (pl.col("date") == raw["date"][0]))), factors)
    value = module().build_manifest(
        req, phase="breadth", minutes=minutes(count=1), references={s: 10. for s in SYMBOLS},
        reference_errors=errors, coverage={}, source_evidence=evidence, published_at=AT.isoformat())
    assert value["state"] == "waiting_input"
    assert value["coverage"][symbol]["missing_daily_dates"] == [str(raw["date"][0])]
