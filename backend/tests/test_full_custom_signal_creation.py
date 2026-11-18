"""Tenant signal creation uses the existing transaction and revision guard."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_custom_signal_creation import definition
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.multitenancy.documents import TenantDocuments
from app.strategy import custom_signals


def test_create_conflict_preserves_tenant_record_and_other_tenant_can_use_same_id(env):
    _, first = register(env)
    _, second = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(first, None, "signal-create"))
    other = TenantDocuments(env["db"], env["identity"].context(second, None, "signal-other"))
    original = definition()
    custom_signals.save_one(own, original)
    before = own.record("custom_signals", original["id"])
    with pytest.raises(FileExistsError, match="已存在"):
        custom_signals.save_one(own, definition(name="Replacement"), create_only=True)
    assert own.record("custom_signals", original["id"]) == before
    custom_signals.save_one(other, definition(name="Other tenant"), create_only=True)
    assert custom_signals.load_all(own) == [original]
    assert custom_signals.load_all(other) == [definition(name="Other tenant")]
    custom_signals.save_one(own, definition(name="Edited"))
    assert custom_signals.load_all(own) == [definition(name="Edited")]


def test_concurrent_tenant_creates_persist_exactly_one_record(env):
    _, user = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(user, None, "signal-concurrent"))

    def create(name):
        try:
            custom_signals.save_one(own, definition(name=name), create_only=True)
            return name
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["First", "Second"]))
    winners = [name for name in results if name is not None]
    assert len(winners) == 1
    assert custom_signals.load_all(own) == [definition(name=winners[0])]
