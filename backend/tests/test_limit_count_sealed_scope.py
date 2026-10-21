"""涨跌停家数只对「当日最终涨跌停名单内」的标的做五档真假板修正。

depth 快照按当日涨跌停名单采集, 但盘中炸板后名单会缩小: 快照里仍留着
已炸板标的的 sealed=False 记录。这些标的已经不在最终涨停名单内, 若再按
sealed=False 从家数里扣一次, 涨停数会被重复扣减 (55 家显示成 41 家)。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import screener as screener_api
from app.services.market_overview_builder import build_market_overview
from app.services.screener import ScreenerService

_AS_OF = date(2026, 10, 20)


class _Depth:
    """涨停名单内 1 真封 + 1 假封; 另有 2 只已炸板(不在名单)残留 sealed=False。"""

    def __init__(self, ready: bool = True) -> None:
        self._up = {
            "600000.SH": {"sealed": True, "ready": ready},
            "600001.SH": {"sealed": False, "ready": ready},
            "600900.SH": {"sealed": False, "ready": True},
            "600901.SH": {"sealed": False, "ready": True},
        }

    def get_sealed_map(self, _as_of, is_down: bool = False) -> dict:
        return {} if is_down else self._up

    def is_sealed_ready(self, _as_of) -> bool:
        return True

    def get_sealed_age(self, _as_of):
        return 0.0


def _snapshot() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH", "600002.SH", "600900.SH", "600901.SH"],
        "name": ["真封", "假封", "无盘口", "已炸板一", "已炸板二"],
        "close": [11.0, 11.0, 11.0, 10.5, 10.5],
        "change_pct": [0.1, 0.1, 0.1, 0.05, 0.05],
        "amount": [1e8] * 5,
        "turnover_rate": [2.0] * 5,
        "volume": [1e6] * 5,
        "signal_limit_up": [True, True, True, False, False],
        "signal_limit_down": [False] * 5,
        "signal_broken_limit_up": [False, False, False, True, True],
        "consecutive_limit_ups": [1, 1, 1, 0, 0],
    })


@pytest.fixture
def _stub_enriched(monkeypatch):
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", lambda self, d: _snapshot())
    monkeypatch.setattr(ScreenerService, "load_prior_consecutive", lambda self, d, c: pl.DataFrame())
    return _snapshot()


def _ladder_request(tmp_path, ready):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, depth_service=_Depth(ready)))
    )


@pytest.mark.parametrize("ready", [True, False])
def test_ladder_count_only_deducts_fake_limits_in_current_list(tmp_path, _stub_enriched, ready):
    payload = screener_api.limit_ladder(
        _ladder_request(tmp_path, ready), as_of=_AS_OF, direction="up", ext_columns=None
    )

    assert payload["counts_raw"]["up"] == 3
    # 只扣名单内那只假封; 两只已炸板残留记录不能再扣一次
    assert payload["counts"]["up"] == (2 if ready else 3)
    assert payload["sealed_counts_up"] == (
        {"real": 1, "fake": 1, "pending": 1}
        if ready else {"real": 0, "fake": 0, "pending": 3}
    )


@pytest.mark.parametrize("ready", [True, False])
def test_overview_count_only_deducts_fake_limits_in_current_list(tmp_path, _stub_enriched, ready):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        execute_all=lambda *a, **k: [],
    )

    payload = build_market_overview(repo, depth_service=_Depth(ready), as_of=_AS_OF)

    assert payload["limit"]["limit_up"] == (2 if ready else 3)
    assert payload["limit"]["fake_up"] == (1 if ready else 0)
