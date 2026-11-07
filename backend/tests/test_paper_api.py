"""虚拟账户 API 契约测试 — 成功/无数据/错误响应 (per CONTRIBUTING API 契约矩阵)。

镜像 test_alerts_query_bounds.py: 只挂本路由的裸 FastAPI app, 不经过主应用
全局中间件 (访问密码门是部署层关注点, 不属于本契约)。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "etf" if s.split(".")[0].startswith(("51", "56", "58", "15")) else "stock",
    )
    return TestClient(app)


def test_overview_before_init(client: TestClient):
    r = client.get("/api/paper/overview")
    assert r.status_code == 200
    assert r.json() == {"initialized": False, "account_id": "default"}


def test_account_edit_delete_refresh_related_api_views(client):
    client.post("/api/paper/account", json={"initial_cash": 200000, "name": "原名"})
    edit = client.put("/api/paper/account", json={"name": "新名", "initial_cash": 500000})
    assert edit.status_code == 200 and edit.json()["account"]["cash"] == 500000
    assert client.get("/api/paper/compare").json()["accounts"][0]["name"] == "新名"
    assert client.get("/api/paper/overview").json()["initial_cash_editable"] is True
    client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10})
    assert client.get("/api/paper/overview").json()["initial_cash_editable"] is False
    assert client.put("/api/paper/account", json={"name": "拒绝", "initial_cash": 1000000}).status_code == 400
    assert client.put("/api/paper/account", json={"id": "evil"}).status_code == 422
    assert client.delete("/api/paper/account").status_code == 200
    assert client.get("/api/paper/accounts").json()["accounts"] == []
    assert client.get("/api/paper/compare").json()["accounts"] == []
    assert client.get("/api/paper/orders").json()["orders"] == []
    assert client.get("/api/paper/overview").json()["initialized"] is False
    assert client.delete("/api/paper/account").status_code == 404
    assert client.put("/api/paper/account", json={"name": "missing"}).status_code == 404
    assert client.delete("/api/paper/account?account=../escape").status_code == 400


def test_account_edit_saves_extended_fees_atomically(client):
    client.post("/api/paper/account", json={"initial_cash": 200000, "name": "原名"})
    changes = {"name": "新名", "min_commission": 3.0, "transfer_fee_pct": .00001,
               "stamp_tax_mode": "historical"}
    response = client.put("/api/paper/account", json=changes)
    assert response.status_code == 200, response.text
    overview = client.get("/api/paper/overview").json()
    assert overview["account_name"] == "新名" and overview["initial_cash"] == 200000
    for key in ("min_commission", "transfer_fee_pct", "stamp_tax_mode"):
        assert overview["fees"][key] == changes[key]
    assert client.put("/api/paper/account", json={
        "name": "不应保存", "min_commission": -1,
    }).status_code == 422
    assert client.get("/api/paper/overview").json() == overview


def test_multi_account_isolation_and_settings(client: TestClient):
    """双账户隔离: 订单/概览互不可见; settings 端点切换涨跌停排队。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})  # default
    r = client.post("/api/paper/account", json={"initial_cash": 500000, "account_id": "acc_a", "name": "策略A"})
    assert r.status_code == 200 and r.json()["account"]["name"] == "策略A"

    # 账户列表
    ids = {a["id"] for a in client.get("/api/paper/accounts").json()["accounts"]}
    assert ids == {"default", "acc_a"}

    # 订单隔离: 同一笔下单只出现在对应账户
    assert client.post("/api/paper/orders?account=acc_a", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0}).status_code == 200
    assert len(client.get("/api/paper/orders?account=acc_a").json()["orders"]) == 1
    assert len(client.get("/api/paper/orders").json()["orders"]) == 0

    # 概览隔离: acc_a 有 pending 单不影响 default 的现金展示
    assert client.get("/api/paper/overview?account=acc_a").json()["initial_cash"] == 500000
    assert client.get("/api/paper/overview").json()["initial_cash"] == 1000000

    # settings: 开启涨跌停排队 → 账户字段更新
    r = client.post("/api/paper/settings?account=acc_a", json={"queue_limit_orders": True})
    assert r.status_code == 200 and r.json()["account"]["queue_limit_orders"] is True
    assert client.get("/api/paper/overview?account=acc_a").json()["queue_limit_orders"] is True
    assert client.get("/api/paper/overview").json()["queue_limit_orders"] is False

    # 非法账户 id → 400 (路径穿越防护)
    assert client.get("/api/paper/overview?account=../x").status_code == 400


def test_account_create_idempotent(client: TestClient):
    r1 = client.post("/api/paper/account", json={"initial_cash": 500000})
    assert r1.status_code == 200
    r2 = client.post("/api/paper/account", json={"initial_cash": 900000})
    assert r2.status_code == 200
    assert r2.json()["account"]["cash"] == 500000  # 幂等: 不覆盖


def test_account_extended_fees_and_rule_execution_fields_round_trip(client):
    fees = {"min_commission": 3, "transfer_fee_pct": .00001, "stamp_tax_mode": "historical"}
    response = client.post("/api/paper/account", json={"initial_cash": 100000, **fees})
    assert response.status_code == 200
    assert {key: response.json()["account"].get(key) for key in fees} == fees
    overview = client.get("/api/paper/overview").json()
    assert {key: overview["fees"].get(key) for key in fees} == fees
    assert overview["stamp_tax_mode"] == "historical"
    rule = client.post("/api/paper/auto_rules", json={
        "name": "pool", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "size_value": 3000, "candidate_priority": "pool",
        "buy_order_type": "close", "sell_order_type": "market",
    })
    assert rule.status_code == 200
    saved = client.get("/api/paper/auto_rules").json()["rules"][0]
    assert saved["candidate_priority"] == "pool"
    assert saved.get("buy_order_type") == "close" and saved.get("sell_order_type") == "market"
    update = client.put(f"/api/paper/auto_rules/{saved['id']}", json={
        "name": "pool edited", "match_kind": "strategy", "match_id": "s1",
        "side": "auto", "size_value": 4000, "buy_order_type": "market",
        "sell_order_type": "close", "candidate_priority": "pool",
    })
    assert update.status_code == 200 and update.json()["rule"]["sell_order_type"] == "close"


def test_account_extended_fee_settings_compatibility_and_isolation(client):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    client.post("/api/paper/account", json={"initial_cash": 100000, "account_id": "second"})
    response = client.post("/api/paper/settings", json={
        "min_commission": 2, "transfer_fee_pct": .0001, "stamp_tax_mode": "historical",
    })
    assert response.status_code == 200 and response.json()["account"].get("min_commission") == 2
    other = client.get("/api/paper/account?account=second").json()["account"]
    assert other.get("min_commission") == 5 and other.get("transfer_fee_pct") == 0
    assert other["stamp_tax_mode"] == "fixed"
    before = client.get("/api/paper/account").json()
    bad = client.post("/api/paper/settings", json={"min_commission": -1, "commission_pct": .0005})
    assert bad.status_code in (400, 422)
    assert client.get("/api/paper/account").json() == before


def test_corporate_action_api_returns_mode_and_rejects_invalid_election(client):
    client.post("/api/paper/account", json={"initial_cash": 100_000})
    assert client.get("/api/paper/corporate-actions").json() == {
        "mode": "events", "events": [], "blocked": [],
    }
    assert client.post("/api/paper/corporate-actions/subscribe", json={
        "action_id": "missing", "qty": 100,
    }).status_code == 400
    assert client.post("/api/paper/corporate-actions/subscribe", json={
        "action_id": "missing", "qty": 1.5,
    }).status_code == 422
    assert client.get("/api/paper/corporate-actions?account=../x").status_code == 400


def test_account_rejects_non_positive_cash(client: TestClient):
    assert client.post("/api/paper/account", json={"initial_cash": 0}).status_code == 400
    assert client.post("/api/paper/account", json={"initial_cash": -1}).status_code == 400


def test_order_create_list_cancel_flow(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0})
    assert r.status_code == 200
    order = r.json()["order"]
    assert order["status"] == "pending"

    # 非百股整数倍 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 150}).status_code == 400
    # 超资金 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100000, "ref_price": 1500.0}).status_code == 400
    # 卖出无持仓 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "sell", "qty": 100}).status_code == 400

    # 列表 + 撤单
    assert len(client.get("/api/paper/orders?status=pending").json()["orders"]) == 1
    r = client.delete(f"/api/paper/orders/{order['id']}")
    assert r.status_code == 200 and r.json()["order"]["status"] == "cancelled"
    # 已撤不可再撤
    assert client.delete(f"/api/paper/orders/{order['id']}").status_code == 400


def test_order_amount_mode_auto_converts(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "amount": 200000, "ref_price": 1500.0})
    assert r.status_code == 200
    assert r.json()["order"]["qty"] == 100  # 20万 / 1500 = 133 股 → 向下取整百 = 100
    # 无参考价的金额单 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "amount": 100}).status_code == 400


def test_freeze_blocks_new_orders(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    assert client.post("/api/paper/freeze?frozen=true").status_code == 200
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10.0})
    assert r.status_code == 400 and "冻结" in r.json()["detail"]
    # 解冻恢复
    assert client.post("/api/paper/freeze?frozen=false").status_code == 200
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10.0}).status_code == 200


def test_freeze_before_init_rejected(client: TestClient):
    assert client.post("/api/paper/freeze?frozen=true").status_code == 400


def test_trades_nav_stats_empty(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    assert client.get("/api/paper/trades").json() == {
        "fills": [], "total": 0, "execution_total": 0, "next_cursor": None,
    }
    assert client.get("/api/paper/nav").json() == {
        "nav": [], "total": 0, "next_cursor": None,
    }
    stats = client.get("/api/paper/stats").json()
    assert stats["rounds"] == 0 and stats["win_rate"] == 0.0


def test_settings_updates_fees(client: TestClient):
    """费用三参数可经 settings 端点调整; 超范围 → 400; 未知字段忽略。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/settings", json={
        "commission_pct": 0.0003, "stamp_tax_pct": 0.0005, "slippage_bps": 8,
    })
    assert r.status_code == 200
    acc = r.json()["account"]
    assert acc["commission_pct"] == 0.0003
    assert acc["stamp_tax_pct"] == 0.0005
    assert acc["slippage_bps"] == 8
    # 超范围
    r = client.post("/api/paper/settings", json={"commission_pct": 0.5})
    assert r.status_code == 400 and "超出合理范围" in r.json()["detail"]
    # 部分更新: 只改滑点, 佣金不动
    r = client.post("/api/paper/settings", json={"slippage_bps": 3})
    acc = r.json()["account"]
    assert acc["slippage_bps"] == 3 and acc["commission_pct"] == 0.0003


def test_compare_accounts(client: TestClient):
    """/compare: 逐账户概览+统计+净值; 未初始化的空壳账户不出现。"""
    # 只有懒创建的空壳目录时 → 空列表
    assert client.get("/api/paper/compare").json()["accounts"] == []

    client.post("/api/paper/account", json={"initial_cash": 1000000, "name": "甲"})
    client.post("/api/paper/account", json={"initial_cash": 500000, "account_id": "acc_b", "name": "乙"})
    rows = client.get("/api/paper/compare").json()["accounts"]
    assert [r["account"] for r in rows] == ["default", "acc_b"]
    row = rows[0]
    assert row["name"] == "甲" and row["initial_cash"] == 1000000
    assert row["total"] == pytest.approx(1000000)
    assert row["pnl_pct"] == 0.0
    assert row["fees"]["commission_pct"] == pytest.approx(0.00025)
    assert row["rounds"] == 0 and row["win_rate"] == 0.0
    assert row["nav"] == []  # 尚无定版净值
    # 字段完整性 (对比表依赖)
    for key in ("cash", "market_value", "total_pnl", "profit_loss_ratio", "max_drawdown", "avg_holding_days", "realized_pnl"):
        assert key in row


def test_auto_rule_portfolio_fields_roundtrip_and_validation(client):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    body = {"name": "风控跟单", "match_kind": "strategy", "match_id": "demo",
            "side": "auto", "size_value": 10000, "candidate_priority": "score",
            "max_symbol_exposure_pct": 25, "max_total_exposure_pct": 80, "risk_follow": True}
    response = client.post("/api/paper/auto_rules", json=body)
    assert response.status_code == 200, response.text
    stored = client.get("/api/paper/auto_rules").json()["rules"][0]
    for key in ("candidate_priority", "max_symbol_exposure_pct", "max_total_exposure_pct", "risk_follow"):
        assert stored[key] == body[key]
    assert client.post("/api/paper/auto_rules", json={**body, "max_total_exposure_pct": 101}).status_code == 422
    assert client.post("/api/paper/auto_rules", json={**body, "candidate_priority": "random"}).status_code == 400
    assert client.post("/api/paper/auto_rules", json={**body, "match_kind": "rule"}).status_code == 400


def test_auto_rule_update_roundtrip_state_and_missing_account(client):
    for account_id in ("default", "second"):
        client.post("/api/paper/account", json={"initial_cash": 100000, "account_id": account_id})
    body = {
        "name": "原规则", "match_kind": "strategy", "match_id": "demo", "side": "auto",
        "size_value": 10000, "enabled": False, "candidate_priority": "symbol", "risk_follow": True,
    }
    rule = client.post("/api/paper/auto_rules?account=second", json=body).json()["rule"]
    url = f"/api/paper/auto_rules/{rule['id']}"
    changes = {**body, "name": "新规则", "size_value": 20000, "max_positions": 3,
               "max_symbol_exposure_pct": 25, "max_total_exposure_pct": 80, "enabled": True}
    assert client.put(url, json=changes).status_code == 404
    response = client.put(url + "?account=second", json=changes)
    assert response.status_code == 200, response.text
    updated = response.json()["rule"]
    assert updated["id"] == rule["id"] and updated["created_at"] == rule["created_at"]
    assert updated["enabled"] is False
    for key in ("name", "size_value", "max_positions", "candidate_priority",
                "max_symbol_exposure_pct", "max_total_exposure_pct", "risk_follow"):
        assert updated[key] == changes[key]
    assert client.get("/api/paper/auto_rules?account=second").json()["rules"] == [updated]
    assert client.get("/api/paper/auto_rules").json()["rules"] == []
    assert client.put("/api/paper/auto_rules/arule_missing?account=second", json=changes).status_code == 404
    assert client.put(url + "?account=../other", json=changes).status_code == 400

    # Omitted optional fields retain the saved values, including old-style clients.
    minimal = {key: changes[key] for key in ("name", "match_kind", "match_id", "size_value")}
    assert client.put(url + "?account=second", json=minimal).json()["rule"] == updated
    assert client.put(url + "?account=second", json={**changes, "max_positions": 1.5}).status_code == 422
    assert client.put(url + "?account=second", json={**changes, "max_total_exposure_pct": 101}).status_code == 422
    assert client.put(url + "?account=second", json={**changes, "match_kind": "rule"}).status_code == 400
    assert client.put(url + "?account=second", json={**changes, "name": ""}).status_code == 400
    assert client.get("/api/paper/auto_rules?account=second").json()["rules"] == [updated]
