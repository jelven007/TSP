from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.strategy import pool_preferences


def test_sql_pool_is_account_and_tenant_scoped_with_conflicts_and_events(env):
    _, account = register(env)
    _, other = register(env)
    context = env["identity"].context(account, None, "pool-test")
    first = TenantDocuments(env["db"], context)
    same = TenantDocuments(env["db"], context)
    other_tenant = TenantDocuments(env["db"], env["identity"].context(other, None, "other-pool"))
    other_user = TenantDocuments(env["db"], replace(context, user_id=other["id"]))
    ids = {"s01", "s02"}
    saved = pool_preferences.save(first, ids, ids=["s01"], revision=0)
    assert pool_preferences.load(same, ids) == saved
    assert pool_preferences.load(other_tenant, ids) == {"ids": [], "revision": 0}
    assert pool_preferences.load(other_user, ids) == {"ids": [], "revision": 0}
    with first.transaction(write=False) as session:
        rows = session.connection.execute(
            "SELECT payload FROM business.full_events WHERE kind='strategy_pool_updated'").fetchall()
    assert rows[-1]["payload"] == {"user_id": account["id"]}

    def write(value):
        try:
            return pool_preferences.save(same, ids, ids=value, revision=1)
        except AccessError as error:
            return error.status

    with ThreadPoolExecutor(2) as threads:
        results = list(threads.map(write, [[], ["s02"]]))
    assert 409 in results
    assert sum(isinstance(value, dict) for value in results) == 1
    latest = pool_preferences.load(first, ids)
    assert latest["revision"] == 2
    assert pool_preferences.save(first, ids, ids=["s01"], revision=0, migrate=True) == latest


def test_sql_pool_rollback_does_not_publish_preference_or_event(env):
    _, account = register(env)
    first = TenantDocuments(env["db"], env["identity"].context(account, None, "pool-rollback"))
    with pytest.raises(RuntimeError), first.transaction() as session:
        pool_preferences.save(session, {"s01"}, ids=["s01"], revision=0)
        raise RuntimeError("abort")
    assert pool_preferences.load(first, {"s01"}) == {"ids": [], "revision": 0}
    with first.transaction(write=False) as session:
        assert session.connection.execute(
            "SELECT count(*) AS n FROM business.full_events WHERE kind='strategy_pool_updated'"
        ).fetchone()["n"] == 0
