"""Shared raw range/amount score and causal S01 system-factor integration."""
import math
from datetime import date, datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.factors.registry import factor_columns_view, get_factor, private_factor_registry
from app.strategy.intraday_features import INTRADAY_FEATURES, build_feature_frame
from app.strategy.scoring import materialize_scoring_columns, scoring_value_expr

FID = "intraday_range_amount_score"
LABEL = "当日低振幅成交额评分"


def sample():
    return pl.DataFrame({
        "symbol": ["600003.SH", "600002.SH", "600001.SH"],
        "date": [date(2026, 11, 16)] * 3,
        "high": [10.2, 10.2, 10.1], "low": [9.7, 9.7, 9.9],
        "prev_close": [10.] * 3, "amount": [100000000., 100000000., 300000000.],
    })


def computed(frame):
    expression = scoring_value_expr(frame.columns, FID)
    assert expression is not None, "System range/amount factor is not executable"
    return frame.with_columns(expression.alias(FID))


def test_system_catalog_registers_exact_formula_units_and_high_direction():
    spec = get_factor(FID)
    assert spec is not None, "System range/amount factor is not registered"
    assert spec.label == LABEL and spec.kind == "virtual"
    assert spec.unit == "score" and spec.direction == "high"
    assert spec.scale_free is False
    assert spec.dependencies == frozenset({"high", "low", "prev_close", "amount"})
    assert factor_columns_view()[-1]["id"] == FID
    with private_factor_registry([]):
        assert get_factor(FID) == spec


def test_raw_formula_is_not_normalized_and_equal_scores_sort_by_symbol():
    frame = computed(sample())
    expected = [-(r["high"] - r["low"]) / r["prev_close"] + .001 * math.log(r["amount"])
                for r in sample().to_dicts()]
    assert frame[FID].to_list() == pytest.approx(expected, abs=1e-14)
    assert materialize_scoring_columns(sample(), {FID})[FID].equals(frame[FID])
    ranked = frame.sort([FID, "symbol"], descending=[True, False])
    assert ranked["symbol"].to_list() == ["600001.SH", "600002.SH", "600003.SH"]
    # ln(amount) is exact: amount=1 gives zero, whereas ln(amount+1) would not.
    one_yuan = computed(sample().with_columns(pl.lit(1.).alias("amount")))
    assert one_yuan[FID][0] == pytest.approx(-.05, abs=1e-14)


@pytest.mark.parametrize("column,value", [
    ("amount", 0.), ("amount", -1.), ("amount", float("nan")),
    ("amount", float("inf")), ("amount", None),
    ("prev_close", 0.), ("prev_close", None), ("prev_close", float("inf")),
    ("high", 0.), ("high", float("nan")), ("low", -1.), ("low", 11.),
])
def test_bad_inputs_remain_missing_instead_of_becoming_zero_scores(column, value):
    frame = sample().with_columns(pl.lit(value, dtype=pl.Float64).alias(column))
    assert computed(frame)[FID].null_count() == frame.height


def test_factor_is_exposed_by_the_existing_system_catalog_api():
    from app.api.factors import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/factors", params={"asset_type": "stock"})
    assert response.status_code == 200
    spec = next((item for item in response.json()["factors"] if item["id"] == FID), None)
    assert spec is not None, "System factor is absent from the public catalog"
    assert spec["label"] == LABEL and spec["direction"] == "high"
    assert spec["unit"] == "score"


def minute_sample():
    return pl.DataFrame({
        "symbol": ["600001.SH"] * 5,
        "datetime": [
            datetime(2026, 11, 16, 9, 31), datetime(2026, 11, 16, 11, 30),
            datetime(2026, 11, 16, 14, 45), datetime(2026, 11, 16, 14, 46),
            datetime(2026, 11, 17, 9, 31),
        ],
        "open": [10.] * 5, "close": [10.] * 5,
        "high": [10.1, 10.2, 10.3, 999., 10.1],
        "low": [9.9, 9.8, 9.7, 1., 9.9],
        "volume": [1000.] * 5,
        "amount": [10000000., 20000000., 70000000., 9000000000., 10000000.],
    })


def test_intraday_score_uses_only_completed_prefix_and_carries_across_lunch():
    assert FID in INTRADAY_FEATURES, "System factor is absent from intraday feature options"
    cutoff = datetime(2026, 11, 16, 14, 46)
    frame = build_feature_frame(minute_sample(), prev_close={"600001.SH": 10.}, cutoff=cutoff)
    assert frame.height == 3
    assert frame[FID][-1] == pytest.approx(-.06 + .001 * math.log(100000000.), abs=1e-14)
    changed_future = minute_sample().with_columns(
        pl.when(pl.col("datetime") >= cutoff).then(999999.).otherwise(pl.col("high")).alias("high"))
    other = build_feature_frame(changed_future, prev_close={"600001.SH": 10.}, cutoff=cutoff)
    assert other[FID].equals(frame[FID])
    all_days = build_feature_frame(minute_sample(), prev_close={"600001.SH": 10.})
    assert all_days[FID][-1] == pytest.approx(-.02 + .001 * math.log(10000000.), abs=1e-14)


def test_intraday_missing_reference_or_invalid_amount_does_not_hide_a_gap():
    assert FID in INTRADAY_FEATURES
    frame = minute_sample().head(3)
    assert build_feature_frame(frame, prev_close={})[FID].null_count() == 3
    damaged = frame.with_columns(
        pl.when(pl.col("datetime") == datetime(2026, 11, 16, 11, 30))
        .then(None).otherwise(pl.col("amount")).alias("amount"))
    values = build_feature_frame(damaged, prev_close={"600001.SH": 10.})[FID].to_list()
    assert values[0] is not None and values[1:] == [None, None]


def test_matrix_daily_score_parity_and_no_previous_minute_reference():
    import numpy as np

    from app.backtest.matrix import build_market_data_matrix, matrix_feature

    frame = sample().with_columns(
        pl.lit(10.).alias("open"), pl.lit(10.).alias("close"), pl.lit(1000.).alias("volume"))
    matrix = build_market_data_matrix(frame, field_columns={"amount", "prev_close"})
    np.testing.assert_allclose(
        matrix_feature(matrix, FID)[0], computed(frame).sort("symbol")[FID].to_numpy(), atol=1e-7,
    )
    minute = minute_sample().drop("open").with_columns(pl.lit(10.).alias("open"))
    # Minute rows do not supply yesterday's close: preceding-minute close is not a substitute.
    assert np.isnan(matrix_feature(build_market_data_matrix(minute), FID)).all()


def test_trusted_helper_materializes_the_catalog_factor_without_io():
    from app.strategy.market_data import get_intraday_range_amount_score

    frame = sample().rename({"prev_close": "prior_close"})
    actual = get_intraday_range_amount_score(frame, prev_close_column="prior_close")
    assert actual[FID].equals(computed(sample())[FID])
    assert get_intraday_range_amount_score(frame)[FID].null_count() == frame.height


def test_safe_source_owned_engine_preserves_negative_raw_scores_and_symbol_ties(tmp_path):
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    source = f'''
import polars as pl
from app.strategy.market_data import get_intraday_range_amount_score
META = {{"id": "custom_score_test", "name": "score test", "asset_types": ["stock"],
         "timeframes": ["1d"], "scoring": {{"{FID}": 1}}, "scoring_mode": "strategy",
         "order_by": "score", "descending": True, "tie_break": "symbol", "basic_filter": {{"enabled": False}}}}
BASIC_FILTER = {{"enabled": False}}
PARAMS = []
def filter_history(frame, params):
    return get_intraday_range_amount_score(frame).with_columns(pl.col("{FID}").alias("score"))
'''
    path = tmp_path / "custom_score_test.py"
    path.write_text(source)
    engine = StrategyEngine([tmp_path])
    assert engine.has("custom_score_test"), engine.load_errors()
    frame = sample().with_columns(pl.lit(10.).alias("close"))
    context = StrategyDataContext("stock", "1d", date(2026, 11, 16), current=frame, history=frame)
    result = engine.run("custom_score_test", context, overrides={"scoring": {"amount": 99}})
    assert [row["symbol"] for row in result.rows] == ["600001.SH", "600002.SH", "600003.SH"]
    expected = computed(frame).sort([FID, "symbol"], descending=[True, False])[FID].to_list()
    assert [row["score"] for row in result.rows] == pytest.approx(expected, abs=1e-14)
    assert all(row["score"] == row[FID] for row in result.rows)
