"""Optional preselection metrics cannot discard otherwise qualified members."""
import polars as pl
import pytest
from test_s02_atr14_strategy import DAY, SOURCE, STRATEGY_ID, frames, prior

from app.strategy import market_data
from app.strategy.engine import StrategyEngine


@pytest.fixture
def selection(monkeypatch):
    monkeypatch.setattr(market_data, "previous_session", prior)
    engine = StrategyEngine([SOURCE.parent])
    _, daily = frames()
    symbols = daily["symbol"].unique().sort().to_list()
    rows = pl.DataFrame({"symbol": symbols, "score": [9., 8.], "reference_close": [10., 20.]})
    return engine.get(STRATEGY_ID).enrich_selection_fn, rows, daily


@pytest.mark.parametrize("problem", ["missing", "bad_price"])
def test_bad_single_window_preserves_all_members_and_scores_valid_subset(selection, problem):
    enrich, rows, daily = selection
    first = rows["symbol"][0]
    if problem == "missing":
        daily = daily.filter(~((pl.col("symbol") == first) & (pl.col("date") == daily["date"].min())))
    else:
        daily = daily.with_columns(
            pl.when(pl.col("symbol") == first).then(0.).otherwise(pl.col("raw_close")).alias("raw_close"))
    result = enrich(rows, {}, daily=daily, as_of=DAY)
    assert result["symbol"].to_list() == rows["symbol"].to_list()
    assert result["score"].to_list() == [9., 8.]
    unknown, valid = result.to_dicts()
    assert unknown["atr14_previous_close"] is None
    assert unknown["atr_rank_score"] is None
    assert unknown["atr_metric_error"]
    assert valid["atr14_previous_close"] > 0
    assert valid["atr_rank_score"] == 50.
    assert valid["atr_rank_population"] == 1


def test_all_unknown_atr_returns_complete_selection_with_explicit_unknowns(selection):
    enrich, rows, _ = selection
    result = enrich(rows, {}, daily=None, as_of=DAY)
    assert result["symbol"].to_list() == rows["symbol"].to_list()
    assert result["atr14_previous_close"].null_count() == len(rows)
    assert result["atr_rank_score"].null_count() == len(rows)
    assert result["atr_rank_population"].to_list() == [0, 0]


def test_complete_windows_keep_original_linear_ordinal_score(selection):
    enrich, rows, daily = selection
    result = enrich(rows, {}, daily=daily, as_of=DAY)
    assert sorted(result["atr_rank_score"].to_list()) == [0., 100.]
    assert result["atr_rank_population"].to_list() == [2, 2]
