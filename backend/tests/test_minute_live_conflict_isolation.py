from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.instrument_suspension import SCHEMA, TIMESTAMP_SCHEMA
from app.services import kline_sync
from app.services.minute_refresh import MinuteRefreshService
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("priority", [False, True])
@pytest.mark.parametrize("mode", ["full", "increment"])
def test_live_conflicting_stock_does_not_block_healthy_minutes(
    tmp_path, monkeypatch, caplog, priority, mode,
):
    evidence = tmp_path / "instrument_status"
    evidence.mkdir()
    pl.DataFrame([{
        "symbol": "000001.SZ", "effective_date": datetime(2026, 1, 5).date(),
        "announced_on": datetime(2026, 1, 4).date(),
        "announced_at": datetime(2026, 1, 4, 18),
        "is_suspended": True, "source_url": "https://www.szse.cn/notice.pdf",
    }], schema={**SCHEMA, **TIMESTAMP_SCHEMA}).write_parquet(evidence / "suspensions.parquet")
    frame = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "datetime": [datetime(2026, 1, 5, 14, 30)] * 2,
        "open": [10., 8.], "high": [10., 8.], "low": [10., 8.],
        "close": [10., 8.], "volume": [100., 200.], "amount": [1000., 1600.],
    })
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(repo, "get_instruments", lambda: frame.select("symbol"))
    monkeypatch.setattr(MinuteRefreshService, "_select_mode", lambda _: mode)
    monkeypatch.setattr(MinuteRefreshService, "_resolve_custom", lambda _: (
        SimpleNamespace(get_intraday_latest=lambda: None), "test"))
    monkeypatch.setattr(MinuteRefreshService, "priority_symbols",
                        lambda _: frame["symbol"].to_list() if priority else [])
    monkeypatch.setattr(kline_sync, "fetch_intraday_custom_batch", lambda *a: (frame, 1))
    monkeypatch.setattr(kline_sync, "fetch_intraday_custom_latest", lambda *a, **kw: (frame, 1))
    published = []
    service = MinuteRefreshService(repo)
    service.set_app_state(SimpleNamespace(public_publisher=SimpleNamespace(
        publish_priority=lambda at: published.append("priority"),
        request_minute_publish=lambda at: published.append("round"),
    )))
    service._run_round()
    stored = pl.read_parquet(tmp_path / "kline_minute/date=2026-01-05/part.parquet")
    assert stored["symbol"].to_list() == ["600000.SH"]
    assert stored["datetime"].to_list() == [datetime(2026, 1, 5, 14, 30)]
    assert stored["volume"].to_list() == [200.]
    assert "round" in published
    assert ("priority" in published) is priority
    assert service._state.rounds == 1
    assert "000001.SZ" in caplog.text and "冲突" in caplog.text
