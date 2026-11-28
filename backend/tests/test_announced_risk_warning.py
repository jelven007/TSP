"""Official risk warning events share the proven public-time evidence boundary."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.data_providers.suspension_announcements import AnnouncementLookup

SYMBOL = "600001.SH"
URL = "https://www.sse.com.cn/disclosure/status.pdf"


@pytest.mark.parametrize("title,clause,expected", [
    ("关于实施其他风险警示的公告", "实施其他风险警示", True),
    ("关于撤销其他风险警示的公告", "撤销其他风险警示", False),
    ("关于撤销退市风险警示并继续实施其他风险警示的公告",
     "撤销退市风险警示并继续实施其他风险警示", True),
])
def test_official_risk_events_have_effective_and_actual_public_dates(monkeypatch, title, clause, expected):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    text = f"股票代码\uff1a600001。公司股票自2026年11月24日起{clause}。"
    monkeypatch.setattr(lookup, "_pdf", lambda url: text)
    try:
        notice = {"symbol": SYMBOL, "title": title, "formal_date": "2026-11-24",
                  "announced_at": "2026-11-23T20:00:00", "url": URL}
        result = lookup.verify_notice(notice, cutoff=datetime(2026, 11, 23, 20, 30))
        assert result["diagnostics"] == []
        events = result.get("risk_warning_events", [])
        assert len(events) == 1
        assert events[0]["effective_date"] == date(2026, 11, 24)
        assert events[0]["announced_at"] == datetime(2026, 11, 23, 20)
        assert events[0]["is_risk_warning"] is expected
        assert events[0]["source_url"] == URL
    finally:
        lookup.close()


def test_qinghai_spring_halt_resume_and_warning_removal_are_distinct(monkeypatch, tmp_path):
    from app.services.announced_stock_status import confirmed_status, merge_risk_events

    symbol = "600381.SH"
    notice = {
        "symbol": symbol, "title": "青海春天撤销退市风险警示及其他风险警示的公告",
        "formal_date": "2026-11-27", "announced_at": "2026-11-26T18:49:57", "url": URL,
    }
    # The issuer uses a typed A-share table and a separately labelled removal date.
    text = (
        "证券代码\uff1a600381 证券简称\uff1a*ST春天。"
        "600381 *ST春天 A股停牌 2026/11/27 全天 2026/11/27 2026/11/30。"
        "(四)撤销退市风险警示及其他风险警示的起始日\uff1a2026年11月30日。"
        "公司股票于2025年4月30日起被实施退市风险警示。"
        "公司股票将于2026年11月27日停牌一天,自2026年11月30日起复牌交易,"
        "公司股票撤销退市风险警示及其他风险警示。"
    )
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    monkeypatch.setattr(lookup, "_pdf", lambda url: text)
    try:
        result = lookup.verify_notice(notice, cutoff=datetime(2026, 11, 28, 6, 30))
    finally:
        lookup.close()
    assert result["diagnostics"] == []
    assert [(e["effective_date"], e["is_suspended"]) for e in result["events"]] == [
        (date(2026, 11, 27), True), (date(2026, 11, 30), False),
    ]
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    assert merge_risk_events(repo, result["risk_warning_events"]) == 1
    assert merge_risk_events(repo, result["risk_warning_events"]) == 0
    assert confirmed_status(repo, date(2026, 11, 30), observed_at=datetime(2026, 11, 26, 18, 49, 56))[
        "risk_removed"] == []
    assert confirmed_status(repo, date(2026, 11, 27), observed_at=datetime(2026, 11, 26, 18, 49, 57))[
        "risk_removed"] == []
    assert confirmed_status(repo, date(2026, 11, 30), observed_at=datetime(2026, 11, 26, 18, 49, 57))[
        "risk_removed"] == [symbol]


def test_conflicting_explicit_warning_removal_dates_fail_closed():
    from app.services.issuer_st_status import parse_issuer_st_events

    text = (
        "证券代码:600381。撤销退市风险警示及其他风险警示的起始日:2026年11月30日。"
        "公司股票自2026年12月1日起撤销退市风险警示及其他风险警示。"
    )
    with pytest.raises(ValueError, match="日期冲突"):
        parse_issuer_st_events("关于撤销退市风险警示及其他风险警示的公告",
                               text, "600381.SH", date(2026, 11, 26))


def test_risk_status_merge_is_idempotent_and_conflicts_preserve_original(tmp_path):
    from app.services import announced_stock_status as status
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    event = {"symbol": SYMBOL, "effective_date": date(2026, 11, 24),
             "announced_on": date(2026, 11, 23), "announced_at": datetime(2026, 11, 23, 20),
             "source_url": URL, "is_risk_warning": True, "evidence_text": "已核验的ST公告正文"}
    assert status.merge_risk_events(repo, [event]) == 1
    assert status.merge_risk_events(repo, [event]) == 0
    with pytest.raises(ValueError, match="冲突"):
        status.merge_risk_events(repo, [{**event, "is_risk_warning": False}])
    history = status.load_risk_events(tmp_path)
    assert history.height == 1
    assert history["is_risk_warning"].to_list() == [True]
    current = status.confirmed_status(repo, date(2026, 11, 24),
                                      observed_at=datetime(2026, 11, 23, 20, 30))
    assert current["risk_warning"] == [SYMBOL]
    before = status.confirmed_status(repo, date(2026, 11, 24),
                                     observed_at=datetime(2026, 11, 23, 19, 59))
    assert before["risk_warning"] == []


def test_late_publication_is_deferred_without_consuming_future_event(monkeypatch):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    monkeypatch.setattr(lookup, "_pdf", lambda *a: pytest.fail("late document cannot be consumed"))
    try:
        result = lookup.verify_notice({
            "symbol": SYMBOL, "title": "关于实施其他风险警示的公告",
            "formal_date": "2026-11-24", "announced_at": "2026-11-23T20:31:00", "url": URL,
        }, cutoff=datetime(2026, 11, 23, 20, 30))
        assert result["risk_warning_events"] == [] and result["deferred"]
    finally:
        lookup.close()


def test_application_for_removal_is_not_confirmed_event():
    from app.data_providers.suspension_announcements import _status_event_title
    assert not _status_event_title("关于申请撤销其他风险警示的公告")
    assert _status_event_title("关于撤销其他风险警示的公告")
    assert _status_event_title("关于停牌的公告")
