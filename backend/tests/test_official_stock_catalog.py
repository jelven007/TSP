from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest

from app.data_providers.official_stock_catalog import (
    OfficialCatalogError,
    fetch_official_stock_catalog,
    normalize_szse_catalog,
)


def test_official_catalog_is_complete_and_uses_exchange_filters(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if "sse.com.cn" in request.url.host:
            code = "600000" if request.url.params["STOCK_TYPE"] == "1" else "688001"
            return httpx.Response(200, json={
                "result": [{"A_STOCK_CODE": code, "SEC_NAME_CN": "*ST测试",
                            "LIST_DATE": "20200102", "DELIST_DATE": "-"}],
                "pageHelp": {"total": 1},
            })
        return httpx.Response(200, json=[{"metadata": {"recordcount": 2}}])
    monkeypatch.setattr(
        "app.data_providers.official_stock_catalog._download_szse",
        lambda client: pl.DataFrame({
            "A股代码": ["000001", "300001"], "A股简称": ["平安银行", "特锐德"],
            "A股上市日期": ["1991-04-03", "2009-10-30"],
            "A股总股本": ["1,000", "2,000"], "A股流通股本": ["800", "1,500"],
        }),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_official_stock_catalog(client=client)
    assert {r["board"] for r in rows} == {"沪主板", "深主板", "创业板", "科创板"}
    assert rows[0]["symbol"] == "000001.SZ"
    assert rows[0]["total_shares"] == 1000.0  # XLSX 股本已经是股, 不能乘一亿
    assert next(r for r in rows if r["symbol"] == "600000.SH")["listing_date"] == "2020-01-02"
    for request in requests[:2]:
        assert request.url.params["COMPANY_STATUS"] == "2,4,5,7,8"
        assert request.headers["referer"].startswith("https://www.sse.com.cn/")


def test_sse_truncated_response_is_rejected_before_szse_download():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"result": [{"A_STOCK_CODE": "600000"}], "pageHelp": {"total": 100}},
    ))) as client, pytest.raises(OfficialCatalogError, match="沪"):
        fetch_official_stock_catalog(client=client)


def test_szse_missing_codes_and_invalid_dates_are_rejected():
    with pytest.raises(OfficialCatalogError):
        normalize_szse_catalog(pl.DataFrame({
            "A股代码": ["1"], "A股简称": ["样本"], "A股上市日期": ["not-a-date"],
        }))


@pytest.mark.parametrize("failure", ["empty", "missing_field", "wrong_count", "missing_board", "duplicate", "http"])
def test_incomplete_official_catalog_is_never_returned(monkeypatch, failure):
    codes = ["000001", "302132"]
    if failure == "missing_board":
        codes = ["000001", "000002"]
    if failure == "duplicate":
        codes = ["000001", "000001", "302132"]
    frame = pl.DataFrame({
        "A股代码": codes, "A股简称": ["样本"] * len(codes),
        "A股上市日期": ["2020-01-02"] * len(codes),
    })
    if failure == "empty":
        frame = frame.clear()
    if failure == "missing_field":
        frame = frame.drop("A股代码")
    monkeypatch.setattr("app.data_providers.official_stock_catalog._download_szse", lambda _: frame)

    def handler(request):
        if failure == "http":
            return httpx.Response(503)
        if "sse.com.cn" in request.url.host:
            code = "600000" if request.url.params["STOCK_TYPE"] == "1" else "688001"
            return httpx.Response(200, json={
                "result": [{"A_STOCK_CODE": code, "SEC_NAME_CN": "测试",
                            "LIST_DATE": "20200102", "DELIST_DATE": "-"}],
                "pageHelp": {"total": 1},
            })
        return httpx.Response(200, json=[{"metadata": {
            "recordcount": frame.height + (1 if failure == "wrong_count" else 0),
        }}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(OfficialCatalogError):
        fetch_official_stock_catalog(client=client)


def test_szse_302_chinext_and_delisting_exclusion():
    rows = normalize_szse_catalog(pl.DataFrame({
        "A股代码": ["302132", "300001"], "A股简称": ["中航成飞", "测试退"],
        "A股上市日期": ["2020-01-02", "2020-01-02"],
    }))
    assert [(row["symbol"], row["board"]) for row in rows] == [("302132.SZ", "创业板")]


def test_catalog_schedule_is_daily_beijing_nine_even_in_manual_mode(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.jobs import daily_pipeline
    from app.services import preferences

    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    preferences.save({"data_sync_auto_enabled": False, "instruments_schedule": {"hour": 8, "minute": 10}})
    monkeypatch.setattr(daily_pipeline, "_register_auction_jobs", lambda *args: None)
    monkeypatch.setattr(daily_pipeline, "_scheduled_instruments_task", lambda *args, **kw: None)
    calls = []
    async def startup(repo, *, source):
        calls.append(source)
    monkeypatch.setattr(daily_pipeline, "run_catalog_job", startup)
    async def exercise():
        scheduler = daily_pipeline.start_scheduler(SimpleNamespace(), SimpleNamespace())
        try:
            await asyncio.sleep(0)
            job = scheduler.get_job("pre_market_instruments")
            assert job.next_run_time is not None
            saturday = datetime(2026, 10, 24, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
            assert job.trigger.get_next_fire_time(None, saturday) == saturday.replace(hour=9)
            assert preferences.get_instruments_schedule() == {"hour": 9, "minute": 0}
        finally:
            scheduler.shutdown(wait=False)
            await daily_pipeline.stop_catalog_jobs()
    asyncio.run(exercise())
    assert calls == ["startup"]
    preferences._invalidate_cache()
