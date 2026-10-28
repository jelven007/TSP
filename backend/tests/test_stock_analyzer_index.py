"""AI 分析 prompt 指数 / ETF 文案测试。"""
from app.services.stock_analyzer import _build_user_prompt


def test_user_prompt_index_no_financials():
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 3000.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=3000.0, symbol="000001.SH", focus="",
        asset_type="index",
    )
    assert "指数" in prompt
    assert "Free 模式" not in prompt  # 指数无财务是常态, 不走 Free 文案


# ===== V3: 虚拟持仓注入 =====

def test_user_prompt_injects_paper_position():
    """模拟交易持有该标的: 注入持仓数量/成本/浮盈, 并要求 AI 给持仓建议。"""
    from app.services.stock_analyzer import _paper_position_part, _build_user_prompt

    part = _paper_position_part([{"account_id": "default", "qty": 1000, "avg_cost": 10.0}],
                                close=11.0, raw_close=11.0)
    assert "1000 股" in part
    assert "10.000" in part
    assert "+10.00%" in part
    assert "虚拟持仓" in part

    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 11.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=11.0, symbol="600000.SH", focus="",
        paper_positions=[{"account_id": "default", "qty": 1000, "avg_cost": 10.0}],
        raw_close=11.0,
    )
    assert "模拟交易" in prompt and "持仓 1000 股" in prompt


def test_paper_position_part_multi_account():
    """V2 多账户: 逐账户注入, 空仓账户跳过, 全空返回空串。"""
    from app.services.stock_analyzer import _paper_position_part

    part = _paper_position_part(
        [
            {"account_id": "a1", "qty": 100, "avg_cost": 10.0},
            {"account_id": "a2", "qty": 0, "avg_cost": 10.0},
            {"account_id": "a3", "qty": 200, "avg_cost": 20.0},
        ],
        close=11.0, raw_close=11.0,
    )
    assert "账户 a1" in part and "账户 a3" in part
    assert "a2" not in part


def test_paper_position_part_empty_without_position():
    """无持仓/零仓/无效成本: 返回空串, 不注入误导性「空仓」表述。"""
    from app.services.stock_analyzer import _paper_position_part

    assert _paper_position_part([], 11.0, 11.0) == ""
    assert _paper_position_part([{"qty": 0, "avg_cost": 10.0}], 11.0, 11.0) == ""
    assert _paper_position_part([{"qty": 100, "avg_cost": 0}], 11.0, 11.0) == ""


def test_paper_position_part_prefers_raw_close():
    """除权后前复权 close 与 raw 成本混算会产生虚假盈亏: 必须优先 raw_close。

    raw 成本 20 元, 除权后前复权 close=11、raw_close=21: 用 close 算出 -45% 是错的。
    """
    from app.services.stock_analyzer import _paper_position_part

    part = _paper_position_part([{"qty": 100, "avg_cost": 20.0}], close=11.0, raw_close=21.0)
    assert "+5.00%" in part
    assert "-45" not in part
