"""Short-lived tenant stream authorization and targeted event wakeups."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.cell.context import AccessError, Role, TenantContext
from app.cell.passwords import token_hash
from app.multitenancy import events
from app.multitenancy.access import middleware
from app.multitenancy.events import EventHub
from app.multitenancy.stream_auth import RevocationState, StreamTicketCodec


def _principal():
    account = {
        "id": "user-1",
        "default_tenant_id": "tenant-1",
        "auth_version": 7,
    }
    context = TenantContext(
        "tenant-1", "user-1", "cell-1", Role.MEMBER,
        frozenset({"watchlist:read", "alerts:read"}), 3, 11, "request-1",
    )
    return account, context


def test_stream_ticket_roundtrip_is_bound_and_single_use(monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("app.multitenancy.stream_auth.time.time", lambda: clock[0])
    codec = StreamTicketCodec(SecretStr("s" * 48), ttl_seconds=60)
    account, context = _principal()

    ticket, ttl = codec.issue(account, context, principal="session:digest-1")
    claims = codec.consume(ticket)

    assert ttl == 60
    assert claims.user_id == account["id"]
    assert claims.tenant_id == context.tenant_id
    assert claims.cell_id == context.cell_id
    assert claims.auth_version == account["auth_version"]
    assert claims.policy_version == context.policy_version
    assert claims.routing_epoch == context.routing_epoch
    assert claims.principal == "session:digest-1"
    assert claims.audience == "tsp-tenant-events"
    assert claims.token_id is None
    assert claims.token_scopes == ()
    assert codec.consume(ticket) is None


def test_api_stream_ticket_preserves_scopes_and_private_events_fail_closed():
    codec = StreamTicketCodec(SecretStr("s" * 48), ttl_seconds=60)
    account, session_context = _principal()
    market_context = replace(
        session_context,
        token_id="token-market",
        token_scopes=frozenset({"read:market"}),
    )

    ticket, _ = codec.issue(account, market_context, principal="api:token-market")
    claims = codec.consume(ticket)
    restored = claims.context("stream-request")

    assert claims.token_id == "token-market"
    assert claims.token_scopes == ("read:market",)
    assert restored.token_id == "token-market"
    assert restored.token_scopes == frozenset({"read:market"})
    assert not events.private_event_visible(restored, {"kind": "strategy_alert"})
    assert not events.private_event_visible(restored, {"kind": "future_private_event"})
    assert events.private_event_visible(restored, {"kind": "events_reset"})

    analysis_context = replace(restored, token_scopes=frozenset({"read:analysis"}))
    assert events.private_event_visible(analysis_context, {"kind": "strategy_alert"})
    assert events.private_event_visible(analysis_context, {"kind": "strategy_results_updated"})
    assert events.private_event_visible(analysis_context, {"kind": "review_progress"})
    assert not events.private_event_visible(analysis_context, {"kind": "future_private_event"})
    assert events.private_event_visible(session_context, {"kind": "future_private_event"})


def test_stream_ticket_rejects_tampering_expiry_and_wrong_audience(monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("app.multitenancy.stream_auth.time.time", lambda: clock[0])
    codec = StreamTicketCodec(SecretStr("s" * 48), ttl_seconds=60)
    account, context = _principal()

    ticket, _ = codec.issue(account, context, principal="session:digest-1")
    body, signature = ticket.removeprefix("tss_").split(".", 1)
    changed = body[:-1] + ("A" if body[-1] != "A" else "B")
    assert codec.consume(f"tss_{changed}.{signature}") is None

    ticket, _ = codec.issue(account, context, principal="session:digest-1")
    clock[0] += 61
    assert codec.consume(ticket) is None

    other = StreamTicketCodec(SecretStr("s" * 48), audience="other", ttl_seconds=60)
    clock[0] -= 61
    ticket, _ = codec.issue(account, context, principal="session:digest-1")
    assert other.consume(ticket) is None


def test_multitenant_ticket_endpoint_binds_browser_session():
    from app.api.events import issue_ticket

    account, context = _principal()
    codec = StreamTicketCodec(SecretStr("s" * 48), ttl_seconds=60)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            account_identity=object(),
            stream_tickets=codec,
        )),
        state=SimpleNamespace(account=account, tenant_context=context),
        cookies={"tf_session": "browser-session"},
    )

    response = issue_ticket(request)
    claims = codec.consume(response["ticket"])
    assert response["expires_in"] == 60
    assert claims.principal == "session:" + token_hash("browser-session")
    assert claims.tenant_id == context.tenant_id


def test_multitenant_event_endpoint_consumes_ticket_once():
    from app.api.events import event_stream

    account, context = _principal()
    codec = StreamTicketCodec(SecretStr("s" * 48), ttl_seconds=60)
    ticket, _ = codec.issue(account, context, principal="session:digest-1")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            account_identity=object(),
            stream_tickets=codec,
        )),
        state=SimpleNamespace(request_id="request-2"),
    )

    response = asyncio.run(event_stream(request, ticket))
    assert response.status_code == 200
    assert request.state.tenant_context.tenant_id == context.tenant_id
    assert request.state.stream_claims.principal == "session:digest-1"

    replay = asyncio.run(event_stream(request, ticket))
    assert replay.status_code == 401


def test_revocation_state_matches_principal_user_and_tenant(monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("app.multitenancy.stream_auth.time.time", lambda: clock[0])
    account, context = _principal()

    def claims(principal="session:digest-1"):
        codec = StreamTicketCodec(SecretStr("s" * 48), ttl_seconds=60)
        ticket, _ = codec.issue(account, context, principal=principal)
        return codec.consume(ticket)

    state = RevocationState(retention_seconds=120)
    current = claims()
    state.ensure_active(current)
    state.apply(json.dumps({"principal": "session:digest-1", "at": clock[0] + 1}))
    with pytest.raises(AccessError, match="重新连接"):
        state.ensure_active(current)

    current = claims("session:digest-2")
    state.apply(json.dumps({"user_id": "user-1", "at": clock[0] + 1}))
    with pytest.raises(AccessError, match="重新连接"):
        state.ensure_active(current)

    current = claims("session:digest-3")
    state.apply(json.dumps({"tenant_id": "tenant-1", "at": clock[0] + 1}))
    with pytest.raises(AccessError, match="重新连接"):
        state.ensure_active(current)


def test_gateway_rejects_routes_outside_realtime_surface():
    app = FastAPI()
    app.state.service_role = "gateway"
    app.middleware("http")(middleware)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/watchlist")
    def watchlist():
        return {"items": []}

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/api/watchlist")
        assert response.status_code == 404
        assert response.json()["detail"] == "gateway_route_not_found"


def test_gateway_readiness_requires_databases_and_all_listeners():
    from app.multitenancy.gateway import readiness

    class Connection:
        def execute(self, _query):
            return self

        def fetchone(self):
            return {"ok": 1}

    class Pool:
        @contextmanager
        def connection(self):
            yield Connection()

    state = SimpleNamespace(
        account_identity=SimpleNamespace(db=SimpleNamespace(control=Pool(), data=Pool())),
        tenant_event_hub=SimpleNamespace(status=lambda: {
            "listener_connected": True,
            "subscriptions": 2,
            "poll_runs": 3,
            "tenant_reads": 4,
            "notifications": 5,
            "full_sweeps": 1,
            "last_event_at": None,
        }),
        stream_revocations=SimpleNamespace(status=lambda: {
            "connected": True,
            "last_notification_at": None,
        }),
        quote_service=SimpleNamespace(status=lambda: {
            "connected": True,
            "last_marker_at": None,
            "reconnects": 0,
        }),
    )

    result = readiness(state, None)
    assert result["ready"] and result["status"] == "ok"
    assert set(result["checks"]) == {
        "databases", "private_events", "revocations", "public_market_events",
    }

    state.quote_service.status = lambda: {
        "connected": False,
        "last_marker_at": None,
        "reconnects": 1,
    }
    result = readiness(state, None)
    assert not result["ready"]
    assert result["checks"]["public_market_events"]["status"] == "error"


def test_event_hub_notification_polls_only_target_tenant(monkeypatch):
    reads = []

    class Log:
        def __init__(self, documents):
            self.documents = documents

        def head(self):
            return 0

        def since(self, cursor):
            reads.append((self.documents.context.tenant_id, cursor))
            return []

    monkeypatch.setattr(events, "EventLog", Log)
    db = SimpleNamespace(settings=SimpleNamespace(pool_size=2))
    hub = EventHub(db)
    first = SimpleNamespace(context=SimpleNamespace(tenant_id="tenant-1"))
    second = SimpleNamespace(context=SimpleNamespace(tenant_id="tenant-2"))
    hub.subscribe(first)
    hub.subscribe(second)
    try:
        hub.poll({"tenant-1"})
        assert reads == [("tenant-1", 0)]
        reads.clear()
        hub.poll()
        assert {tenant for tenant, _ in reads} == {"tenant-1", "tenant-2"}
    finally:
        hub.close()
