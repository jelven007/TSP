"""S02 ledger operations on disposable PostgreSQL databases and real RLS."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register
from test_paper_s02_execution import DAY, SYM, advance, bars, entry
from test_paper_s02_execution import setup as file_setup
from test_paper_s02_execution import (
    test_buy_matches_shared_window_fees_and_survives_replay as check_buy,
)
from test_paper_s02_execution import test_interleaving_stops_new_fills as check_interleaving
from test_paper_s02_execution import (
    test_t1_exit_at_s02_trigger_is_capacity_limited_and_fee_aggregated as check_exit,
)

from app.multitenancy.paper_storage import TenantPaper
from app.strategy import paper, paper_auto


@pytest.fixture
def sql_setup(env, tmp_path, monkeypatch):
    base = file_setup.__wrapped__(tmp_path, monkeypatch)
    _, owner = register(env)
    _, other = register(env)
    storage = TenantPaper(env["db"], env["identity"].context(owner, None, "paper"), tmp_path)
    isolated = TenantPaper(env["db"], env["identity"].context(other, None, "paper"), tmp_path)
    paper.create_account(storage, 1_000_000.)
    rule = paper_auto.create_auto_rule(storage, base[2])
    paper.create_account(isolated, 1_000_000.)
    return (storage, base[1], rule, base[3], base[4]), isolated


def test_sql_shared_execution_and_tenant_isolation(sql_setup):
    setup, other = sql_setup
    check_buy(setup)
    assert paper.load_fills(other) == [] and paper.load_orders(other) == []
    assert paper.get_account(other)["cash"] == 1_000_000.


def test_sql_t1_exit_and_aggregate_fees(sql_setup):
    check_exit(sql_setup[0])


@pytest.mark.parametrize("action", ["cancel", "disable", "freeze", "delete", "change"])
def test_sql_rule_and_account_interleavings(sql_setup, action):
    check_interleaving(sql_setup[0], action)


def test_sql_ambiguous_commit_is_idempotent_under_parallel_retries(sql_setup):
    setup, _other = sql_setup
    storage, clock, _rule, repo, _event = setup
    entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    clock[0] = start.replace(tzinfo=clock[0].tzinfo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: paper_auto.on_rule_events(
            storage, [], minute_repo=repo, completed_through=clock[0]), range(4)))
    assert len(paper.load_fills(storage)) == 1
    assert paper.load_positions(storage)[SYM]["qty"] == 100
    assert paper.get_account(storage)["cash"] == pytest.approx(paper.replay_positions(storage)[1])


def test_sql_fragment_failure_rolls_back_account_order_and_fill(sql_setup, monkeypatch):
    setup, _other = sql_setup
    storage, _clock, _rule, repo, _event = setup
    order = entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    original = paper.save_order

    def fail_after_append(data, value, *args, **kwargs):
        if value.get("filled_qty"):
            raise RuntimeError("after append")
        return original(data, value, *args, **kwargs)

    monkeypatch.setattr(paper, "save_order", fail_after_append)
    with pytest.raises(RuntimeError, match="after append"):
        advance(setup, start)
    assert paper.load_fills(storage) == [] and paper.get_account(storage)["cash"] == 1_000_000.
    assert not paper.get_order(storage, order["id"]).get("filled_qty")
    monkeypatch.setattr(paper, "save_order", original)
    advance(setup, start)
    assert len(paper.load_fills(storage)) == 1
