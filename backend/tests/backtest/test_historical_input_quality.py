from datetime import date

import polars as pl

from app.backtest.governance import research_preflight, status_quality
from app.backtest.strategy import StrategyBacktestConfig
from app.services.data_release import historical_input_quality


def test_unknown_publication_counts_do_not_certify_announcement_dates(tmp_path):
    root = tmp_path / "financials/metrics"
    root.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH"],
        "announce_date": ["2020-03-01", None], "period_end": ["2019-12-31"] * 2,
    }).write_parquet(root / "part.parquet")
    quality = historical_input_quality(tmp_path)
    financial = quality["financial_publication"]
    assert financial["rows"] == financial["unknown_rows"] == 2
    assert financial["unknown_ratio"] == 1
    assert quality["universe"]["missing_delisted_ratio"] is None
    assert quality["universe"]["survivorship_verified"] is False


def test_empty_inputs_have_unknown_denominator(tmp_path):
    financial = historical_input_quality(tmp_path)["financial_publication"]
    assert financial["rows"] == 0
    assert financial["unknown_ratio"] is None


def test_verified_status_and_frozen_inputs_do_not_prove_historical_universe(tmp_path):
    day = date(2024, 1, 2)
    frame = pl.DataFrame({
        "date": [day, day], "instrument_status_known": [True, False],
        "is_risk_warning": [False, False], "is_listed": [True, True],
    })
    quality = status_quality(frame, day, day)
    assert quality["unknown_status_ratio"] == .5
    config = StrategyBacktestConfig("s", None, day, day, strict_research=True)
    provenance = {
        "release_consistent": True, "input_snapshot": {"id": "retained"},
        "historical_inputs": historical_input_quality(tmp_path),
    }
    issues = research_preflight(config, provenance, quality)
    assert any("50.0%" in issue for issue in issues)
    assert any("退市" in issue for issue in issues)
    assert any("原公告" in issue for issue in issues)
    assert provenance["research_governance"]["historical_pit_verified"] is False


def test_response_contract_keeps_additive_evidence(tmp_path):
    from app.api.contracts import BacktestProvenance
    from app.services.data_release import backtest_provenance

    trace = backtest_provenance(tmp_path, asset_type="stock", config={})
    response = BacktestProvenance.model_validate(trace).model_dump(mode="json")
    assert response["historical_inputs"] == trace["historical_inputs"]
    assert response["input_snapshot"] is None
