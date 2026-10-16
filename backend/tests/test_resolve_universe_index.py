"""_resolve_universe 持久化四板股票池测试。"""
from datetime import date

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_resolve_universe_uses_only_persisted_four_board_snapshot(repo, tmp_path):
    class _Capset:
        def has(self, cap):
            return True

    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir(exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ", "300001.SZ", "688001.SH", "000001.SH"],
            "name": ["沪主板", "深主板", "创业板", "科创板", "上证指数"],
            "as_of": [date(2026, 10, 16)] * 5,
        }
    ).write_parquet(target)

    universe = daily_pipeline._resolve_universe(_Capset(), repo)
    assert universe == ["000001.SZ", "300001.SZ", "600000.SH", "688001.SH"]


def test_resolve_universe_fails_closed_without_snapshot(repo):
    class _Capset:
        def has(self, cap):
            return True

    with pytest.raises(RuntimeError, match="没有可用的股票池快照"):
        daily_pipeline._resolve_universe(_Capset(), repo)
