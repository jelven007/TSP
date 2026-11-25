# ruff: noqa: RUF001 -- Official Chinese PDF excerpts are regression inputs.
from datetime import date, datetime

import pytest

from app.data_providers.suspension_announcements import AnnouncementLookup, parse_announcement

URL = "https://static.cninfo.com.cn/finalpage/2026-11-21/1225682610.PDF"
CUTOFF = datetime(2026, 11, 22, 12)


@pytest.mark.parametrize("symbol,text,expected", [
    ("002122.SZ", "证券代码：002122 公司股票交易将于2026年11月20日（星期五）"
     "开市起停牌一天，并于2026年11月23日（星期一）开市起复牌。",
     [(date(2026, 11, 20), True), (date(2026, 11, 23), False)]),
    ("002872.SZ", "证券代码：002872 公司股票将于2026年11月23日（星期一）"
     "停牌1天，2026年11月24日（星期二）开市起复牌并撤销其他风险警示。",
     [(date(2026, 11, 23), True), (date(2026, 11, 24), False)]),
    ("603580.SH", "证券代码：603580 证券代码 证券简称 停复牌类型 停牌起始日"
     "停牌期间 停牌终止日 复牌日 603580 艾艾精工 A股停牌 2026/11/23 全天"
     "2026/11/23 2026/11/24 公司股票于2026年11月23日（星期一）停牌一个交易日，"
     "并将在要约收购结果公告当日复牌。",
     [(date(2026, 11, 23), True), (date(2026, 11, 24), False)]),
])
def test_official_stock_trade_duration_and_table_wording(symbol, text, expected):
    events = parse_announcement(symbol, text, announced_at=datetime(2026, 11, 20, 19, 53),
                                cutoff=CUTOFF, source_url=URL, collect_future=True)
    assert [(row["effective_date"], row["is_suspended"]) for row in events] == expected


def test_only_convertible_conversion_halt_is_verified_without_a_stock_event(monkeypatch):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    monkeypatch.setattr(lookup, "_pdf", lambda _:
        "证券代码：688148 转债代码：118020 证券代码 证券简称 停复牌类型 "
        "停牌起始日 停牌期间 停牌终止日 复牌日 "
        "118020 芳源转债 可转债转股停牌 2026/11/19 全天 2026/11/19 2026/11/20")
    result = lookup.verify_notice(
        {"symbol": "688148.SH", "title": "关于向下修正“芳源转债”转股价格暨转股停牌的公告",
         "formal_date": "2026-11-19", "announced_at": "2026-11-18T17:04:23", "url": URL},
        cutoff=CUTOFF)
    lookup.close()
    assert result["events"] == []
    assert result["diagnostics"] == []


@pytest.mark.parametrize("text", [
    "公司股票预计于2026年11月20日停牌一天，并于2026年11月23日开市起复牌。",
    "子公司股票于2026年11月20日停牌一天，并于2026年11月23日开市起复牌。",
    "公司股票于2026年11月20日停牌一小时，并于2026年11月23日开市起复牌。",
    "公司股票于2026年11月20日10:00停牌一天。",
    "证券代码 证券简称 停复牌类型 停牌起始日 停牌期间 停牌终止日 复牌日 "
    "118020 芳源转债 可转债转股停牌 2026/11/19 全天 2026/11/19 2026/11/20",
])
def test_new_wording_keeps_plans_other_subjects_partial_halts_and_bonds_excluded(text):
    assert parse_announcement("688148.SH", "证券代码：688148 " + text,
                              announced_at=datetime(2026, 11, 20, 19), cutoff=CUTOFF,
                              source_url=URL, collect_future=True) == []


@pytest.mark.parametrize("symbol,text,expected", [
    ("002634.SZ", "证券代码：002634 浙江棒杰控股集团股份有限公司（以下简称“公司”）"
     "股票于2026年4月27日（星期一）停牌一天，并于2026年4月28日（星期二）开市起复牌。",
     [(date(2026, 4, 27), True), (date(2026, 4, 28), False)]),
    ("002129.SZ", "证券代码：002129 公司股票（证券简称：TCL中环，证券代码：002129）"
     "自2026年11月2日开市时开始停牌。",
     [(date(2026, 11, 2), True)]),
    ("600193.SH", "证券代码：600193 证券代码 证券简称 停复牌类型 停牌起始日 "
     "停牌期间 停牌终止日 复牌日 600193 *ST创兴 A股停牌 2026/4/28 "
     "公司于2026年4月27日提交披露了2025年年度报告。",
     [(date(2026, 4, 28), True)]),
    ("000552.SZ", "证券代码：000552 公司证券（A股股票：甘肃能化000552；"
     "可转债：能化转债127027）自2026年4月2日开市时起开始停牌。",
     [(date(2026, 4, 2), True)]),
])
def test_observed_historical_alias_open_and_typed_table(symbol, text, expected):
    events = parse_announcement(symbol, text, announced_at=datetime(2026, 4, 1, 19),
                                cutoff=CUTOFF, source_url=URL, collect_future=True)
    assert [(row["effective_date"], row["is_suspended"]) for row in events] == expected


def test_szse_attachment_uses_the_exchange_download_host(monkeypatch):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    path = "/disc/disk03/finalpage/2026-10-29/example.PDF"
    class Response:
        def json(self):
            return {"data": [{"title": "关于停牌的公告", "secCode": ["002598"],
                              "publishTime": "2026-10-29", "attachPath": path}],
                    "announceCount": 1}
    monkeypatch.setattr(lookup, "_request", lambda *a, **kw: Response())
    notices = lookup._szse("002598", date(2026, 10, 28), date(2026, 10, 30))
    lookup.close()
    assert notices[0]["url"] == "https://disc.static.szse.cn/download" + path


@pytest.mark.parametrize("symbol,text,expected", [
    ("600745.SH", "证券代码：600745 证券简称：闻泰科技 600745 闻泰科技 A股停牌 2026/4/30 全天"
     "2026/4/30 2026/5/6 110081 闻泰转债 可转债转股停牌 2026/4/30 全天"
     "2026/4/30 2026/5/6",
     [(date(2026, 4, 30), True), (date(2026, 5, 6), False)]),
    ("000078.SZ", "证券代码：000078 公司股票将于2026年4月30日停牌一日，"
     "复牌后将被实施其他风险警示。",
     [(date(2026, 4, 30), True)]),
    ("300996.SZ", "证券代码：300996 公司股票（股票简称：普联软件，股票代码：300996）"
     "及可转换公司债券（债券简称：普联转债，债券代码：123261）"
     "自2026年7月30日（星期四）开市起复牌。",
     [(date(2026, 7, 30), False)]),
    ("601059.SH", "证券代码：601059 公司A股股票将自2026年9月15日"
     "（即A股异议股东现金选择权申报首日）开市起连续停牌，不再交易。",
     [(date(2026, 9, 15), True)]),
])
def test_adjacent_table_rows_one_day_and_joint_stock_bond_events(symbol, text, expected):
    rows = parse_announcement(symbol, text, announced_at=datetime(2026, 4, 1, 19),
                              cutoff=CUTOFF, source_url=URL, collect_future=True)
    assert [(row["effective_date"], row["is_suspended"]) for row in rows] == expected


def test_resume_table_uses_resume_column_not_previous_halt_end_date():
    text = ("证券代码：603759 证券简称：海天股份 证券代码 证券简称 停复牌类型 "
            "停牌起始日 停牌期间 停牌终止日 复牌日 "
            "603759 海天股份 A股复牌 2026/10/19 2026/10/20 "
            "113700 海天转债 可转债债券复牌 2026/10/19 2026/10/20")
    rows = parse_announcement("603759.SH", text, announced_at=datetime(2026, 10, 19, 21, 25),
                              cutoff=CUTOFF, source_url=URL, collect_future=True)
    assert [(r["effective_date"], r["is_suspended"]) for r in rows] == [(date(2026, 10, 20), False)]


def test_original_bond_typo_does_not_hide_explicit_stock_halt():
    text = ("证券代码：300992 公司股票（证券简称：泰福泵业，证券代码：300992）"
            "和可转换公司转债（债券简称：泰福转债，债券代码：123160）"
            "自2026年3月24日（星期二）开市起停牌。")
    rows = parse_announcement("300992.SZ", text, announced_at=datetime(2026, 3, 23, 19),
                              cutoff=CUTOFF, source_url=URL, collect_future=True)
    assert [(r["effective_date"], r["is_suspended"]) for r in rows] == [(date(2026, 3, 24), True)]


@pytest.mark.parametrize("text", [
    "本公司股票于2026年5月19日进入退市整理期交易。",
    "公司股票进入退市整理期的起始日为2026年5月19日，退市整理期为15个交易日。",
    "公司股票进入退市整理期交易的起始日为2026年5月19日。",
])
def test_explicit_delisting_period_trading_is_a_resume(text):
    rows = parse_announcement("688287.SH", "证券代码：688287 " + text,
                              announced_at=datetime(2026, 5, 18, 18, 40), cutoff=CUTOFF,
                              source_url=URL, collect_future=True)
    assert [(r["effective_date"], r["is_suspended"]) for r in rows] == [(date(2026, 5, 19), False)]


@pytest.mark.parametrize("text", [
    "公司股票预计于2026年5月19日进入退市整理期交易。",
    "公司股票进入退市整理期的起始日预计为2026年5月19日。",
    "子公司股票于2026年5月19日进入退市整理期交易。",
])
def test_delisting_resume_requires_a_confirmed_own_stock_date(text):
    assert parse_announcement("688287.SH", "证券代码：688287 " + text,
                              announced_at=datetime(2026, 5, 18, 18, 40), cutoff=CUTOFF,
                              source_url=URL, collect_future=True) == []


def test_confirmed_own_a_and_b_security_halt():
    text = ("证券代码：000016、200016 公司证券（证券简称：*ST康佳A、*ST康佳B，"
            "证券代码：000016、200016）自2026年8月24日（星期一）开市起停牌。")
    rows = parse_announcement("000016.SZ", text, announced_at=datetime(2026, 8, 23, 18),
                              cutoff=CUTOFF, source_url=URL, collect_future=True)
    assert [(r["effective_date"], r["is_suspended"]) for r in rows] == [(date(2026, 8, 24), True)]


def test_security_parenthesis_must_identify_the_own_stock_not_a_bond():
    text = ("证券代码：000016 公司证券（证券简称：康佳转债，证券代码：118020）"
            "自2026年8月24日开市起停牌。")
    assert parse_announcement("000016.SZ", text, announced_at=datetime(2026, 8, 23, 18),
                              cutoff=CUTOFF, source_url=URL, collect_future=True) == []


def test_official_discovery_includes_delisting_trading_notices(monkeypatch):
    lookup = AnnouncementLookup(check_cancelled=lambda: None)
    class Response:
        def json(self):
            return {"result": [{"SECURITY_CODE": "688287",
                               "TITLE": "关于公司股票进入退市整理期交易的第一次风险提示公告",
                               "SSEDATE": "2026-05-19", "ADDDATE": "2026-05-18 18:40:54",
                               "URL": "/disclosure/example.pdf"}]}
    monkeypatch.setattr(lookup, "_request", lambda *a, **kw: Response())
    notices = lookup._sse("688287", date(2026, 5, 18), date(2026, 5, 19))
    lookup.close()
    assert len(notices) == 1
