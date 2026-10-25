"""策略目标范围、动态自选成员与删除保护。"""
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import watchlist as watchlist_api
from app.config import settings
from app.services import watchlist
from app.strategy import config
from app.strategy.engine import StrategyEngine


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "multitenancy_enabled", False)
    return tmp_path


def frame():
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ", "300750.SZ", "688001.SH", "600001.SH"],
        "name": ["浦发", "平安", "宁德", "华兴", "*ST测试"],
        "is_risk_warning": [False, False, False, False, True],
    })


def test_targets_apply_when_numeric_filter_is_disabled(local):
    bf = {"enabled": False, "target_mode": "selected", "boards": ["沪主板"],
          "include_custom": True, "target_symbols": ["300750.SZ"], "exclude_st": True}
    rows = StrategyEngine._apply_basic_filter(frame(), bf)
    assert rows["symbol"].to_list() == ["600000.SH", "300750.SZ"]


def test_all_targets_and_empty_selection_have_distinct_meanings(local):
    df = frame()
    assert StrategyEngine._apply_basic_filter(df, {
        "enabled": False, "target_mode": "all", "exclude_st": False,
    }).height == 5
    assert StrategyEngine._apply_basic_filter(df, {
        "enabled": False, "target_mode": "selected", "boards": [],
        "include_custom": True, "target_symbols": [],
    }).is_empty()


def test_group_members_are_resolved_again_after_changes(local):
    _, group = watchlist.create_group("核心")
    watchlist.add("000001.SZ", group_id=group["id"])
    config.save_override(local, "target", {"basic_filter": {
        "enabled": False, "target_mode": "selected", "boards": [],
        "include_custom": True, "watchlist_group_ids": [group["id"]],
    }})
    first = config.load_override(local, "target")
    assert first["basic_filter"]["target_symbols"] == ["000001.SZ"]
    watchlist.add("300750.SZ", group_id=group["id"])
    second = config.load_override(local, "target")
    assert set(second["basic_filter"]["target_symbols"]) == {"000001.SZ", "300750.SZ"}
    assert first["basic_filter"]["target_symbols"] == ["000001.SZ"]
    config.save_override(local, "target", second)
    import json
    saved = json.loads((local / "user_data/strategy_overrides/target.json").read_text())
    assert "target_symbols" not in saved["basic_filter"]


def test_referenced_group_cannot_be_deleted_through_service_or_api(local):
    _, group = watchlist.create_group("核心")
    watchlist.add("600000.SH", group_id=group["id"])
    config.save_override(local, "target", {"basic_filter": {
        "target_mode": "selected", "boards": [], "include_custom": True,
        "watchlist_group_ids": [group["id"]],
    }})
    with pytest.raises(ValueError, match="被策略引用"):
        watchlist.delete_group(group["id"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))
    with pytest.raises(HTTPException) as error:
        watchlist_api.delete_group(group["id"], request)
    assert error.value.status_code == 409
    assert watchlist.list_symbols()[0]["group_ids"] == [group["id"]]
    assert watchlist.list_groups()[0]["referenced_by"] == ["target"]
    config.delete_override(local, "target")
    assert watchlist.delete_group(group["id"])[0] == []


def test_save_rejects_missing_target_group_without_replacing_config(local):
    config.save_override(local, "target", {"params": {"x": 2}})
    with pytest.raises(ValueError, match="自选分组不存在"):
        config.save_override(local, "target", {"basic_filter": {
            "target_mode": "selected", "include_custom": True,
            "watchlist_group_ids": ["missing"], "boards": [],
        }})
    assert config.load_override(local, "target") == {"params": {"x": 2}}


def test_group_creation_can_import_codes_atomically(local, monkeypatch):
    groups, group = watchlist.create_group("CSV", "sky", symbols=["600000.SH", "000001.SZ"])
    assert groups == [group]
    assert {r["symbol"] for r in watchlist.list_symbols()} == {"600000.SH", "000001.SZ"}
    assert all(r["group_ids"] == [group["id"]] for r in watchlist.list_symbols())
    before = watchlist.list_symbols()
    monkeypatch.setattr(watchlist, "_write_entries", lambda df: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        watchlist.create_group("失败", symbols=["300750.SZ"])
    assert [g["name"] for g in watchlist.list_groups()] == ["CSV"]
    assert watchlist.list_symbols() == before


def test_csv_group_import_preserves_existing_notes_and_added_dates(local):
    watchlist.add("600000.SH", note="保留笔记")
    old = watchlist.list_symbols()[0]
    _, group = watchlist.create_group("导入", symbols=["600000.SH"])
    saved = watchlist.list_symbols()[0]
    assert saved == {**old, "group_ids": [group["id"]]}


def test_matrix_targets_match_polars_even_when_numeric_filter_is_disabled():
    from datetime import date

    from app.backtest.matrix import build_basic_filter_mask, build_market_data_matrix
    data = frame().with_columns(pl.lit(date(2026, 10, 23)).alias("date"),
                                *[pl.lit(10.).alias(name) for name in ("open", "high", "low", "close")])
    market = build_market_data_matrix(data, field_columns={"is_risk_warning"})
    bf = {"enabled": False, "target_mode": "selected", "boards": ["沪主板"],
          "include_custom": True, "target_symbols": ["300750.SZ"], "exclude_st": True}
    mask = build_basic_filter_mask(market, bf)
    assert {symbol for symbol, hit in zip(market.symbols, mask[0], strict=True) if hit} == {"600000.SH", "300750.SZ"}


def test_stock_targets_do_not_filter_etfs():
    from app.backtest.strategy import _basic_filter_for_asset
    df = pl.DataFrame({"symbol": ["510300.SH"], "name": ["沪深300ETF"]})
    bf = _basic_filter_for_asset({"enabled": False, "target_mode": "selected",
                                 "boards": ["沪主板"], "exclude_st": True}, "etf")
    assert StrategyEngine._apply_basic_filter(df, bf).height == 1


def test_remote_execution_snapshot_uses_one_group_snapshot_for_composite_children():
    from app.services.strategy_results import execution_snapshot

    engine = SimpleNamespace(
        _strategies={"target": object()}, remote_context=True,
        _override_loader=lambda sid: {"basic_filter": {"target_symbols": ["OLD"]}},
        task=lambda kind: {"kind": kind, "overrides": {"target": {
            "basic_filter": {"target_symbols": ["SNAPSHOT"]},
        }}},
    )
    frozen = execution_snapshot(engine)
    assert frozen._override_loader("target")["basic_filter"]["target_symbols"] == ["SNAPSHOT"]


def test_backtest_resolves_group_ids_when_detail_does_not_include_internal_symbols(local):
    from app.backtest.strategy import StrategyBacktestService

    _, group = watchlist.create_group("回测", symbols=["600000.SH"])
    strategy = SimpleNamespace(basic_filter={"enabled": False})
    config_value = {"basic_filter": {"target_mode": "selected", "boards": [], "include_custom": True,
                                     "watchlist_group_ids": [group["id"]]}}
    assert StrategyBacktestService._effective_basic_filter(strategy, config_value)["target_symbols"] == ["600000.SH"]


@pytest.mark.asyncio
async def test_group_csv_http_accepts_code_only_deduplicates_and_skips_unknowns(local):
    from io import BytesIO

    from fastapi import UploadFile

    directory = local / "instruments"
    directory.mkdir()
    pl.DataFrame({"code": ["600000", "300750"], "symbol": ["600000.SH", "300750.SZ"],
                  "name": ["浦发", "宁德"]}).write_parquet(directory / "instruments.parquet")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=local)),
    )))
    uploaded = UploadFile(filename="codes.csv", file=BytesIO(b"code\n600000\n300750\n600000\n999999\n"))
    result = await watchlist_api.create_group_from_csv(request, name="CSV", file=uploaded, color="sky")
    assert result["imported"] == 2 and result["skipped"] == 1
    assert all(row["group_ids"] == [result["group"]["id"]] for row in watchlist.list_symbols())


@pytest.mark.asyncio
async def test_group_csv_does_not_leave_a_group_when_no_stock_matches(local):
    from io import BytesIO

    from fastapi import UploadFile

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=local)),
    )))
    file = UploadFile(filename="empty.csv", file=BytesIO(b"code\n999999\n"))
    with pytest.raises(HTTPException):
        await watchlist_api.create_group_from_csv(request, name="空", file=file, color="sky")
    assert watchlist.list_groups() == []
