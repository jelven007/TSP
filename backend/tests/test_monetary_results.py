"""CNY tolerance classifies completed results without rounding their values."""
from decimal import Decimal

import polars as pl
import pytest


@pytest.mark.parametrize("left,right,expected", [
    (10., 10.01, True),
    (10., 10.010001, False),
    (10.01, 10., True),
    (-10., -10.01, True),
    (1_000_000_000., 1_000_000_000.02, False),
    (None, None, False),
    (float("nan"), float("nan"), False),
    (float("inf"), float("inf"), False),
])
def test_scalar_and_expression_agree(left, right, expected):
    from app.monetary import money_result_equal, money_result_equal_expr

    assert money_result_equal(left, right) is expected
    frame = pl.DataFrame({"a": [left], "b": [right]},
                         schema={"a": pl.Float64, "b": pl.Float64})
    assert frame.select(money_result_equal_expr(pl.col("a"), pl.col("b"))).item() is expected
    assert frame["a"].to_list() == [left] or left != left


def test_exact_decimal_boundary_and_unmodified_result():
    from app.monetary import money_result_equal

    left, right = Decimal("12345678901234567890"), Decimal("12345678901234567890.01")
    assert money_result_equal(left, right)
    assert not money_result_equal(left, right + Decimal("0.00001"))
    assert right == Decimal("12345678901234567890.01")


def test_processed_close_within_cent_is_accepted_and_preserved():
    from datetime import date
    from types import SimpleNamespace

    from app.services.daily_close_validation import validate_daily_close

    day = date(2026, 11, 13)
    raw = pl.DataFrame({"symbol": ["000001.SZ"], "date": [day], "close": [10.]})
    rows = raw.with_columns(pl.lit(10.01).alias("raw_close"))
    repo = SimpleNamespace(get_daily_close_inputs=lambda *args, **kw: (raw, pl.DataFrame()))
    validate_daily_close(repo, day, rows, check_closing_minute=False)
    assert rows["raw_close"].item() == 10.01 and raw["close"].item() == 10.
    with pytest.raises(ValueError, match="昨收"):
        validate_daily_close(repo, day, rows.with_columns(pl.lit(10.010001).alias("raw_close")),
                             check_closing_minute=False)
