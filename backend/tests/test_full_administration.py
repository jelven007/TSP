"""Administrator selection and token scope/revocation with real PostgreSQL."""
# ruff: noqa: F401, F811 -- reuse the isolated cluster and fixtures.
import uuid

import pytest
from fastapi import Request
from test_full_identity import env, full_cluster, register

from app.cell.context import AccessError
from app.multitenancy.access import verify_live_access
from app.multitenancy.research import authorize
from app.multitenancy.tokens import Tokens, router


def test_admin_tenant_directory_displays_owner_accounts_for_legacy_names(env):
    tenant_token, first = register(env)
    _, second = register(env)
    env["control"].execute(
        "UPDATE control.tenants SET name='个人工作空间' WHERE id=%s",
        (first["default_tenant_id"],),
    )
    admin_token, _ = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    client = env["client"]
    client.cookies.set("tf_session", admin_token)
    response = client.get("/api/account/tenants")
    assert response.status_code == 200, response.text
    entries = {entry["id"]: entry for entry in response.json()["tenants"]}
    assert entries[first["default_tenant_id"]]["name"] == "个人工作空间"
    assert entries[first["default_tenant_id"]]["display_name"] == first["email"]
    assert entries[second["default_tenant_id"]]["display_name"] == second["email"]

    client.cookies.set("tf_session", tenant_token)
    entries = client.get("/api/account/tenants").json()["tenants"]
    assert [entry["id"] for entry in entries] == [first["default_tenant_id"]]
    assert "display_name" not in entries[0]


def test_admin_tenant_directory_prefers_owner_username_without_duplicate_spaces(env):
    _, first = register(env)
    _, second = register(env)
    username = "tenant_" + uuid.uuid4().hex[:12]
    env["control"].execute(
        "UPDATE control.users SET username=%s WHERE id=%s", (username, first["id"]),
    )
    env["control"].execute(
        "INSERT INTO control.memberships(tenant_id,user_id,role) VALUES(%s,%s,'owner')",
        (first["default_tenant_id"], second["id"]),
    )
    _, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    entries = [
        entry for entry in env["identity"].directory(admin)
        if entry["id"] == first["default_tenant_id"]
    ]
    assert len(entries) == 1
    assert entries[0]["display_name"] == username


def test_admin_tenant_directory_preserves_named_spaces_without_an_owner(env):
    _, account = register(env)
    tenant_id = account["default_tenant_id"]
    env["control"].execute(
        "UPDATE control.tenants SET name='机构研究组' WHERE id=%s", (tenant_id,),
    )
    env["control"].execute(
        "DELETE FROM control.memberships WHERE tenant_id=%s", (tenant_id,),
    )
    _, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    entry = next(entry for entry in env["identity"].directory(admin) if entry["id"] == tenant_id)
    assert entry["display_name"] == "机构研究组"


def test_admin_selection_persists_and_old_ui_scope_is_rejected(env):
    _, first = register(env)
    _, second = register(env)
    token, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    client = env["client"]
    client.cookies.set("tf_session", token)
    assert client.post("/api/account/tenant", json={"tenant_id": first["default_tenant_id"]}).status_code == 200
    first_scope = client.get("/api/watchlist").headers["x-tsp-account-scope"]
    assert client.get("/api/watchlist").json()["tenant_id"] == first["default_tenant_id"]
    assert client.post("/api/account/tenant", json={"tenant_id": second["default_tenant_id"]}).status_code == 200
    assert client.get("/api/watchlist", headers={"X-TSP-Account-Scope": first_scope}).status_code == 401
    assert env["identity"].current_user(token)["default_tenant_id"] == second["default_tenant_id"]
    assert env["control"].execute(
        "SELECT 1 FROM control.audit WHERE actor_id=%s AND action='admin.tenant.select'", (admin["id"],),
    ).fetchone()
    # The other tenant remains inaccessible to a regular tenant even through selection.
    tenant_token, tenant = register(env)
    client.cookies.set("tf_session", tenant_token)
    assert client.post("/api/account/tenant", json={"tenant_id": first["default_tenant_id"]}).status_code == 403
    assert client.get("/api/account/tenants").json()["tenants"][0]["id"] == tenant["default_tenant_id"]


def test_tokens_bind_admin_target_and_scope_not_current_selection(env):
    _, first = register(env)
    _, second = register(env)
    session, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    service = env["identity"]
    service.choose_tenant(admin, first["default_tenant_id"], "token-setup")
    admin = service.current_user(session)
    tokens = Tokens(service)
    created = tokens.create(admin, "market", ["read:market"], 90, "token-create")
    value = created["plaintext"]
    assert value.startswith("tsp_ft_") and value not in str(tokens.list(admin))
    account, context = tokens.authenticate(value, None, "read", "GET", "/api/kline/daily")
    assert account["default_tenant_id"] == first["default_tenant_id"]
    assert context.token_id == created["token"]["id"] and "paper:write" not in context.permissions
    service.choose_tenant(admin, second["default_tenant_id"], "token-switch")
    assert tokens.authenticate(value, None, "after-switch", "GET", "/api/kline/daily")[1].tenant_id == context.tenant_id
    with pytest.raises(AccessError, match="目标租户"):
        tokens.authenticate(value, second["default_tenant_id"], "forge", "GET", "/api/kline/daily")
    _, stream_context = tokens.authenticate(value, None, "stream", "POST", "/api/events/ticket")
    assert stream_context.token_id == created["token"]["id"]
    assert stream_context.token_scopes == frozenset({"read:market"})
    for method, path in [("GET", "/api/settings"), ("GET", "/api/alerts"), ("POST", "/api/paper/orders"),
                         ("POST", "/api/ext-data/private/ingest")]:
        with pytest.raises(AccessError, match="无权访问"):
            tokens.authenticate(value, None, "deny", method, path)
    with pytest.raises(AccessError, match="不存在"):
        tokens.revoke(service.current_user(session), created["token"]["id"], "wrong-selection")
    tokens.revoke(admin, created["token"]["id"], "revoke")
    with pytest.raises(AccessError) as exc:
        tokens.authenticate(value, None, "revoked", "GET", "/api/kline/daily")
    assert exc.value.status == 401


def test_original_token_routes_and_external_post_do_not_require_cookie_or_origin(env):
    env["client"].app.include_router(router)

    @env["client"].app.get("/api/kline/daily")
    def read_market(request: Request):
        return {"tenant": request.state.tenant_context.tenant_id}

    @env["client"].app.post("/api/backtest/run")
    def compute(request: Request):
        verify_live_access(request)
        return {"token_id": request.state.tenant_context.token_id}

    _, tenant = register(env)
    session, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    env["identity"].choose_tenant(admin, tenant["default_tenant_id"], "route-setup")
    client = env["client"]
    client.cookies.set("tf_session", session)
    response = client.post("/api/settings/api-tokens", json={"name": "integration", "scopes": ["run:backtest"]})
    assert response.status_code == 200, response.text
    value = response.json()["plaintext"]
    item_id = response.json()["token"]["id"]
    client.cookies.clear()
    headers = {"Authorization": "Bearer " + value, "Origin": ""}
    assert client.post("/api/backtest/run", headers=headers).json() == {"token_id": item_id}
    assert client.get("/api/kline/daily", headers=headers).status_code == 403
    assert client.get("/api/settings/api-tokens", headers=headers).status_code == 403
    assert client.get("/api/auth/status", headers=headers).status_code == 403
    client.cookies.set("tf_session", session)
    assert client.delete("/api/settings/api-tokens/" + item_id).status_code == 200
    assert client.post("/api/backtest/run", headers=headers).status_code == 401
    assert client.post("/api/backtest/run", headers={"Origin": ""}).status_code == 403


def test_token_versions_and_background_authorization_fail_closed(env):
    _, account = register(env)
    _, root_admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    env["identity"].update_user(root_admin, account["id"], "admin", True, "promote")
    session, account = env["identity"].login(account["email"], "full-tenant-password", uuid.uuid4().hex)
    tokens = Tokens(env["identity"])
    created = tokens.create(account, "compute", ["run:backtest"], 1, "compute-token")
    _, context = tokens.authenticate(created["plaintext"], None, "compute", "POST", "/api/backtest/run")
    with authorize(env["identity"], context, account["auth_version"]) as narrowed:
        assert "strategies:write" in narrowed.permissions and "paper:write" not in narrowed.permissions
    env["control"].execute("UPDATE control.api_tokens SET routing_epoch=routing_epoch+1 WHERE id=%s",
                           (created["token"]["id"],))
    with pytest.raises(AccessError):
        tokens.authenticate(created["plaintext"], None, "stale", "POST", "/api/backtest/run")
    with pytest.raises(AccessError), authorize(env["identity"], context, account["auth_version"]):
        pytest.fail("stale background token accepted")
    another = tokens.create(account, "paper", ["paper:trade"], 1, "paper-token")
    env["identity"].change_password(session, "full-tenant-password", "changed-password")
    with pytest.raises(AccessError):
        tokens.authenticate(another["plaintext"], None, "password", "GET", "/api/paper")
    with pytest.raises(AccessError):
        tokens.create({**account, "permissions": ["business:read"]}, "forged", ["read:market"], 1, "deny")
    with pytest.raises(AccessError, match="授权已变更"):
        env["identity"].update_user(account, root_admin["id"], "tenant", True, "stale-admin")
    with pytest.raises(AccessError, match="授权已变更"):
        env["identity"].choose_tenant(account, account["default_tenant_id"], "stale-selection")


def test_tenant_notification_ui_contract_and_shared_mutations_are_guarded(env, monkeypatch):
    from app.multitenancy.notifications import router as notification_router

    monkeypatch.setattr(env["db"], "settings", env["db"].settings.model_copy(update={
        "smtp_host": "mail.example.test", "smtp_from": "sender@example.test",
    }))
    env["client"].app.include_router(notification_router)
    token, tenant = register(env)
    other_token, other = register(env)
    client = env["client"]
    client.cookies.set("tf_session", token)
    base = "/api/monitor-rules/notifications"
    response = client.post(base + "/channels", json={
        "name": "我的邮箱", "kind": "email", "destination": tenant["email"], "enabled": True,
    })
    assert response.status_code == 200, response.text
    channel = response.json()["channels"][0]
    prefs = {"default_channels": [channel["id"]], "review_channels": [channel["id"]], "review_push_mode": "auto"}
    assert client.put(base + "/preferences", json=prefs).json() == prefs
    schedule = {"enabled": True, "hour": 15, "minute": 40}
    assert client.post(base + "/review-schedule", json=schedule).json() == schedule
    display = client.get("/api/business/preferences").json()
    assert display["review_schedule"] == schedule and display["review_push_channels"] == [channel["id"]]
    client.cookies.set("tf_session", other_token)
    assert client.get(base + "/channels").json() == {"channels": []}
    assert client.get(base + "/deliveries").json() == {"deliveries": []}
    assert client.get(base + "/review-schedule").json()["enabled"] is False
    assert client.put(base + "/preferences", json=prefs).status_code == 400
    assert client.post(base + "/channels", json={
        **channel, "destination": other["email"],
    }).status_code == 404
    assert client.post(base + "/channels", json={
        "name": "另一邮箱", "kind": "email", "destination": tenant["email"], "enabled": True,
    }).status_code == 403
    for path in ["/api/kline/refresh_views", "/api/kline/rebuild_enriched", "/api/regime/recompute",
                 "/api/index/sync_daily", "/api/ext-data/source/pull/run"]:
        assert client.post(path, json={}).status_code == 403
    assert client.get("/api/ext-data/source/api-key").status_code == 403


def test_admin_platform_forwarding_preserves_body_and_excludes_browser_credentials(env, monkeypatch):
    import asyncio
    import json

    import httpx
    from starlette.requests import ClientDisconnect, Request

    from app.multitenancy.platform import PlatformAdmin

    monkeypatch.setenv("TSP_PLATFORM_ADMIN_TOKEN", "internal-test-token")
    seen = []

    async def remote(request):
        seen.append((request, await request.aread()))
        return httpx.Response(200, json={"queued": True})

    gateway = PlatformAdmin("https://platform.test")
    asyncio.run(gateway.client.aclose())
    gateway.client = httpx.AsyncClient(base_url="https://platform.test", transport=httpx.MockTransport(remote),
                                      headers={"Authorization": "Bearer internal-test-token"})
    env["client"].app.state.platform_admin = gateway
    client = env["client"]
    tenant_token, _ = register(env)
    client.cookies.set("tf_session", tenant_token)
    assert client.post("/api/pipeline/run", json={"years": 1}).status_code == 403
    assert not seen
    session, _ = env["identity"].login("admin", "Admin123", "platform-test")
    client.cookies.set("tf_session", session)
    response = client.post("/api/pipeline/run", json={"years": 1})
    assert response.status_code == 200 and response.json() == {"queued": True}
    assert response.headers["cache-control"] == "no-store"
    request, body = seen[0]
    assert json.loads(body) == {"years": 1}
    assert request.headers["authorization"] == "Bearer internal-test-token"
    assert "cookie" not in request.headers and "x-tenant-id" not in request.headers

    async def disconnected():
        raise ClientDisconnect

    read_request = Request({
        "type": "http", "method": "GET", "scheme": "https",
        "server": ("api.test", 443), "client": ("127.0.0.1", 1),
        "root_path": "", "path": "/api/settings", "raw_path": b"/api/settings",
        "query_string": b"", "headers": [], "state": {},
    }, disconnected)
    read_request.state.account = {"email": "admin@example.com"}
    read_response = asyncio.run(gateway.forward(read_request))
    assert read_response.status_code == 200
    assert seen[-1][1] == b""
    asyncio.run(gateway.close())


def test_tenant_public_market_forwarding_keeps_scope_and_uses_only_market_credentials(env):
    from fastapi.responses import JSONResponse

    seen = []
    class PublicReader:
        async def forward(self, request):
            seen.append(request.state.tenant_context.tenant_id)
            return JSONResponse({"rows": [{"symbol": "600000.SH", "close": 10}]})

    env["client"].app.state.platform_reads = PublicReader()
    token, user = register(env)
    _, other = register(env)
    client = env["client"]
    client.cookies.set("tf_session", token)
    assert client.get("/api/kline/daily").json()["rows"][0]["symbol"] == "600000.SH"
    assert seen == [user["default_tenant_id"]]
    assert client.get("/api/kline/daily", headers={"X-Tenant-ID": other["default_tenant_id"]}).status_code == 403
    assert len(seen) == 1
    env["control"].execute("UPDATE control.tenants SET status='suspended' WHERE id=%s",
                           (user["default_tenant_id"],))
    assert client.get("/api/kline/minute").status_code == 403
    assert len(seen) == 1


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_ext_config_reads_return_platform_pull_only_to_admin_sessions(env, tmp_path, method):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from app.api.ext_data import router as ext_router
    from app.multitenancy.platform import PlatformAdmin
    from app.services.ext_data import ExtConfigStore
    from app.services.ext_presets import _concept_preset

    preset = _concept_preset()
    private_config = preset.to_dict()
    private_config["pull"]["headers"] = {"X-Upstream-Key": "private-upstream-key"}
    preset.pull = None
    ExtConfigStore(tmp_path).upsert(preset)
    client = env["client"]
    client.app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client.app.include_router(ext_router)
    seen = []

    async def remote(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [private_config]})

    gateway = PlatformAdmin("https://platform.test", token="internal-test-token")
    asyncio.run(gateway.client.aclose())
    gateway.client = httpx.AsyncClient(
        base_url="https://platform.test",
        transport=httpx.MockTransport(remote),
        headers={"Authorization": "Bearer internal-test-token"},
    )
    client.app.state.platform_admin = gateway
    try:
        tenant_session, tenant = register(env)
        client.cookies.set("tf_session", tenant_session)
        public = client.get("/api/ext-data")
        assert public.status_code == 200, public.text
        assert "pull" not in public.json()["items"][0]
        assert "private-upstream-key" not in public.text
        assert not seen

        admin_session, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
        env["identity"].choose_tenant(admin, tenant["default_tenant_id"], "ext-config-read")
        client.cookies.set("tf_session", admin_session)
        response = client.request(method, "/api/ext-data", params={"source": "data-page"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        if method == "GET":
            assert response.json()["items"][0]["pull"] == private_config["pull"]
        assert len(seen) == 1
        forwarded = seen[0]
        assert forwarded.method == method
        assert forwarded.url.params["source"] == "data-page"
        assert forwarded.headers["authorization"] == "Bearer internal-test-token"
        assert "cookie" not in forwarded.headers and "x-tenant-id" not in forwarded.headers

        created = Tokens(env["identity"]).create(
            env["identity"].current_user(admin_session), "ext reader", ["read:ext"], 1, "ext-token",
        )
        headers = {"Authorization": "Bearer " + created["plaintext"]}
        assert client.get("/api/ext-data", headers=headers).status_code == 403
        assert len(seen) == 1
    finally:
        asyncio.run(gateway.close())
