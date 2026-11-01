"""Complete scope pools are independent of strategy signals and execution."""
from dataclasses import replace
from datetime import date

import polars as pl
import pytest

from app.cell.context import AccessError
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.engine import StrategyDataContext, StrategyEngine

DAY = date(2026, 10, 26)
PRIOR = date(2026, 10, 23)
BF = {"target_mode": "selected", "boards": ["沪主板"], "enabled": True,
      "data_reference": "previous_trade_day", "float_cap_min": None, "float_cap_max": 20e9,
      "market_cap_min": None, "amount_min": None, "exclude_new_days": 0}


def frames():
    return pl.DataFrame([
        {"symbol": symbol, "date": day, "close": price, "volume": volume,
         "is_suspended": suspended, "float_shares": 1e9}
        for symbol, day, price, volume, suspended in [
            ("600000.SH", PRIOR, 10.0, 100.0, False),
            ("600001.SH", PRIOR, 15.0, 100.0, False),
            ("600002.SH", PRIOR, 25.0, 100.0, False),
            ("600000.SH", DAY, 100.0, 100.0, False),
            ("600001.SH", DAY, 15.0, 0.0, True),
            ("600002.SH", DAY, 5.0, 100.0, False),
        ]
    ])


@pytest.mark.parametrize("enabled", [False, True])
def test_suspension_exclusion_is_independent_and_uses_requested_day(enabled):
    current = frames().filter(pl.col("date") == DAY)
    filtered = StrategyEngine._apply_basic_filter(current, {
        "enabled": enabled, "exclude_suspended": True,
    }, as_of=DAY)
    assert filtered["symbol"].to_list() == ["600000.SH", "600002.SH"]
    assert filtered["close"].to_list() == [100.0, 5.0]


def test_prior_future_and_zero_volume_unknown_are_not_declared_suspended():
    prior = frames().filter(pl.col("date") == PRIOR).with_columns(pl.lit(True).alias("is_suspended"))
    future = prior.with_columns(pl.lit(date(2026, 10, 27)).alias("date"))
    unknown = frames().filter(pl.col("date") == DAY).with_columns(
        pl.lit(None, dtype=pl.Boolean).alias("is_suspended"), pl.lit(0.0).alias("volume"))
    for rows in (prior, future, unknown):
        assert StrategyEngine._apply_basic_filter(rows, {
            "enabled": False, "exclude_suspended": True,
        }, as_of=DAY).height == 3


@pytest.fixture
def engine(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"], "limit": 1}
BASIC_FILTER = {"enabled": False}
def filter(df, params):
    return pl.col("close") > 90
''')
    return StrategyEngine([tmp_path])


def test_pool_precedes_strategy_filter_display_limit_and_caller_pool(engine):
    history = frames()
    context = StrategyDataContext("stock", "1d", DAY,
        current=history.filter(pl.col("date") == DAY), history=history)
    result = engine.run("sample", context, pool=["600000.SH"], overrides={"basic_filter": BF})
    assert result.total == 1
    pool = getattr(result, "daily_stock_pool", None)
    assert pool is not None, "execution did not capture the complete base pool"
    assert pool["symbols"] == ["600000.SH", "600001.SH"]
    assert pool["reference_date"] == str(PRIOR)
    assert pool["total"] == 2


def test_pool_builder_needs_no_minute_algorithm_or_previous_portfolio(engine):
    from app.strategy.daily_pool import build_pool

    engine.get("sample").meta["minute_execution"] = "momentum_capacity_v1"
    history = frames()
    context = StrategyDataContext("stock", "1m", DAY,
        current=history.filter(pl.col("date") == DAY), daily_history=history)
    pool = build_pool(engine, "sample", context, overrides={"basic_filter": {**BF, "exclude_suspended": True}})
    assert pool["symbols"] == ["600000.SH"]
    assert pool["suspended_count"] == 1


def test_halted_stock_missing_current_row_remains_in_unchecked_previous_day_pool(engine):
    from app.strategy.daily_pool import build_pool

    history = frames().filter(~((pl.col("date") == DAY) & (pl.col("symbol") == "600001.SH")))
    current = history.filter((pl.col("date") == DAY) & (pl.col("symbol") != "600001.SH"))
    pool = build_pool(engine, "sample", StrategyDataContext(
        "stock", "1d", DAY, current=current, history=history), overrides={"basic_filter": BF})
    assert pool["symbols"] == ["600000.SH", "600001.SH"]
    assert pool["unknown_suspension_count"] == 1


def test_disabled_numeric_filters_do_not_remove_a_stock_without_today_quote(engine):
    from app.strategy.daily_pool import build_pool
    history = frames().filter(~((pl.col("date") == DAY) & (pl.col("symbol") == "600001.SH")))
    current = history.filter((pl.col("date") == DAY) & (pl.col("symbol") != "600001.SH"))
    pool = build_pool(engine, "sample", StrategyDataContext(
        "stock", "1d", DAY, current=current, history=history),
        overrides={"basic_filter": {"enabled": False, "exclude_suspended": False}})
    assert pool["symbols"] == ["600000.SH", "600001.SH", "600002.SH"]


@pytest.mark.parametrize("extra", [
    {"price_max": 1}, {"market_cap_max": 1}, {"amount_min": 1e12},
    {"turnover_min": 99}, {"change_pct_min": 99}, {"exclude_new_days": 999},
])
def test_pool_ignores_other_numeric_filters_and_their_missing_dependencies(engine, extra):
    from app.strategy.daily_pool import build_pool

    history = frames()
    pool = build_pool(engine, "sample", StrategyDataContext(
        "stock", "1d", DAY, current=history.filter(pl.col("date") == DAY), history=history),
        overrides={"basic_filter": {**BF, **extra}})
    assert pool["symbols"] == ["600000.SH", "600001.SH"]
    assert not set(extra).intersection(pool["basic_filter"])
    assert pool["definition_version"] == "scope_float_cap_v2"


def test_remaining_numeric_filters_still_restrict_strategy_selection(engine):
    from app.strategy.daily_pool import build_pool

    history = frames()
    current = history.filter(pl.col("date") == DAY)
    config = {**BF, "price_max": 12}
    context = StrategyDataContext("stock", "1d", DAY, current=current, history=history)
    assert build_pool(engine, "sample", context, overrides={"basic_filter": config})["symbols"] == [
        "600000.SH", "600001.SH"]
    assert engine._apply_basic_filter(current, config, history=history, as_of=DAY)["symbol"].to_list() == [
        "600000.SH"]
    assert config["price_max"] == 12


@pytest.mark.parametrize("reference,expected", [
    ("previous_trade_day", ["600000.SH", "600001.SH"]),
    ("signal_day", ["600001.SH", "600002.SH"]),
])
def test_pool_float_cap_uses_raw_reference_close_and_inclusive_bounds(engine, reference, expected):
    from app.strategy.daily_pool import build_pool

    history = frames().with_columns(
        pl.col("close").alias("raw_close"), pl.lit(1.0).alias("close"))
    pool = build_pool(engine, "sample", StrategyDataContext(
        "stock", "1d", DAY, current=history.filter(pl.col("date") == DAY), history=history),
        overrides={"basic_filter": {**BF, "data_reference": reference,
                                   "float_cap_min": 5e9, "float_cap_max": 15e9}})
    assert pool["symbols"] == expected
    assert pool["reference_date"] == str(PRIOR if reference == "previous_trade_day" else DAY)


@pytest.mark.parametrize("missing", ["session", "float_shares", "raw_close"])
def test_float_cap_pool_requires_exact_reference_data(engine, missing):
    from app.strategy.daily_pool import build_pool

    history = frames().with_columns(pl.col("close").alias("raw_close"))
    if missing == "session":
        history = history.with_columns(
            pl.when(pl.col("date") == PRIOR).then(pl.lit(date(2026, 10, 22)))
            .otherwise(pl.col("date")).alias("date"))
    else:
        history = history.with_columns(
            pl.when(pl.col("date") == PRIOR).then(None).otherwise(pl.col(missing)).alias(missing))
    with pytest.raises(ValueError, match=r"参考交易日|缺少"):
        build_pool(engine, "sample", StrategyDataContext(
            "stock", "1d", DAY, current=history.filter(pl.col("date") == DAY), pool_history=history),
            overrides={"basic_filter": BF})


@pytest.mark.parametrize("enabled", [False, True])
def test_scope_only_pool_needs_no_history_or_other_numeric_data(engine, enabled):
    from app.strategy.daily_pool import build_pool

    current = pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "date": [DAY, DAY]})
    pool = build_pool(engine, "sample", StrategyDataContext("stock", "1d", DAY, current=current),
        overrides={"basic_filter": {
            "target_mode": "selected", "boards": ["沪主板"], "enabled": enabled,
            "data_reference": "previous_trade_day", "price_min": 999, "exclude_new_days": 999,
            **({"float_cap_max": 1} if not enabled else {}),
        }})
    assert pool["symbols"] == ["600000.SH"]
    assert pool["basic_filter"]["enabled"] is enabled


def test_disabled_numeric_pool_retains_legacy_board_and_st_scope(engine):
    from app.strategy.daily_pool import build_pool

    current = pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH", "000001.SZ"], "date": [DAY] * 3,
        "is_risk_warning": [False, True, False],
    })
    pool = build_pool(engine, "sample", StrategyDataContext("stock", "1d", DAY, current=current),
        overrides={"basic_filter": {"enabled": False, "boards": ["沪主板"], "exclude_st": True}})
    assert pool["symbols"] == ["600000.SH"]


def test_custom_group_union_and_float_cap_both_determine_membership(engine):
    from app.strategy.daily_pool import build_pool

    history = frames().with_columns(
        pl.when(pl.col("symbol") == "600001.SH").then(pl.lit("000001.SZ"))
        .otherwise(pl.col("symbol")).alias("symbol"))
    engine.get("sample").basic_filter = {**BF, "float_cap_max": 10e9}
    pool = build_pool(engine, "sample", StrategyDataContext(
        "stock", "1d", DAY, current=history.filter(pl.col("date") == DAY), history=history),
        overrides={"basic_filter": {"include_custom": True, "target_symbols": ["000001.SZ"],
                                   "float_cap_max": 20e9}})
    assert pool["symbols"] == ["000001.SZ", "600000.SH"]
    assert engine.get("sample").basic_filter["float_cap_max"] == 10e9


def key():
    return ResultIdentity("sample", "示例", "v1", "{}", "{}", "null", "stock", "1d")


def payload(symbols, **changes):
    return {"as_of": str(DAY), "strategy_id": "sample", "symbols": symbols,
            "total": len(symbols), "basic_filter": BF, "reference_date": str(PRIOR), **changes}


def test_pool_persistence_reuses_snapshot_and_retains_variants_and_empty_success(tmp_path):
    from app.services.daily_stock_pools import DailyStockPools

    store = DailyStockPools(tmp_path)
    first = store.save(key(), payload(["600000.SH"]), started_at=10)
    assert store.save(key(), payload(["600000.SH"]), started_at=30)["id"] == first["id"]
    variant = store.save(key(), payload([]), started_at=40)
    store.save(key(), payload(["600009.SH"]), started_at=20)
    assert store.read("sample", str(DAY))["id"] == variant["id"]
    assert store.read("sample", str(DAY))["symbols"] == []
    assert store.read("sample", str(PRIOR)) is None
    with StrategyResults(tmp_path)._local() as conn:
        assert conn.execute("SELECT count(*) FROM daily_stock_pools").fetchone()[0] == 3


def test_old_and_new_definition_snapshots_coexist_without_relabelling_history(tmp_path):
    from app.services.daily_stock_pools import DailyStockPools

    store = DailyStockPools(tmp_path)
    legacy = store.save(key(), payload(["600000.SH"]), started_at=10)
    latest = store.save(key(), payload(["600000.SH"], definition_version="scope_float_cap_v2"),
                        started_at=20)
    assert legacy["id"] != latest["id"]
    assert "definition_version" not in store.read("sample", DAY, record_id=legacy["id"])
    assert store.read("sample", DAY)["definition_version"] == "scope_float_cap_v2"
    assert store.save(key(), payload(["600000.SH"], definition_version="scope_float_cap_v2"),
                      started_at=30)["id"] == latest["id"]
    store.save(key(), payload(["600000.SH"]), started_at=15)
    assert store.read("sample", DAY)["id"] == latest["id"]


def test_compute_scope_only_succeeds_while_missing_float_reference_is_per_strategy_error(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from app.config import settings
    from app.multitenancy.compute_worker import execute_request
    from app.services.screener import ScreenerService
    from app.tickflow import repository

    original_iterdir = type(tmp_path).iterdir
    monkeypatch.setattr(type(tmp_path), "iterdir",
                        lambda p: iter(()) if str(p) == "/market" else original_iterdir(p))
    monkeypatch.setattr(settings, "data_dir", settings.data_dir)
    monkeypatch.setattr(repository, "DataStore",
                        lambda *a, **k: SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(repository, "KlineRepository", lambda _: SimpleNamespace())
    current = pl.DataFrame({"symbol": ["600000.SH"], "date": [DAY]})
    monkeypatch.setattr(ScreenerService, "build_daily_pool_context",
                        lambda *a, **k: StrategyDataContext("stock", "1d", DAY, current=current))
    code = '''
META = {"id": "sample", "name": "示例", "asset_types": ["stock"]}
BASIC_FILTER = {"enabled": False}
def filter(df, params):
    return True
'''
    result = execute_request({
        "kind": "daily_stock_pools", "as_of": str(DAY), "strategy_ids": ["sample", "float_sample"],
        "sources": [{"id": sid, "source": "custom", "code": code.replace('"sample"', f'"{sid}"')}
                    for sid in ("sample", "float_sample")],
        "overrides_map": {"float_sample": {"basic_filter": BF}},
    }, tmp_path)
    assert result["pools"]["sample"]["symbols"] == ["600000.SH"]
    assert set(result["errors"]) == {"float_sample"}
    assert "参考数据" in result["errors"]["float_sample"]


def test_result_and_pool_share_transaction_and_delete_fence(tmp_path):
    from app.services.daily_stock_pools import DailyStockPools

    store = StrategyResults(tmp_path)
    result = {"as_of": str(DAY), "rows": [], "daily_stock_pool": payload(["600000.SH"])}
    store.save(key(), result, started_at=10)
    assert DailyStockPools(tmp_path).read("sample", str(DAY))["total"] == 1
    with pytest.raises(AccessError):
        store.save(key(), {**result, "rows": [{"score": float("nan")}],
                          "daily_stock_pool": payload([])}, started_at=20)
    assert DailyStockPools(tmp_path).read("sample", str(DAY))["total"] == 1
    with store.deleting_strategy("sample"):
        pass
    assert DailyStockPools(tmp_path).read("sample", str(DAY)) is None
    with pytest.raises(AccessError):
        DailyStockPools(tmp_path).save(key(), payload([]), started_at=30)
    store.activate("sample")
    DailyStockPools(tmp_path).save(replace(key(), storage_generation=1), payload([]), started_at=40)


def test_matrix_suspension_mask_is_independent_of_numeric_reference():
    from app.backtest.matrix import build_basic_filter_mask, build_market_data_matrix

    rows = frames().with_columns(
        pl.col("close").alias("open"), pl.col("close").alias("high"), pl.col("close").alias("low"))
    matrix = build_market_data_matrix(rows, field_columns={"is_suspended"})
    assert build_basic_filter_mask(matrix, {"enabled": False, "exclude_suspended": True})[-1].tolist() == [
        True, False, True]


def test_local_daily_generation_archives_all_strategies_without_running_algorithms(engine, tmp_path):
    from types import SimpleNamespace

    from app.services.daily_stock_pools import DailyStockPools, generate_local

    history = frames()
    context = StrategyDataContext("stock", "1d", DAY,
        current=history.filter(pl.col("date") == DAY), pool_history=history)
    engine.get("sample").filter_fn = lambda *_: pytest.fail("trading algorithm executed")
    assert generate_local(engine, SimpleNamespace(build_daily_pool_context=lambda _: context),
                          tmp_path, DAY) == {"saved": ["sample"], "errors": {}}
    assert DailyStockPools(tmp_path).read("sample", DAY)["total"] == 3


def test_pool_read_api_pages_complete_membership_without_running_strategy(engine, tmp_path):
    from types import SimpleNamespace

    from app.api.screener import daily_stock_pool
    from app.services.daily_stock_pools import DailyStockPools

    DailyStockPools(tmp_path).save(key(), payload(["600000.SH", "600001.SH"]), started_at=10)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=engine, repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))),
        state=SimpleNamespace())
    response = daily_stock_pool(request, "sample", DAY, limit=1, offset=1)
    assert response["pool"]["total"] == 2
    assert response["pool"]["symbols"] == ["600001.SH"]


def test_daily_pool_compute_operation_is_allowed_by_client_and_broker():
    from app.multitenancy.executor import KINDS
    assert "daily_stock_pools" in KINDS
