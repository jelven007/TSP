"""目标分组在真实 PostgreSQL 中的隔离、删除竞态和事务一致性。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.multitenancy.documents import TenantDocuments
from app.multitenancy.watchlist import Watchlist
from app.strategy import config


def stores(env):
    _, account = register(env)
    context = env["identity"].context(account, None, "targets")
    return TenantDocuments(env["db"], context), Watchlist(env["db"], context)


def target(group_id):
    return {"basic_filter": {"enabled": False, "target_mode": "selected", "boards": [],
                             "include_custom": True, "watchlist_group_ids": [group_id]}}


def test_sql_targets_resolve_only_owned_groups_and_block_deletion(env):
    own, watchlist = stores(env)
    other, foreign = stores(env)
    _, group = watchlist.create_group("导入", symbols=["600000.SH", "300750.SZ"])
    config.save_override(own, "target", target(group["id"]))
    assert config.load_override(own, "target")["basic_filter"]["target_symbols"] == ["300750.SZ", "600000.SH"]
    assert "target_symbols" not in own.get("strategy_overrides", "target")["basic_filter"]
    assert foreign.list_groups() == []
    with pytest.raises(ValueError, match="不存在"):
        config.save_override(other, "target", target(group["id"]))
    with pytest.raises(ValueError, match="被策略引用"):
        watchlist.delete_group(group["id"])
    from app.cell.context import AccessError
    from app.cell.watchlist import Watchlist as CellWatchlist
    with pytest.raises(AccessError) as error:
        CellWatchlist(env["db"]).delete_group(own.context, group["id"])
    assert error.value.status == 409
    assert watchlist.list_groups()[0]["referenced_by"] == ["target"]
    watchlist.remove_from_group("600000.SH", group["id"])
    assert config.load_override(own, "target")["basic_filter"]["target_symbols"] == ["300750.SZ"]
    config.delete_override(own, "target")
    assert watchlist.delete_group(group["id"])[0] == []


@pytest.mark.parametrize("operation", ["clear", "clear_group", "cell_clear"])
def test_sql_clear_reference_protection_is_atomic_and_tenant_scoped(env, operation):
    from app.cell.context import AccessError
    from app.cell.watchlist import Watchlist as CellWatchlist

    own, watchlist = stores(env)
    _other, foreign = stores(env)
    _, group = watchlist.create_group("核心", symbols=["600000.SH"])
    _, second = watchlist.create_group("观察", symbols=["600000.SH"])
    foreign.add("000001.SZ")
    config.save_override(own, "target", target(group["id"]))
    before, rev = watchlist.list_symbols(), watchlist.revision()
    if operation == "cell_clear":
        with pytest.raises(AccessError) as error:
            CellWatchlist(env["db"]).remove(own.context, None)
        assert error.value.status == 409
    else:
        args = [group["id"]] if operation == "clear_group" else []
        with pytest.raises(ValueError, match="被策略引用"):
            getattr(watchlist, operation)(*args)
    assert watchlist.list_symbols() == before and watchlist.revision() == rev
    assert len(before) == 1 and set(before[0]["group_ids"]) == {group["id"], second["id"]}
    # 其他租户和不被引用的分组仍可清空。
    assert foreign.clear() == 1
    watchlist.clear_group(second["id"])
    assert watchlist.list_symbols()[0]["group_ids"] == [group["id"]]
    config.delete_override(own, "target")
    assert watchlist.clear() == 1


def test_sql_watchlist_clear_http_returns_conflict_and_preserves_members(env):
    from types import SimpleNamespace

    from app.api.watchlist import router

    token, account = register(env)
    context = env["identity"].context(account, None, "clear-http")
    own, watchlist = TenantDocuments(env["db"], context), Watchlist(env["db"], context)
    _, group = watchlist.create_group("核心", symbols=["600000.SH"])
    config.save_override(own, "target", target(group["id"]))
    client = env["client"]
    client.app.include_router(router)
    client.app.state.repo = SimpleNamespace(get_name_map=lambda _: {})
    client.cookies.set("tf_session", token)
    for method, path in [
        ("delete", "/api/watchlist"), ("post", f"/api/watchlist/groups/{group['id']}/clear"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 409, response.text
        assert "被策略引用" in response.json()["detail"]
    assert watchlist.list_symbols()[0]["group_ids"] == [group["id"]]
    config.delete_override(own, "target")
    assert client.delete("/api/watchlist").json() == {"removed": 1}


def test_sql_strategy_monitor_enable_reloads_current_target_members(env, monkeypatch):
    from types import SimpleNamespace

    from app.api import monitor_rules, strategy

    token, account = register(env)
    context = env["identity"].context(account, None, "enable-http")
    own, watchlist = TenantDocuments(env["db"], context), Watchlist(env["db"], context)
    _, group = watchlist.create_group("核心", symbols=["600000.SH"])
    config.save_override(own, "target", target(group["id"]))
    monkeypatch.setattr(strategy, "_get_engine", lambda _: SimpleNamespace(
        get=lambda _: object(), validate_context=lambda *_: None,
    ))
    load = config.load_override
    members = []

    def record(storage, sid):
        value = load(storage, sid)
        members.append(value["basic_filter"]["target_symbols"])
        return value

    monkeypatch.setattr(config, "load_override", record)
    client = env["client"]
    client.app.include_router(monitor_rules.router)
    client.app.state.repo = SimpleNamespace()
    client.cookies.set("tf_session", token)
    rule = {"id": "target_monitor", "name": "策略监控", "type": "strategy",
            "scope": "all", "strategy_id": "target", "enabled": True}
    assert client.post("/api/monitor-rules", json=rule).status_code == 200
    assert members == [["600000.SH"]]
    assert client.post("/api/monitor-rules", json={**rule, "enabled": False}).status_code == 200
    watchlist.add("300750.SZ", group_id=group["id"])
    assert client.post("/api/monitor-rules", json=rule).status_code == 200
    assert members == [["600000.SH"], ["300750.SZ", "600000.SH"]]
    with own.transaction(write=False) as unit:
        saved = unit.connection.execute("SELECT definition FROM business.monitor_rules").fetchone()["definition"]
    assert saved["symbols"] == [] and "target_symbols" not in saved["basic_filter"]


def test_sql_group_import_and_strategy_save_rollback_together(env):
    own, watchlist = stores(env)
    _, group = watchlist.create_group("核心")
    with pytest.raises(RuntimeError, match="undo"), own.transaction() as unit:
        config.save_override(unit, "target", target(group["id"]))
        assert config.load_override(unit, "target")["basic_filter"]["target_symbols"] == []
        raise RuntimeError("undo")
    assert config.load_override(own, "target") == {}
    assert watchlist.delete_group(group["id"])[0] == []
    with pytest.raises(RuntimeError, match="undo"), own.transaction() as unit:
        # 同一事务的分组/成员写入由数据库一起回滚。
        unit.connection.execute(
            "INSERT INTO business.watchlist_groups(tenant_id,id,name,color,position) VALUES(%s,'rollback','回滚','sky',0)",
            (own.context.tenant_id,),
        )
        config.save_override(unit, "target", target("rollback"))
        raise RuntimeError("undo")
    assert watchlist.list_groups() == [] and config.load_override(own, "target") == {}


def test_concurrent_save_and_delete_cannot_leave_a_dangling_group_reference(env):
    own, watchlist = stores(env)
    _, group = watchlist.create_group("竞态")

    def save():
        try:
            config.save_override(own, "race", target(group["id"]))
            return "saved"
        except ValueError:
            return "missing"

    def delete():
        try:
            watchlist.delete_group(group["id"])
            return "deleted"
        except ValueError:
            return "referenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(save), pool.submit(delete)
        result = (first.result(), second.result())
    assert result in {("saved", "referenced"), ("missing", "deleted")}
    assert bool(own.get("strategy_overrides", "race")) == bool(watchlist.list_groups())


def test_compute_task_freezes_target_members_for_saved_and_preview_configuration(env):
    from app.multitenancy.strategies import TenantStrategyEngine
    from app.strategy.engine import StrategyEngine

    own, watchlist = stores(env)
    _, group = watchlist.create_group("快照", symbols=["600000.SH"])
    config.save_override(own, "target", target(group["id"]))
    engine = TenantStrategyEngine(own, StrategyEngine([]), None)
    task = engine.task("backtest", config={"overrides": target(group["id"])})
    assert task["config"]["overrides"]["basic_filter"]["target_symbols"] == ["600000.SH"]
    watchlist.add("300750.SZ", group_id=group["id"])
    assert task["overrides"]["target"]["basic_filter"]["target_symbols"] == ["600000.SH"]
    assert engine.task("strategy")["overrides"]["target"]["basic_filter"]["target_symbols"] == ["300750.SZ", "600000.SH"]


def test_tenant_settings_reads_only_one_and_cannot_write_admin_limit(env):
    from app.api.settings import router

    token, _ = register(env)
    client = env["client"]
    client.app.include_router(router)
    client.cookies.set("tf_session", token)
    result = client.get("/api/settings/strategy-concurrency")
    assert result.status_code == 200
    assert result.json()["editable"] is False and result.json()["effective_concurrency"] == 1
    assert client.post("/api/settings/strategy-concurrency", json={"strategy_concurrency": 5}).status_code == 403
