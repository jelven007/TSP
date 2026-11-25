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
