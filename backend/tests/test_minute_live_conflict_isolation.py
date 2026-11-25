from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.instrument_suspension import SCHEMA, TIMESTAMP_SCHEMA
from app.services import kline_sync
from app.services.minute_refresh import MinuteRefreshService
from app.services.selected_minute_process import _collect
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("collector", ["market", "selected"])
@pytest.mark.parametrize("mode", ["full", "increment"])
def test_live_conflicting_stock_does_not_block_healthy_minutes(
    tmp_path, monkeypatch, caplog, collector, mode,
):
    at = datetime(2026, 1, 5, 14, 30)
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
        "datetime": [at] * 2,
        "open": [10., 8.], "high": [10., 8.], "low": [10., 8.],
        "close": [10., 8.], "volume": [100., 200.], "amount": [1000., 1600.],
    })
    symbols = frame["symbol"].to_list()
    provider = SimpleNamespace(get_intraday_latest=lambda: None)

    def fetch_batch(client, name, members):
        assert client is provider and name == "test" and members == symbols and mode == "full"
        return frame, 1

    def fetch_latest(client, name, *, symbols):
        assert client is provider and name == "test" and symbols == frame["symbol"].to_list()
        assert mode == "increment"
        return frame, 1

    monkeypatch.setattr(kline_sync, "fetch_intraday_custom_batch", fetch_batch)
    monkeypatch.setattr(kline_sync, "fetch_intraday_custom_latest", fetch_latest)
    if collector == "selected":
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda _: (provider, False, None))
        reply = _collect({"data_dir": str(tmp_path), "day": str(at.date()), "provider": "test",
                          "mode": mode, "symbols": symbols}, load_sources=False)
        assert reply["error"] is None and reply["written_rows"] == 1
    else:
        repo = KlineRepository(DataStore(tmp_path))
        monkeypatch.setattr(repo, "get_instruments", lambda: frame.select("symbol"))
        service = MinuteRefreshService(repo)
        monkeypatch.setattr(service, "_select_mode", lambda: mode)
        monkeypatch.setattr(service, "_resolve_custom", lambda: (provider, "test"))
        monkeypatch.setattr("app.services.minute_refresh.cn_now", lambda: at)
        published = []
        service.set_app_state(SimpleNamespace(public_publisher=SimpleNamespace(
            publish_priority=lambda at: published.append("priority"),
            request_minute_publish=lambda at: published.append("round"),
        )))
        service.set_priority("tenant", {"day": str(at.date()), "sequence": 1,
                                        "symbols": symbols, "selected": ["000001.SZ"]})
        service._run_round()
        assert published == ["round"]
        assert service._state.rounds == 1 and service._state.last_rows == 1
    stored = pl.read_parquet(tmp_path / "kline_minute/date=2026-01-05/part.parquet")
    assert stored["symbol"].to_list() == ["600000.SH"]
    assert stored["datetime"].to_list() == [at]
    assert stored["volume"].to_list() == [200.]
    assert "000001.SZ" in caplog.text and "冲突" in caplog.text
