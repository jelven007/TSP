"""S05 owns its scoring; archived fields remain compatible with routed entries."""
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy.basic_filter import previous_session

DAY = date(2026, 11, 26)
SOURCE = previous_session(DAY)
OBSERVED = datetime(2026, 11, 25, 22, tzinfo=CN_TZ)


def inputs(count=2):
    dates = [SOURCE]
    for _ in range(64):
        dates.insert(0, previous_session(dates[0]))
    symbols = [f"600{i:03d}.SH" for i in range(count, 0, -1)]
    snapshot = {
        "as_of": str(DAY), "source_date": str(SOURCE), "identity": "source-identity",
        "scheduled_for": f"{SOURCE}T21:00:00+08:00",
        "generated_at": f"{SOURCE}T21:10:00+08:00",
        "coverage": {"scope_total": count, "available_daily": count},
        "rows": [{"symbol": s, "name": "测试公司", "reference_close": 10.} for s in symbols],
    }
    daily = pl.DataFrame([
        {"symbol": s, "date": d, "close": 10., "raw_close": 10.,
         "high": 11., "low": 9., "amount": 1e8}
        for d in dates for s in symbols])
    flows = daily.select("symbol", "date").with_columns(
        pl.lit(3e6).alias("super_large_net_inflow"), pl.lit(2e6).alias("large_net_inflow"))
    common = {"symbol": symbols[0] if symbols else "", "announce_date": date(2026, 8, 1),
              "period_end": date(2026, 6, 30)}
    financial = {
        "metrics": pl.DataFrame([{**common, "net_income_yoy": 10., "roe": 7.5,
                                 "debt_to_asset_ratio": 57.5, "eps_basic": .25}]),
        "income": pl.DataFrame([{**common, "net_income": 1e8}]),
        "cash_flow": pl.DataFrame([{**common, "net_operating_cash_flow": .75e8}]),
    }
    return snapshot, daily, flows, financial


def forbid_legacy(monkeypatch):
    from app.services import s02_173004552_selection as legacy

    def forbidden(*args, **kwargs):
        raise AssertionError("S05 called the legacy 173004552 scorer")

    monkeypatch.setattr(legacy, "freeze_selection", forbidden)
    monkeypatch.setattr(legacy, "compute_selection", forbidden)


def test_s05_scores_without_legacy_and_has_fixed_dimension_values(monkeypatch):
    from app.services.s05_selection import freeze_selection

    snapshot, daily, flows, financial = inputs()
    before = deepcopy(snapshot)
    forbid_legacy(monkeypatch)
    out = freeze_selection(snapshot, daily, flows, financial, DAY)
    row = next(r for r in out["rows"] if r["symbol"] == "600002.SH")
    assert row["s02_tech_score"] == pytest.approx(29.)
    assert row["s02_fund_score"] == pytest.approx(73.77777777777777)
    assert row["s02_emotion_score"] == pytest.approx(20.76923076923077)
    assert row["s02_fundamental_score"] == pytest.approx(53.888888888888886)
    assert row["s02_score"] == pytest.approx(
        .4 * 29 + .3 * 73.77777777777777 + .25 * 20.76923076923077 + .05 * 53.888888888888886)
    assert out["rows"][0]["symbol"] == "600002.SH"
    assert snapshot == before


def test_independent_scorer_preserves_neutral_missing_top50_and_ties():
    from app.services.s05_scoring import freeze_selection

    snapshot, daily, _, _ = inputs(52)
    out = freeze_selection(snapshot, daily, pl.DataFrame(), {}, DAY)
    assert [r["symbol"] for r in out["rows"]] == [f"600{i:03d}.SH" for i in range(1, 51)]
    assert [r["s02_rank"] for r in out["rows"]] == list(range(1, 51))
    assert all(r["s02_fund_score"] == r["s02_fundamental_score"] == 50 for r in out["rows"])
    assert out["score_coverage"]["candidate_total"] == out["score_coverage"]["scorable_total"] == 52
    assert len(out["score_coverage"]["missing_components"]) == 52


def test_independent_scorer_uses_prior_completed_date_and_prior_announcements():
    from app.services.s05_scoring import freeze_selection

    snapshot, daily, flows, financial = inputs()
    baseline = freeze_selection(snapshot, daily, flows, financial, DAY)
    future = daily.filter(pl.col("date") == SOURCE).with_columns(
        pl.lit(DAY).alias("date"), pl.lit(999.).alias("close"),
        pl.lit(999.).alias("raw_close"), pl.lit(1000.).alias("high"))
    future_metrics = financial["metrics"].with_columns(
        pl.lit(SOURCE).alias("announce_date"), pl.lit(999.).alias("roe"))
    financial["metrics"] = pl.concat([financial["metrics"], future_metrics])
    out = freeze_selection(snapshot, pl.concat([daily, future]), flows, financial, DAY)
    assert out["rows"] == baseline["rows"]
    assert str(DAY) not in {r["date"] for r in out["market_breadth"]}


@pytest.mark.parametrize("corrupt", ["duplicate", "reference", "range"])
def test_independent_scorer_isolates_stock_errors_and_fails_when_all_invalid(corrupt):
    from app.services.s05_scoring import freeze_selection

    snapshot, daily, flows, financial = inputs()
    symbol = snapshot["rows"][0]["symbol"]
    if corrupt == "duplicate":
        daily = pl.concat([daily, daily.filter((pl.col("symbol") == symbol) & (pl.col("date") == SOURCE))])
    elif corrupt == "reference":
        snapshot["rows"][0]["reference_close"] = 9.
    else:
        daily = daily.with_columns(pl.when(pl.col("symbol") == symbol).then(9.).otherwise(pl.col("high")).alias("high"))
    out = freeze_selection(snapshot, daily, flows, financial, DAY)
    assert len(out["rows"]) == 1
    assert symbol in out["score_coverage"]["invalid_symbols"]
    assert "S05" in out["score_coverage"]["invalid_symbols"][symbol]
    snapshot["rows"][1]["reference_close"] = 9.
    with pytest.raises(ValueError, match=r"S05.*全部"):
        freeze_selection(snapshot, daily, flows, financial, DAY)


def test_independent_scores_match_reference_on_full_missing_and_trending_inputs():
    from app.services.s02_173004552_selection import freeze_selection as reference
    from app.services.s05_scoring import freeze_selection

    snapshot, daily, flows, financial = inputs()
    daily = daily.with_columns(
        pl.when(pl.col("symbol") == "600001.SH").then(
            10. - (pl.lit(SOURCE) - pl.col("date")).dt.total_days() * .01
        ).otherwise(pl.col("close")).alias("close"))
    for funds, reports in ((flows, financial), (pl.DataFrame(), {})):
        old = reference(snapshot, daily, funds, reports, DAY, include_missing_components=True)
        new = freeze_selection(snapshot, daily, funds, reports, DAY)
        assert new["rows"] == sorted(
            [r for r in old["rows"] if r.get("s02_rank")], key=lambda r: (r["s02_rank"], r["symbol"]))
        assert new["score_coverage"] == old["score_coverage"]
        assert new["market_breadth"] == old["market_breadth"]


def test_real_parquet_selection_loads_own_scorer_and_observed_provenance(tmp_path, monkeypatch):
    from app.services import financial_sync, s03_selection
    from app.services.related_market_data import DATASET_IDS
    from app.services.s05_selection import compute_selection, freeze_source

    snapshot, daily, flows, financial = inputs()
    for day, frame in daily.partition_by("date", as_dict=True).items():
        path = tmp_path / f"kline_daily_enriched/date={day[0]}/part.parquet"
        path.parent.mkdir(parents=True)
        frame.write_parquet(path)
    calls = []

    def extension(_root, columns, start, end, observed, *, config_id):
        calls.append((config_id, start, end, observed))
        if config_id == DATASET_IDS["sina_money_flow"]:
            return flows, {"dataset": config_id, "observed": observed.isoformat()}
        return pl.DataFrame([{
            "symbol": "600002.SH", "title": "测试公司\uFF1A权益分派实施公告",
            "display_time": "2026-11-24T20:00:00+08:00",
        }]), {"dataset": config_id}

    monkeypatch.setattr(s03_selection, "_extension", extension)
    monkeypatch.setattr(financial_sync, "read_financials_as_of", lambda _root, key, _at: financial[key])
    monkeypatch.setattr(financial_sync, "latest_financial_revision_meta", lambda _root, key, _at: {"key": key})
    forbid_legacy(monkeypatch)
    dependency = freeze_source(snapshot, SOURCE, DAY)
    out = compute_selection(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))),
                            SOURCE, DAY, dependency, OBSERVED)
    assert len(out["rows"]) == 2
    blocked = next(r for r in out["rows"] if r["symbol"] == "600002.SH")
    assert blocked["s02_entry_exclusion"] == "recent_distribution_notice"
    assert out["score_coverage"]["excluded_for_entry"] == ["600002.SH"]
    assert out["score_provenance"]["scoring_implementation"] == "s05_independent_four_dimensions_v1"
    assert out["score_provenance"]["observed_cutoff"] == OBSERVED.isoformat()
    assert all(end == SOURCE and at == OBSERVED for _, _, end, at in calls)
    assert out["source_selection"] == dependency and "identity" not in out
    assert out["scheduled_for"] == f"{SOURCE}T22:00:00+08:00"
