"""S03 consumes the frozen 20:00 S02 archive and its own bounded window."""
# ruff: noqa: RUF001 -- Exercise fullwidth announcement issuer punctuation.
from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.daily_selections import due_window

SOURCE = date(2026, 11, 20)
TARGET = date(2026, 11, 23)
S02 = "custom_mvxxcp88"
S03 = "custom_mvxxcs03"


def test_saved_optional_metrics_do_not_replace_five_factor_score_or_order():
    from types import SimpleNamespace

    from app.services.s03_selection import enrich_candidates

    def enrich(rows, params, *, daily, as_of):
        assert params == {"period": 30}
        return rows.reverse().with_columns(
            pl.lit(999.).alias("score"), pl.lit(2.).alias("atr14"),
            pl.lit(.1).alias("atr14_previous_close"))

    strategy = SimpleNamespace(enrich_selection_fn=enrich)
    engine = SimpleNamespace(get=lambda sid: strategy,
                             resolve_params=lambda *args: {"period": 30})
    ranked = {"rows": [{"symbol": "600002.SH", "score": 80., "selection_rank": 1},
                       {"symbol": "600001.SH", "score": 70., "selection_rank": 2}]}
    result = enrich_candidates(
        engine, SimpleNamespace(daily_history=None, as_of=TARGET), ranked, {})
    assert [(r["symbol"], r["score"], r["selection_rank"]) for r in result["rows"]] == [
        ("600002.SH", 80., 1), ("600001.SH", 70., 2)]
    assert all(row["atr14_previous_close"] == .1 for row in result["rows"])


@pytest.mark.parametrize("hour,minute,second,sid,expected", [
    (19, 59, 59, S02, None),
    (20, 0, 0, S02, (SOURCE, TARGET)),
    (21, 0, 0, S02, (SOURCE, TARGET)),
    (21, 0, 1, S02, None),
    (20, 59, 59, S03, None),
    (21, 0, 0, S03, (SOURCE, TARGET)),
    (22, 0, 0, S03, (SOURCE, TARGET)),
    (22, 0, 1, S03, None),
])
def test_independent_windows(hour, minute, second, sid, expected):
    now = datetime(2026, 11, 20, hour, minute, second, tzinfo=CN_TZ)
    assert due_window(now, monitored_only=True, strategy_id=sid) == expected


def source_snapshot():
    return {"as_of": str(TARGET), "source_date": str(SOURCE), "rows": [],
            "identity": "saved-s02", "scheduled_for": "2026-11-20T20:00:00+08:00",
            "generated_at": "2026-11-20T20:12:00+08:00"}


def test_source_dependency_rejects_late_reconstruction_and_wrong_dates():
    from app.services.s03_selection import freeze_source
    value = freeze_source(source_snapshot(), SOURCE, TARGET)
    assert value["strategy_id"] == S02
    assert value["snapshot"]["rows"] == []
    for field, invalid in [
        ("source_date", "2026-11-19"), ("as_of", "2026-11-24"),
        ("scheduled_for", "2026-11-20T21:00:00+08:00"),
        ("generated_at", "2026-11-20T21:00:01+08:00"),
    ]:
        with pytest.raises(ValueError):
            freeze_source({**source_snapshot(), field: invalid}, SOURCE, TARGET)


def test_empty_dependency_needs_no_scoring_data():
    from app.services.s03_selection import rank_candidates
    result = rank_candidates(source_snapshot(), pl.DataFrame(), {}, pl.DataFrame(),
                             {}, pl.DataFrame(), SOURCE)
    assert result["rows"] == []
    assert result["coverage"]["candidate_total"] == 0
    assert result["scoring_policy"] == "s03_five_dimensions_v1"


def test_composite_weights_and_score_bounds():
    from app.services.s03_selection import composite_score, scale
    assert composite_score(100, 0, 0, 0, 0) == 40
    assert composite_score(10, 20, 30, 40, 50) == 23
    assert scale(None, 0, 1) is None
    assert scale(float("nan"), 0, 1) is None
    assert scale(-1, 0, 1) == 0
    assert scale(2, 0, 1) == 100


def test_top30_is_stable_preserves_dependency_fields_and_records_missing():
    from app.services.s03_selection import ranked_result
    rows = [{"symbol": f"{i:06}.SH", "score": 50., "atr14": 2.}
            for i in reversed(range(35))]
    result = ranked_result(rows, {"999999.SH": "缺资金20日"}, 36)
    assert len(result["rows"]) == 30
    assert [r["symbol"] for r in result["rows"]] == [f"{i:06}.SH" for i in range(30)]
    assert result["rows"][0]["atr14"] == 2.
    assert result["coverage"]["candidate_total"] == 36
    assert result["coverage"]["scorable_total"] == 35
    assert result["coverage"]["invalid_symbols"] == {"999999.SH": "缺资金20日"}
    with pytest.raises(ValueError):
        ranked_result([], {"x": "缺数据"}, 1)


def scoring_inputs():
    from datetime import timedelta
    days = [SOURCE - timedelta(days=i) for i in reversed(range(65))]
    daily = pl.DataFrame([{"symbol": "600001.SH", "date": day,
                          "close": 10. + i / 100, "raw_close": 10. + i / 100,
                          "high": 11., "low": 9., "amount": 1000., "name": "测试公司"}
                         for i, day in enumerate(days)])
    flows = pl.DataFrame([{"symbol": "600001.SH", "date": day,
                          "super_large_net_inflow": 20., "large_net_inflow": 30.}
                         for day in days[-20:]])
    financial = {
        "metrics": pl.DataFrame([{"symbol": "600001.SH", "period_end": "2026-09-30",
                                 "announce_date": "2026-10-30", "net_income_yoy": 20.,
                                 "roe": 10., "debt_to_asset_ratio": 40., "eps_basic": .5}]),
        "income": pl.DataFrame([{"symbol": "600001.SH", "period_end": "2026-09-30",
                                "announce_date": "2026-10-30", "net_income": 100.}]),
        "cash_flow": pl.DataFrame([{"symbol": "600001.SH", "period_end": "2026-09-30",
                                   "announce_date": "2026-10-30",
                                   "net_operating_cash_flow": 120.}]),
    }
    notices = pl.DataFrame([
        {"symbol": "600001.SH", "date": "2026-11-19", "title": "测试公司：中标合同",
         "announcement_id": "good"},
        {"symbol": "600001.SH", "date": "2026-11-19", "title": "其他公司：退市",
         "announcement_id": "wrong"},
        {"symbol": "600001.SH", "date": "2026-11-20", "title": "测试公司：退市",
         "announcement_id": "same-day"},
    ])
    snapshot = {**source_snapshot(), "rows": [{"symbol": "600001.SH", "atr14": 2.}]}
    return snapshot, daily, financial, flows, {"breadth": .6}, notices


def test_scoring_uses_actual_ratios_weights_and_issuer_time_filter():
    from app.services.s03_selection import composite_score, rank_candidates
    result = rank_candidates(*scoring_inputs(), SOURCE)
    row = result["rows"][0]
    assert row["fund_score"] == pytest.approx(73.7777777778)
    assert row["news_score"] == 60
    assert row["scoring_inputs"]["news_ids"] == ["good"]
    assert row["scoring_inputs"]["flow20"] == .05
    assert row["score"] == pytest.approx(composite_score(
        row["tech_score"], row["fund_score"], row["emotion_score"],
        row["fundamental_score"], row["news_score"]))
    assert row["atr14"] == 2.


def test_missing_flow_is_unknown_and_later_financial_report_is_not_used():
    from app.services.s03_selection import rank_candidates
    snapshot, daily, financial, flows, market, notices = scoring_inputs()
    with pytest.raises(ValueError, match="资金"):
        rank_candidates(snapshot, daily, financial, flows.head(19), market, notices, SOURCE)
    financial["metrics"] = financial["metrics"].with_columns(
        pl.lit("2026-11-21").alias("announce_date"))
    with pytest.raises(ValueError, match="财务"):
        rank_candidates(snapshot, daily, financial, flows, market, notices, SOURCE)


def test_bad_stock_does_not_stop_valid_candidates_or_duplicate_notice_score():
    from app.services.s03_selection import rank_candidates
    snapshot, daily, financial, flows, market, notices = scoring_inputs()
    snapshot["rows"].append({"symbol": "000001.SZ"})
    notices = pl.concat([notices, notices.head(1)])
    result = rank_candidates(snapshot, daily, financial, flows, market, notices, SOURCE)
    assert result["coverage"]["candidate_total"] == 2
    assert result["coverage"]["scorable_total"] == 1
    assert result["coverage"]["invalid_symbols"] == {"000001.SZ": "缺完整60日日线或日线重复"}
    assert result["rows"][0]["news_score"] == 60


def test_compute_route_consumes_empty_frozen_source_without_loading_market():
    from app.services.daily_selections import compute_selections
    from app.services.s03_selection import freeze_source
    result = compute_selections(None, None, SOURCE, TARGET, [S03], {},
        now=datetime(2026, 11, 20, 21, tzinfo=CN_TZ),
        source_selection=freeze_source(source_snapshot(), SOURCE, TARGET))
    assert result["errors"] == {}
    assert result["selections"][S03]["rows"] == []
    assert result["selections"][S03]["scheduled_for"] == "2026-11-20T21:00:00+08:00"


def test_extension_explicit_dataset_avoids_mixing_sources_and_excludes_future_observation(tmp_path):
    from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
    from app.services.s03_selection import _extension
    fields = [ExtField(key) for key in ("symbol", "date", "observed_at",
                                       "super_large_net_inflow", "large_net_inflow")]
    for sid in ("first", "second"):
        ExtConfigStore(tmp_path).upsert(ExtConfig(sid, sid, "timeseries", fields))
        root = tmp_path / "ext_data" / sid / "timeseries" / f"date={SOURCE}"
        root.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "date": [str(SOURCE)] * 2,
                      "observed_at": ["2026-11-20T20:00:00+08:00", "2026-11-21T20:00:00+08:00"],
                      "super_large_net_inflow": [1., 2.], "large_net_inflow": [2., 3.]}).write_parquet(
                          root / "part.parquet")
    frame, evidence = _extension(tmp_path, ["super_large_net_inflow", "large_net_inflow"],
        SOURCE, SOURCE, datetime(2026, 11, 20, 21, tzinfo=CN_TZ), config_id="first")
    assert frame["symbol"].to_list() == ["600001.SH"]
    assert evidence["config_id"] == "first"
    with pytest.raises(ValueError, match="不唯一"):
        _extension(tmp_path, ["super_large_net_inflow", "large_net_inflow"],
                   SOURCE, SOURCE, datetime(2026, 11, 20, 21, tzinfo=CN_TZ))
