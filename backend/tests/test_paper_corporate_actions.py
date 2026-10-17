from datetime import date

import pytest

from app.data_providers.corporate_actions import CorporateAction
from app.services.corporate_actions import save_actions
from app.strategy import paper, paper_actions

SYM = "600519.SH"


def event(**kwargs):
    return CorporateAction(
        id="dividend-2026", symbol=SYM, source="fixture",
        record_date=date(2026, 9, 21), ex_date=date(2026, 9, 22),
        dates_verified=True, evidence="issuer announcement fixture",
        **kwargs,
    )


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 9, 22))
    paper.create_account(tmp_path, 100_000, slippage_bps=0)
    paper._append_fill(tmp_path, {
        "kind": "fill", "symbol": SYM, "asset_type": "stock",
        "order_id": "entry", "date": "2026-09-21",
        "side": "buy", "qty": 1000, "price": 10, "fee": 0,
    })
    paper.rebuild_positions(tmp_path)
    return tmp_path


def test_cash_dividend_is_receivable_then_cash_never_extra_shares(account):
    save_actions(account, [event(cash_per_share=1, cash_pay_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-22")
    assert paper.load_positions(account)[SYM]["qty"] == 1000
    assert paper.get_account(account)["cash"] == 90_000
    nav = paper.daily_nav(account, "2026-09-22", {SYM: 9})
    assert nav["receivables"] == 1000
    assert nav["nav"] == 100_000
    paper_actions.advance(account, "2026-09-24")
    assert paper.get_account(account)["cash"] == 91_000
    paper_actions.advance(account, "2026-09-24")
    assert paper.replay_positions(account)[1] == 91_000
    assert paper.daily_nav(account, "2026-09-24", {SYM: 9})["receivables"] == 0


def test_bonus_entitlement_uses_record_close_and_unlocks_on_listing(account):
    save_actions(account, [event(bonus_per_share=.5, bonus_listing_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-22")
    pos = paper.load_positions(account)[SYM]
    assert pos["qty"] == 1500
    assert pos["avg_cost"] * pos["qty"] == pytest.approx(10000, abs=.01)
    assert paper._available_of(pos, "2026-09-23") == 1000
    assert paper._available_of(pos, "2026-09-24") == 1500
    assert paper.daily_nav(account, "2026-09-22", {SYM: 10 / 1.5})["nav"] == 100_000


def test_rights_need_explicit_payment_and_wait_for_listing(account, monkeypatch):
    action = event(rights_per_share=.2, rights_price=5,
                   rights_start_date=date(2026, 9, 22), rights_end_date=date(2026, 9, 23),
                   rights_listing_date=date(2026, 9, 25))
    save_actions(account, [action])
    paper_actions.advance(account, "2026-09-22")
    assert paper.get_account(account)["cash"] == 90_000
    paper_actions.subscribe(account, action.id, 200)
    assert paper.get_account(account)["cash"] == 89_000
    assert paper.load_positions(account)[SYM]["qty"] == 1000
    assert paper.daily_nav(account, "2026-09-22", {SYM: 11000 / 1200})["nav"] == 100_000
    paper_actions.subscribe(account, action.id, 200)  # one immutable election
    assert paper.get_account(account)["cash"] == 89_000
    paper_actions.advance(account, "2026-09-25")
    pos = paper.load_positions(account)[SYM]
    assert pos["qty"] == 1200
    assert pos["qty"] * pos["avg_cost"] == pytest.approx(11_000, abs=.01)
    assert paper._available_of(pos, "2026-09-25") == 1200


def test_unknown_dates_fail_closed_and_legacy_factor_replay_survives(account):
    action = CorporateAction(id="unknown", symbol=SYM, source="rustdx",
                             ex_date=date(2026, 9, 22), cash_per_share=1)
    save_actions(account, [action])
    result = paper_actions.advance(account, "2026-09-22")
    assert result["blocked"][0]["id"] == "unknown"
    assert paper.load_positions(account)[SYM]["qty"] == 1000
    paper._append_fill(account, {"kind": "corp_action", "symbol": SYM,
                                "date": "2026-09-22", "factor": 1.25})
    assert paper.replay_positions(account)[0][SYM]["qty"] == 1250


def test_conflicting_provider_revision_does_not_rewrite_posted_entitlement(account):
    save_actions(account, [event(cash_per_share=1, cash_pay_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-22")
    save_actions(account, [event(cash_per_share=2, cash_pay_date=date(2026, 9, 24))])
    assert paper_actions.advance(account, "2026-09-24")["blocked"]
    assert paper.get_account(account)["cash"] == 90_000


def test_rights_payment_before_ex_date_preserves_equity(account):
    action = event(rights_per_share=.2, rights_price=5,
                   rights_start_date=date(2026, 9, 22), rights_end_date=date(2026, 9, 23),
                   rights_listing_date=date(2026, 9, 25)).model_copy(update={"ex_date": date(2026, 9, 24)})
    save_actions(account, [action])
    paper_actions.subscribe(account, action.id, 200)
    assert paper.daily_nav(account, "2026-09-22", {SYM: 10})["nav"] == 100_000
    paper_actions.advance(account, "2026-09-24")
    assert paper.daily_nav(account, "2026-09-24", {SYM: 11000 / 1200})["nav"] == 100_000


def test_cash_entitlement_survives_sale_before_payment(account):
    save_actions(account, [event(cash_per_share=1, cash_pay_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-22")
    paper._append_fill(account, {
        "kind": "fill", "symbol": SYM, "order_id": "exit", "date": "2026-09-22",
        "side": "sell", "qty": 1000, "price": 9, "fee": 0,
    })
    paper.rebuild_positions(account)
    assert paper.daily_nav(account, "2026-09-22", {SYM: 9})["nav"] == 100_000
    paper_actions.advance(account, "2026-09-24")
    assert paper.replay_positions(account)[1] == 100_000
    assert paper.stats(account)["corporate_income"] == 1000


def test_new_ex_date_purchase_gets_no_registered_dividend(account):
    save_actions(account, [event(cash_per_share=1, cash_pay_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-22")
    paper._append_fill(account, {
        "kind": "fill", "symbol": SYM, "order_id": "new", "date": "2026-09-22",
        "side": "buy", "qty": 100, "price": 9, "fee": 0,
    })
    paper_actions.advance(account, "2026-09-24")
    assert paper.load_positions(account)[SYM]["qty"] == 1100
    assert paper.get_account(account)["cash"] == 90_100


def test_locked_bonus_is_not_consumed_instead_of_sellable_new_lot(account):
    save_actions(account, [event(bonus_per_share=.5, bonus_listing_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-22")
    for side, qty, day, oid in [("buy", 100, "2026-09-22", "new"), ("sell", 1100, "2026-09-23", "exit")]:
        paper._append_fill(account, {
            "kind": "fill", "symbol": SYM, "order_id": oid, "date": day,
            "side": side, "qty": qty, "price": 7, "fee": 0,
        })
    pos = paper.replay_positions(account)[0][SYM]
    assert pos["qty"] == 500
    assert paper._available_of(pos, "2026-09-23") == 0
    assert paper._available_of(pos, "2026-09-24") == 500
    assert sum(row["cost_basis"] for row in paper.round_trips(account)) == 10_700


def test_fractional_shares_fail_closed_and_odd_lot_final_sale_is_allowed(account, monkeypatch):
    save_actions(account, [event(bonus_per_share=.0001, bonus_listing_date=date(2026, 9, 24))])
    assert paper_actions.advance(account, "2026-09-22")["blocked"]
    assert paper.load_positions(account)[SYM]["qty"] == 1000
    save_actions(account, [event(bonus_per_share=.05, bonus_listing_date=date(2026, 9, 24))])
    paper_actions.advance(account, "2026-09-24")
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 9, 24))
    assert paper.create_order(account, SYM, "sell", qty=50)[1]
    assert paper.create_order(account, SYM, "sell", qty=1050)[1] is None


def test_unknown_event_does_not_certify_nav_or_fill(account):
    save_actions(account, [CorporateAction(
        id="unknown", symbol=SYM, source="rustdx", ex_date=date(2026, 9, 22), cash_per_share=1,
    )])
    order, err = paper.create_order(account, SYM, "sell", qty=1000, ref_price=9)
    assert err is None
    assert paper._fill_order(account, order, 9, "2026-09-22") is None
    assert paper.get_order(account, order["id"])["status"] == "pending"
    assert paper.settle_day(account, "2026-09-22")["nav"] is None
    assert paper.load_nav(account) == []


def test_old_events_before_first_position_do_not_block(account):
    old = CorporateAction(id="old", symbol=SYM, source="rustdx",
                          ex_date=date(2025, 9, 22), cash_per_share=1)
    save_actions(account, [old])
    assert paper_actions.advance(account, "2026-09-22")["blocked"] == []


def test_insufficient_cash_frozen_and_invalid_rights_election(account):
    action = event(rights_per_share=.2, rights_price=1000,
                   rights_start_date=date(2026, 9, 22), rights_end_date=date(2026, 9, 23),
                   rights_listing_date=date(2026, 9, 25))
    save_actions(account, [action])
    with pytest.raises(ValueError, match="资金不足"):
        paper_actions.subscribe(account, action.id, 200)
    paper.set_frozen(account, frozen=True)
    with pytest.raises(ValueError, match="冻结"):
        paper_actions.subscribe(account, action.id, 1)
    paper.set_frozen(account, frozen=False)
    with pytest.raises(ValueError, match="数量"):
        paper_actions.subscribe(account, action.id, 201)
    paper_actions.subscribe(account, action.id, 10)
    with pytest.raises(ValueError, match="不可改写"):
        paper_actions.subscribe(account, action.id, 11)
    assert paper.get_account(account)["cash"] == 80_000


def test_crash_after_journal_append_recovers_from_replay(account, monkeypatch):
    save_actions(account, [event(cash_per_share=1, cash_pay_date=date(2026, 9, 24))])
    original = paper.rebuild_positions
    monkeypatch.setattr(paper, "rebuild_positions", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        paper_actions.advance(account, "2026-09-24")
    monkeypatch.setattr(paper, "rebuild_positions", original)
    paper_actions.advance(account, "2026-09-24")
    assert paper.get_account(account)["cash"] == 91_000
    assert len([f for f in paper.load_fills(account) if f.get("stage") == "cash_paid"]) == 1
