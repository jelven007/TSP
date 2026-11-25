"""Validate saved S02 ATR against original bars and the actual adjustment evidence."""
import importlib
import importlib.util
from datetime import date

import polars as pl
import pytest
from test_strategy_input_manifest import request


def data():
    from app.strategy.basic_filter import previous_session
    days = [date(2026, 11, 23)]
    for _ in range(14):
        days.insert(0, previous_session(days[0]))
    raw = pl.DataFrame([
        {"symbol": symbol, "date": day, "open": 10., "high": 11., "low": 9., "close": 10.}
        for symbol in request()["pool"]["symbols"] for day in days])
    factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.String, "ex_factor": pl.Float64})
    req = request()
    for i, row in enumerate(req["selection"]["rows"]):
        row.update(atr14=2., atr14_previous_close=.2, atr_reference_date="2026-11-23",
                   atr_rank=i + 1, atr_rank_population=2, atr_rank_score=100. * (1 - i))
    return req, raw, factors


def verify(req, raw, factors):
    name = "app.services.strategy_input_source"
    assert importlib.util.find_spec(name), "raw/adjustment input validation is missing"
    return importlib.import_module(name).verify_source(req, raw, factors)


def test_raw_previous_close_is_checked_independently_of_saved_atr():
    req, raw, factors = data()
    req["selection"]["rows"][0]["reference_close"] = 9.
    evidence, errors = verify(req, raw, factors)
    assert list(errors) == [req["selection"]["rows"][0]["symbol"]]
    assert evidence["raw_daily_sha256"]
    assert evidence["adjustments_sha256"]


def test_missing_adjustment_changes_atr_and_is_not_hidden_by_same_raw_prices():
    req, raw, factors = data()
    factors = pl.DataFrame({"symbol": [req["pool"]["symbols"][0]],
                            "trade_date": ["2026-11-11"], "ex_factor": [1.1]})
    _, errors = verify(req, raw, factors)
    assert "ATR" in errors[req["pool"]["symbols"][0]]
    assert req["pool"]["symbols"][1] not in errors


def test_bad_symbol_window_is_isolated_and_missing_atr_keeps_existing_contract():
    req, raw, factors = data()
    row = req["selection"]["rows"][0]
    row.update(atr14=None, atr14_previous_close=None, atr_rank=None,
               atr_rank_score=None, atr_metric_error="original missing ATR")
    raw = raw.filter(~((pl.col("symbol") == row["symbol"]) & (pl.col("date") == date(2026, 11, 3))))
    evidence, errors = verify(req, raw, factors)
    assert row["symbol"] not in errors
    assert evidence["atr_validation"][row["symbol"]]["state"] == "unknown_frozen_metric"


def test_unknown_global_raw_schema_fails_explicitly():
    req, raw, factors = data()
    with pytest.raises(ValueError, match="原始"):
        verify(req, raw.drop("symbol"), factors)


def test_missing_known_atr_history_stays_pending_instead_of_invalid():
    req, raw, factors = data()
    symbol = req["pool"]["symbols"][0]
    missing = raw["date"][0]
    raw = raw.filter(~((pl.col("symbol") == symbol) & (pl.col("date") == missing)))
    evidence, errors = verify(req, raw, factors)
    assert symbol not in errors
    assert evidence["dependencies"][symbol]["missing_daily_dates"] == [str(missing)]
    assert evidence["atr_validation"][symbol]["state"] == "pending"


def test_duplicate_daily_rows_are_invalid_not_missing():
    req, raw, factors = data()
    evidence, errors = verify(req, pl.concat([raw, raw.head(1)]), factors)
    symbol = req["pool"]["symbols"][0]
    assert symbol in errors
    assert evidence["dependencies"][symbol]["missing_daily_dates"] == []


def test_receiving_source_recomputes_instead_of_trusting_rehashed_validation():
    from app.services import strategy_input_source as source
    from app.services.strategy_input_manifest import digest
    req, raw, factors = data()
    evidence, _ = verify(req, raw, factors)
    evidence["raw_daily"][1]["high"] += 1.
    evidence["raw_daily_sha256"] = digest(evidence["raw_daily"])
    assert hasattr(source, "validate_evidence"), "receiver does not recompute source evidence"
    with pytest.raises(ValueError, match="重算"):
        source.validate_evidence(req, evidence)


def test_ranks_keep_original_population_and_gaps_after_status_exclusion():
    req, raw, factors = data()
    for rank, row in zip((1, 3), req["selection"]["rows"], strict=True):
        row.update(atr_rank=rank, atr_rank_population=3,
                   atr_rank_score=100. * (3 - rank) / 2)
    _, errors = verify(req, raw, factors)
    assert not errors
    req["selection"]["rows"][1]["atr_rank"] = 1
    with pytest.raises(ValueError, match="排名"):
        verify(req, raw, factors)


def test_rank_order_cannot_disagree_with_verified_atr():
    req, raw, factors = data()
    for rank, row in zip((2, 1), req["selection"]["rows"], strict=True):
        row.update(atr_rank=rank, atr_rank_population=2,
                   atr_rank_score=100. * (2 - rank))
    with pytest.raises(ValueError, match="排名"):
        verify(req, raw, factors)


def test_boolean_adjustment_is_invalid_even_when_numerically_one():
    req, raw, factors = data()
    factors = pl.DataFrame({"symbol": [req["pool"]["symbols"][0]],
                            "trade_date": ["2026-11-11"], "ex_factor": [True]})
    _, errors = verify(req, raw, factors)
    assert req["pool"]["symbols"][0] in errors
