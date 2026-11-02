from datetime import datetime
from types import SimpleNamespace

import pytest

from app.api import contracts, intraday
from app.market_time import CN_TZ


@pytest.mark.parametrize(("wall_clock", "expected_day", "trading"), [
    ("2026-11-01T23:59:59", "2026-11-01", False),
    ("2026-11-02T00:00:00", "2026-11-02", True),
    ("2026-11-02T09:23:00", "2026-11-02", True),
    ("2026-10-06T09:23:00", "2026-10-06", False),
    ("2030-11-04T00:00:00", "2030-11-04", None),
])
@pytest.mark.parametrize("has_service", [False, True])
def test_status_uses_fresh_beijing_clock_not_cached_quotes(monkeypatch, wall_clock, expected_day, trading, has_service):
    now = datetime.fromisoformat(wall_clock).replace(tzinfo=CN_TZ)
    monkeypatch.setattr("app.market_time.cn_now", lambda: now)
    cached = {"enabled": True, "running": True, "symbol_count": 1, "index_symbol_count": 0,
              "quote_age_ms": None, "is_trading_hours": False, "last_fetch_ms": None,
              "market_date": "2026-10-30", "is_trading_day": False}
    service = SimpleNamespace(status=lambda: cached) if has_service else None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=service)))
    response = intraday.status(request)
    assert response.get("market_date") == expected_day
    assert response.get("is_trading_day") is trading
    assert response.get("market_time") == now.isoformat()
    assert response["running"] is has_service
    if has_service:
        assert cached["market_date"] == "2026-10-30"
    assert contracts.QuoteStatusResponse.model_validate(response).model_dump()["market_date"] == expected_day
