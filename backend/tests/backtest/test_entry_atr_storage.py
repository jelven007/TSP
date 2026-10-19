"""Execution ATR can be derived from old OHLC snapshots without rewriting them."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    build_market_data_matrix,
    load_market_data_matrix_from_parquet,
    matrix_feature,
)


def _panel():
    return pl.DataFrame({
        "symbol": ["600001.SH"] * 30,
        "date": [date(2024, 1, 2) + timedelta(days=i) for i in range(30)],
        "open": [10.] * 30, "close": [10.] * 30,
        "high": [10. + (i % 4 + 1) / 10 for i in range(30)],
        "low": [9.8] * 30, "volume": [10000.] * 30,
    })


@pytest.mark.parametrize("storage", ["panel", "parquet", "mmap"])
def test_missing_atr_column_is_derived_with_the_shared_causal_formula(tmp_path, storage):
    panel = _panel()
    expected = matrix_feature(build_market_data_matrix(panel), "atr_14")
    if storage == "panel":
        actual = build_market_data_matrix(panel, field_columns={"atr_14"})
    else:
        root = tmp_path / "input"
        root.mkdir()
        paths = []
        for frame in panel.partition_by("date"):
            folder = root / f"date={frame['date'][0].isoformat()}"
            folder.mkdir()
            path = folder / "bars.parquet"
            frame.write_parquet(path)
            paths.append(path)
        before = [path.read_bytes() for path in paths]
        actual = load_market_data_matrix_from_parquet(
            root, date(2024, 1, 2), date(2024, 2, 1), field_columns={"atr_14"},
            cache_root=tmp_path / "cache" if storage == "mmap" else None,
        )
        assert [path.read_bytes() for path in paths] == before
    np.testing.assert_allclose(actual.fields["atr_14"], expected)
    assert not actual.fields["atr_14"].flags.writeable
    changed = panel.with_columns(
        pl.when(pl.col("date") > date(2024, 1, 16)).then(50.).otherwise(pl.col("high")).alias("high"),
    )
    future = build_market_data_matrix(changed, field_columns={"atr_14"})
    np.testing.assert_allclose(actual.fields["atr_14"][:15], future.fields["atr_14"][:15])


def test_existing_atr_values_and_gaps_are_not_recomputed():
    panel = _panel().with_columns(pl.Series("atr_14", [None] * 5 + [2.] * 25))
    market = build_market_data_matrix(panel, field_columns={"atr_14"})
    assert np.isnan(market.fields["atr_14"][:5]).all()
    assert (market.fields["atr_14"][5:] == 2).all()
