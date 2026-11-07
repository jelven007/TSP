"""Account mutations preserve ledgers; deletion owns the complete account scope."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from app.services import alert_store
from app.strategy import paper, paper_auto

DAY = date(2026, 10, 14)
SYM = "600519.SH"


def test_edit_unused_account_updates_equity_and_preserves_identity(tmp_path):
    before = paper.create_account(tmp_path, 200_000, account_id="edit", name="旧名")
    paper.create_account(tmp_path, 500_000, account_id="other")
    edited = paper.update_account(
        tmp_path, "edit", name=" 新名 ", initial_cash=500_000,
        commission_pct=.0003, stamp_tax_pct=.0005, slippage_bps=8,
        queue_limit_orders=True,
    )
    assert edited["id"] == "edit" and edited["created_at"] == before["created_at"]
    assert edited["name"] == "新名" and edited["cash"] == edited["initial_cash"] == 500_000
    ov = paper.overview(tmp_path, account_id="edit")
    assert ov["account_name"] == "新名" and ov["total"] == 500_000 and ov["total_pnl"] == 0
    assert paper.get_account(tmp_path, "other")["cash"] == 500_000
    assert paper.list_accounts(tmp_path)[0]["name"] == "新名"


def test_account_edit_fee_extension_preserves_existing_records(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    before = paper.create_account(tmp_path, 200_000)
    paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10)
    paper.evaluate_intraday(tmp_path, {SYM: 10})
    fills = paper.load_fills(tmp_path)
    cash = paper.get_account(tmp_path)["cash"]
    updated = paper.update_account(tmp_path, name="新名", min_commission=3,
                                   transfer_fee_pct=.00001, stamp_tax_mode="historical")
    assert updated["name"] == "新名" and updated["cash"] == cash
    assert updated["initial_cash"] == before["initial_cash"]
    assert updated["min_commission"] == 3 and updated["transfer_fee_pct"] == .00001
    assert updated["stamp_tax_mode"] == "historical"
    assert paper.load_fills(tmp_path) == fills
    with pytest.raises(ValueError):
        paper.update_account(tmp_path, name="不应保存", min_commission=-1)
    assert paper.get_account(tmp_path) == updated


@pytest.mark.parametrize("fields", [
    {"name": " "}, {"name": "x" * 81}, {"initial_cash": 0},
    {"initial_cash": True}, {"slippage_bps": float("nan")},
    {"commission_pct": True}, {"queue_limit_orders": "false"}, {"cash": 1}, {"id": "changed"},
])
def test_edit_rejects_invalid_fields_without_partial_changes(tmp_path, fields):
    before = paper.create_account(tmp_path, 200_000)
    with pytest.raises(ValueError):
        paper.update_account(tmp_path, name="合法名字", **fields) if "name" not in fields else paper.update_account(tmp_path, **fields)
    assert paper.get_account(tmp_path) == before


@pytest.mark.parametrize("activity", ["order", "fill", "position", "nav", "corrupt_position"])
def test_initial_capital_is_locked_after_activity(tmp_path, activity, monkeypatch):
    paper.create_account(tmp_path, 200_000)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    if activity in {"order", "fill"}:
        order, error = paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10)
        assert error is None
        if activity == "order":
            paper.cancel_order(tmp_path, order["id"])
        else:
            paper.evaluate_intraday(tmp_path, {SYM: 10})
    elif activity == "position":
        (paper._root(tmp_path) / "positions.json").write_text(json.dumps({"positions": {SYM: {"qty": 100}}}))
    elif activity == "corrupt_position":
        (paper._root(tmp_path) / "positions.json").write_text("invalid")
    else:
        paper._write_nav_line(tmp_path, DAY.isoformat(), {"date": DAY.isoformat(), "nav": 200_000})
    before = paper.get_account(tmp_path)
    with pytest.raises(ValueError, match="初始资金"):
        paper.update_account(tmp_path, name="不能部分修改", initial_cash=500_000)
    assert paper.get_account(tmp_path) == before
    fills = paper.load_fills(tmp_path)
    paper.update_account(tmp_path, name="仍可改名", commission_pct=.001)
    assert paper.load_fills(tmp_path) == fills
    assert paper.get_account(tmp_path)["cash"] == before["cash"]


def test_delete_cascades_account_but_preserves_other_account_and_shared_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    for aid in ("delete", "other"):
        paper.create_account(tmp_path, 200_000, account_id=aid)
        paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10, account_id=aid)
        paper.evaluate_intraday(tmp_path, {SYM: 10}, aid)
        paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10, account_id=aid)
        paper._write_nav_line(tmp_path, DAY.isoformat(), {"date": DAY.isoformat(), "nav": 200_000}, aid)
        paper_auto.create_auto_rule(tmp_path, {
            "name": "共享来源", "match_kind": "strategy", "match_id": "shared", "size_value": 1000,
        }, aid)
        for kind in ("event", "exit", "exit_archive", "risk", "auto_skips"):
            path = paper._root(tmp_path, aid) / kind
            path.mkdir(exist_ok=True)
            (path / "record.json").write_text("{}")
    alert_store.append_many(tmp_path, [
        {"ts": 1, "source": "paper", "account_id": "delete"},
        {"ts": 2, "source": "paper", "account_id": "other"},
        {"ts": 3, "source": "strategy", "account_id": "delete"},
    ])
    other_before = {p.relative_to(paper._root(tmp_path, "other")): p.read_bytes()
                    for p in paper._root(tmp_path, "other").rglob("*") if p.is_file()}
    assert paper.delete_account(tmp_path, "delete") is True
    assert not (paper.accounts_base(tmp_path) / "delete").exists()
    assert paper.list_account_ids(tmp_path) == ["other"]
    remaining = [json.loads(line) for line in alert_store._path(tmp_path).read_text().splitlines()]
    assert not any(e.get("source") == "paper" and e.get("account_id") == "delete" for e in remaining)
    assert any(e.get("account_id") == "other" for e in remaining)
    assert any(e.get("source") == "strategy" for e in remaining)
    assert other_before == {p.relative_to(paper._root(tmp_path, "other")): p.read_bytes()
                            for p in paper._root(tmp_path, "other").rglob("*") if p.is_file()}


def test_deleted_account_reads_and_late_workers_never_recreate_it(tmp_path):
    paper.create_account(tmp_path, 200_000)
    stale = paper.get_account(tmp_path)
    paper.delete_account(tmp_path)
    assert paper.get_account(tmp_path) is None
    assert paper.load_orders(tmp_path) == paper.load_fills(tmp_path) == paper.load_nav(tmp_path) == []
    assert paper_auto.load_auto_rules(tmp_path) == paper_auto.load_exit_intents(tmp_path) == []
    assert paper_auto.load_auto_skips(tmp_path) == []
    assert paper.evaluate_intraday(tmp_path, {SYM: 10}) == []
    assert paper.settle_day(tmp_path, DAY.isoformat())["nav"] is None
    with pytest.raises(ValueError, match="尚未创建"):
        paper.save_account(tmp_path, stale)
    with pytest.raises(ValueError, match="尚未创建"):
        paper_auto.create_auto_rule(tmp_path, {
            "name": "迟到规则", "match_kind": "strategy", "match_id": "shared", "size_value": 1000,
        })
    assert not (paper.accounts_base(tmp_path) / "default").exists()
    assert paper.delete_account(tmp_path) is False


def test_delete_rejects_symlink_scope_and_is_serialized_with_matching(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "account.json").write_text('{"id":"linked"}')
    base = paper.accounts_base(tmp_path)
    base.mkdir(parents=True)
    (base / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        paper.delete_account(tmp_path, "linked")
    assert (outside / "account.json").exists()
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 200_000)
    paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda op: op(), [
            lambda: paper.evaluate_intraday(tmp_path, {SYM: 10}),
            lambda: paper.delete_account(tmp_path),
        ]))
    assert paper.get_account(tmp_path) is None
    assert not (base / "default").exists()


@pytest.mark.parametrize("failure", ["remove", "alerts"])
def test_failed_delete_restores_account_and_alerts(tmp_path, monkeypatch, failure):
    from app.strategy import paper_lifecycle

    paper.create_account(tmp_path, 200_000)
    root = paper._root(tmp_path)
    alert_store.append_many(tmp_path, [{"ts": 1, "source": "paper", "account_id": "default"}])
    before = (root / "account.json").read_bytes()
    alerts_before = alert_store._path(tmp_path).read_bytes()
    if failure == "remove":
        original = paper_lifecycle.shutil.rmtree

        def fail(path, *args, **kwargs):
            if path == root:
                (root / "account.json").unlink()
                raise PermissionError("fixture")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(paper_lifecycle.shutil, "rmtree", fail)
    else:
        monkeypatch.setattr(paper_lifecycle, "atomic_write_text", lambda *a: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(OSError):
        paper.delete_account(tmp_path)
    assert (root / "account.json").read_bytes() == before
    assert alert_store._path(tmp_path).read_bytes() == alerts_before


def test_late_paper_alerts_after_delete_are_discarded(tmp_path):
    paper.create_account(tmp_path, 200_000)
    paper.delete_account(tmp_path)
    alert_store.append_many(tmp_path, [
        {"ts": 1, "source": "paper", "account_id": "default"},
        {"ts": 2, "source": "strategy"},
    ])
    remaining = [json.loads(line) for line in alert_store._path(tmp_path).read_text().splitlines()]
    assert remaining == [{"ts": 2, "source": "strategy"}]


def test_account_change_events_require_paper_permission(tmp_path, monkeypatch):
    from app.services.events import bus

    published = []
    monkeypatch.setattr(bus, "publish", lambda kind, payload, scope: published.append((kind, payload, scope)))
    paper.create_account(tmp_path, 200_000)
    paper.update_account(tmp_path, name="新名")
    assert published == [("paper_changed", {"account_id": "default", "action": "updated"}, "paper:trade")]


def test_private_account_events_follow_paper_scope():
    from types import SimpleNamespace

    from app.multitenancy.events import private_event_visible

    event = {"kind": "paper_changed"}
    assert private_event_visible(SimpleNamespace(token_scopes=["paper:trade"]), event)
    assert not private_event_visible(SimpleNamespace(token_scopes=["read:analysis"]), event)
