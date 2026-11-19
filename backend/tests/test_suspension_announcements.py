# ruff: noqa: RUF001 -- Real Chinese announcement punctuation is parser input.
from datetime import date, datetime

import httpx
import pytest

from app.data_providers.suspension_announcements import AnnouncementLookup, parse_announcement

DAY = date(2026, 11, 12)
CUTOFF = datetime(2026, 11, 12, 20)
URL = "https://www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-11-12/a.pdf"
HEADER = "证券代码：600185 证券简称：格力地产\n"


@pytest.mark.parametrize("sentence, suspended", [
    ("公司股票将于2026年11月12日（星期四）开市起停牌。", True),
    ("公司股票自2026年11月12日开市起复牌。", False),
    ("公司股票于2026年11月12日起继续停牌。", True),
])
def test_official_body_requires_own_stock_explicit_opening_and_effective_date(sentence, suspended):
    events = parse_announcement("600185.SH", HEADER + sentence,
                                announced_at=datetime(2026, 11, 11, 18, 41, 26),
                                cutoff=CUTOFF, source_url=URL)
    assert len(events) == 1
    assert events[0]["is_suspended"] is suspended
    assert events[0]["effective_date"] == DAY
    assert events[0]["source_url"] == URL


@pytest.mark.parametrize("symbol,sentence,effective", [
    ("002623.SZ", "证券代码：002623 公司股票（证券简称：亚玛顿，证券代码：002623）"
     "自2026年11月10日（星期二）上午开市起复牌。", date(2026, 11, 10)),
    ("601326.SH", "证券代码：601326 公司A股股票将于2026年11月4日（星期三）开市起复牌。",
     date(2026, 11, 4)),
])
def test_published_resume_wording_with_morning_opening_or_a_share_subject(symbol, sentence, effective):
    events = parse_announcement(symbol, sentence, announced_at=datetime(2026, 11, 3, 18),
                                cutoff=CUTOFF, source_url=URL)
    assert len(events) == 1 and events[0]["effective_date"] == effective
    assert events[0]["is_suspended"] is False


@pytest.mark.parametrize("sentence", [
    "公司股票预计于2026年11月12日开市起复牌。",
    "公司股票拟于2026年11月12日开市起停牌。",
    "公司股票自2026年11月13日开市起停牌。",
    "公司股票于2026年11月12日10:00临时停牌。",
    "公司股票于2026年11月12日停牌一小时。",
    "子公司股票自2026年11月12日开市起停牌。",
    "公司股票连续停牌。",
    "公司B股股票自2026年11月12日开市起停牌。",
    "子公司A股股票自2026年11月12日上午开市起停牌。",
    "公司A股股票自2026年11月12日下午开市起停牌。",
])
def test_plans_future_partial_day_and_ambiguous_announcements_cannot_close_daily_gap(sentence):
    assert parse_announcement("600185.SH", HEADER + sentence,
                              announced_at=datetime(2026, 11, 11, 18),
                              cutoff=CUTOFF, source_url=URL) == []


@pytest.mark.parametrize("stamp", [None, datetime(2026, 11, 12, 22, 44)])
def test_unknown_or_late_publication_time_does_not_use_formal_midnight_date(stamp):
    assert parse_announcement("600185.SH", HEADER + "公司股票自2026年11月12日开市起停牌。",
                              announced_at=stamp, cutoff=CUTOFF, source_url=URL) == []


def test_other_stock_and_conflicting_body_are_not_accepted():
    text = HEADER + "公司股票自2026年11月12日开市起停牌。"
    assert parse_announcement("600186.SH", text, announced_at=datetime(2026, 11, 11, 18),
                              cutoff=CUTOFF, source_url=URL) == []
    assert parse_announcement("600185.SH", text + "公司股票自2026年11月12日开市起复牌。",
                              announced_at=datetime(2026, 11, 11, 18),
                              cutoff=CUTOFF, source_url=URL) == []


@pytest.mark.parametrize("stamp, expected", [
    ("2026-11-12 19:44:09:341", 1),
    ("2026-11-12 22:44:09:341", 0),
])
def test_szse_formal_date_uses_only_exact_matching_public_index_clock(monkeypatch, stamp, expected):
    calls = []
    title = "关于公司股票停牌的公告"
    def response(request):
        calls.append(request)
        if request.url.host == "www.szse.cn":
            assert request.headers["Referer"] == "https://www.szse.cn/"
            return httpx.Response(200, json={"announceCount": 1, "data": [
                {"secCode": ["000635"], "title": "英 力 特：" + title, "publishTime": "2026-11-13 00:00:00",
                 "attachPath": "/disc/disk03/finalpage/2026-11-13/a.PDF"}]})
        assert request.url.host == "np-anotice-stock.eastmoney.com"
        return httpx.Response(200, json={"data": {"list": [
            {"codes": [{"stock_code": "000635", "short_name": "英力特"}],
             "title": "英力特:" + title, "notice_date": "2026-11-13 00:00:00",
             "display_time": stamp}]}})
    client = httpx.Client(transport=httpx.MockTransport(response))
    lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
    monkeypatch.setattr(lookup, "_pdf", lambda url:
                        "股票代码：000635 公司股票自2026年11月12日开市起停牌。")
    result = lookup("000635.SZ", DAY, cutoff=CUTOFF)
    assert len(result["events"]) == expected
    assert len(calls) == 2
    client.close()


def test_discovery_collects_tomorrow_halt_without_relaxing_historical_repair():
    text = "证券代码：600567 公司股票将于2026年11月19日开市起停牌。"
    kwargs = {"announced_at": datetime(2026, 11, 18, 22, 12),
              "cutoff": datetime(2026, 11, 18, 23), "source_url": URL}
    assert parse_announcement("600567.SH", text, **kwargs) == []
    events = parse_announcement("600567.SH", text, collect_future=True, **kwargs)
    assert len(events) == 1 and events[0]["effective_date"] == date(2026, 11, 19)
    assert events[0]["announced_at"] == kwargs["announced_at"]


@pytest.mark.parametrize("exchange", ["SH", "SZ"])
def test_complete_market_pagination_finds_new_symbol_beyond_first_page(exchange):
    calls = []
    page_size = 100 if exchange == "SH" else 50
    def response(request):
        if exchange == "SH":
            query = request.url.params
            page = int(query["pageHelp.pageNo"])
            assert query["pageHelp.cacheSize"] == "1"
            assert query["pageHelp.beginPage"] == query["pageHelp.endPage"] == str(page)
        else:
            import json
            query = json.loads(request.content)
            page = query["pageNum"]
            assert query["stock"] == [] and query["pageSize"] == 50
        calls.append(page)
        rows = []
        for index in range((page - 1) * page_size, min(page * page_size, 101)):
            title = "股票停牌公告" if index == 100 else "其他公告"
            if exchange == "SH":
                rows.append({"SECURITY_CODE": "600567", "TITLE": title, "SSEDATE": "2026-11-19",
                             "ADDDATE": "2026-11-18 22:12:00", "URL": f"/disclosure/{index}.pdf"})
            else:
                rows.append({"secCode": ["000635"], "title": title, "publishTime": "2026-11-19",
                             "attachPath": f"/disc/{index}.PDF"})
        body = {"result": rows, "pageHelp": {"total": 101}} if exchange == "SH" else {
            "data": rows, "announceCount": 101}
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
        result = lookup.market_notices(exchange, DAY, date(2026, 11, 20))
        assert result["rows"] == 101 and result["pages"] == (2 if exchange == "SH" else 3)
        assert len(result["notices"]) == 1
        assert result["notices"][0]["symbol"] == ("600567.SH" if exchange == "SH" else "000635.SZ")
        assert calls == ([1, 2, 1] if exchange == "SH" else [1, 2, 3, 1])


@pytest.mark.parametrize("fault", ["duplicate_page", "missing_page", "changed_total", "leading_page_moved"])
def test_incomplete_or_mutating_market_feed_cannot_certify_coverage(monkeypatch, fault):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    calls = []
    def page(exchange, begin, end, number):
        calls.append(number)
        ids = range(100) if number == 1 else [100]
        if fault == "duplicate_page" and number == 2:
            ids = [0]
        if fault == "missing_page" and number == 2:
            ids = []
        if fault == "leading_page_moved" and len(calls) == 3:
            ids = range(1, 101)
        total = 102 if fault == "changed_total" and number == 2 else 101
        return {"pageHelp": {"total": total}, "result": [
            {"SECURITY_CODE": "600567", "TITLE": "停牌公告", "SSEDATE": "2026-11-19",
             "ADDDATE": "2026-11-18 22:12:00", "URL": f"/{index}.pdf"} for index in ids]}
    monkeypatch.setattr(lookup, "_market_page", page)
    with pytest.raises(ValueError, match=r"分页|重复|变化"):
        lookup.market_notices("SH", DAY, date(2026, 11, 20))
    lookup.close()


@pytest.mark.parametrize("finish_before_timeout", [False, True])
def test_market_scan_resumes_budget_checkpoint_and_revalidates_first_page(finish_before_timeout):
    checkpoints, calls = [], []
    def response(request):
        page = int(request.url.params["pageHelp.pageNo"])
        calls.append(page)
        rows = [{"SECURITY_CODE": "600567", "TITLE": "停牌公告", "SSEDATE": "2026-11-19",
                 "ADDDATE": "2026-11-18 22:12:00", "URL": f"/{index}.pdf"}
                for index in range((page - 1) * 100, min(page * 100, 101))]
        return httpx.Response(200, json={"result": rows, "pageHelp": {"total": 101}})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        first = AnnouncementLookup(client=client, check_cancelled=lambda: None,
                                   max_requests=2 if finish_before_timeout else 1)
        with pytest.raises(ValueError, match="预算"):
            first.market_notices("SH", DAY, DAY, on_progress=checkpoints.append)
        saved = checkpoints[-1]
        assert saved["page"] == (2 if finish_before_timeout else 1)
        second = AnnouncementLookup(client=client, check_cancelled=lambda: None)
        result = second.market_notices("SH", DAY, DAY, checkpoint=saved, on_progress=checkpoints.append)
        assert result["pages"] == 2 and result["rows"] == len(result["notices"]) == 101
        assert calls == ([1, 2, 1, 1] if finish_before_timeout else [1, 1, 2, 1])


def test_moved_feed_discards_checkpoint_instead_of_certifying_combined_pages():
    saved = []
    def response(request):
        page = int(request.url.params["pageHelp.pageNo"])
        rows = [{"SECURITY_CODE": "600567", "TITLE": "其他公告", "SSEDATE": "2026-11-19",
                 "URL": f"/{index}.pdf"} for index in range(100)]
        return httpx.Response(200, json={"result": rows, "pageHelp": {"total": 101 + page}})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None, max_requests=1)
        with pytest.raises(ValueError, match="预算"):
            lookup.market_notices("SH", DAY, DAY, on_progress=saved.append)
        checkpoint = {**saved[-1], "total": 101}
        lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
        with pytest.raises(ValueError, match="变化"):
            lookup.market_notices("SH", DAY, DAY, checkpoint=checkpoint, on_progress=saved.append)
        assert saved[-1] is None


@pytest.mark.parametrize("code,column,org_id", [
    ("688798", "sse", "gfbj0833221"), ("000635", "szse", "gssz0000635"),
])
def test_alternate_attachment_resolves_actual_security_identity(code, column, org_id):
    title = "关于公司股票停牌的公告"
    url = "https://static.cninfo.com.cn/finalpage/2026-11-13/notice.PDF"
    def response(request):
        if request.url.path.endswith("topSearch/query"):
            assert b"keyWord=" + code.encode() in request.content
            return httpx.Response(200, json=[{"code": code, "orgId": org_id}])
        assert b"column=" + column.encode() in request.content
        assert (code + "%2C" + org_id).encode() in request.content
        return httpx.Response(200, json={"announcements": [
            {"secCode": code, "announcementTitle": title,
             "announcementTime": 1794499200000, "adjunctUrl": url}]})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
        assert lookup._cninfo_alternative(code, {"title": title, "formal_date": "2026-11-13"},
                                          DAY, date(2026, 11, 14)) == url


def test_szse_forbidden_pdf_uses_matched_official_alternative(monkeypatch):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    monkeypatch.setattr(lookup, "_szse", lambda *args: [{
        "title": "停牌公告", "formal_date": "2026-11-12",
        "announced_at": datetime(2026, 11, 12, 19), "url": URL}])
    alternate = "https://static.cninfo.com.cn/finalpage/2026-11-12/a.PDF"
    monkeypatch.setattr(lookup, "_cninfo_alternative", lambda *args: alternate)
    def pdf(url):
        if url == URL:
            raise ValueError("403 Forbidden")
        return "证券代码：000635 公司股票自2026年11月12日开市起停牌。"
    monkeypatch.setattr(lookup, "_pdf", pdf)
    result = lookup("000635.SZ", DAY, cutoff=CUTOFF)
    assert len(result["events"]) == 1 and result["events"][0]["source_url"] == alternate
    lookup.close()


@pytest.mark.parametrize("kind,expected", [("A股股票", 1), ("可转换公司债券", 0)])
def test_security_subject_requires_explicit_stock_kind_and_opening_time(kind, expected):
    text = ("证券代码：000635 公司证券（证券品种：" + kind
            + "，证券简称：英力特，证券代码：000635）自2026年11月13日开市时起开始停牌。")
    result = parse_announcement("000635.SZ", text, announced_at=datetime(2026, 11, 12, 22, 44, 9),
                                cutoff=datetime(2026, 11, 13, 20), source_url=URL)
    assert len(result) == expected


def test_zero_network_budget_does_not_make_a_request():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected network")))
    lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None, max_requests=0)
    with pytest.raises(ValueError, match="预算"):
        lookup("600185.SH", DAY, cutoff=CUTOFF)
    client.close()


def test_official_pdf_error_cannot_be_parsed_as_a_valid_empty_notice():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"<html>unavailable</html>")))
    lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
    with pytest.raises(ValueError, match="PDF"):
        lookup._pdf(URL)
    client.close()


def test_decoded_compressed_response_is_not_decoded_twice():
    import gzip
    client = httpx.Client(transport=httpx.MockTransport(lambda request:
                          httpx.Response(200, content=gzip.compress(b'{"result": []}'),
                                         headers={"Content-Encoding": "gzip"})))
    lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
    assert lookup._request("GET", URL).json() == {"result": []}
    client.close()


def test_response_size_is_bounded_before_pdf_parsing():
    client = httpx.Client(transport=httpx.MockTransport(lambda request:
                          httpx.Response(200, content=b"x" * (4 * 1024**2 + 1))))
    lookup = AnnouncementLookup(client=client, check_cancelled=lambda: None)
    with pytest.raises(ValueError, match="4MB"):
        lookup._request("GET", URL)
    client.close()
