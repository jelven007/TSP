"""Minimal realtime gateway resources for tenant event streams."""
from __future__ import annotations

import logging
from contextlib import ExitStack, asynccontextmanager

from fastapi import FastAPI

from app.cell.config import CellSettings
from app.cell.database import Databases
from app.config import settings
from app.multitenancy.events import EventHub
from app.multitenancy.identity import AccountIdentity
from app.multitenancy.quote_markers import QuoteMarkerBridge
from app.multitenancy.stream_auth import RevocationListener, StreamTicketCodec

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.service_role != "gateway":
        raise RuntimeError("realtime gateway requires SERVICE_ROLE=gateway")
    db = Databases(CellSettings())
    db.open()
    with ExitStack() as resources:
        resources.callback(db.close)
        app.state.service_role = "gateway"
        app.state.account_identity = AccountIdentity(db)
        app.state.stream_tickets = StreamTicketCodec(db.settings.stream_ticket_secret)

        revocations = RevocationListener(db.settings.control_dsn.get_secret_value())
        app.state.stream_revocations = revocations
        revocations.start()
        resources.callback(revocations.close)

        events = EventHub(db)
        app.state.tenant_event_hub = events
        events.start()
        resources.callback(events.close)

        quotes = QuoteMarkerBridge(
            db.settings.market_url,
            db.settings.market_token.get_secret_value(),
        )
        app.state.quote_service = quotes
        quotes.start()
        resources.callback(quotes.close)

        app.state.scheduler = None
        logger.info("tenant realtime gateway ready")
        yield


def readiness(state, _data_dir) -> dict:
    checks = {}
    identity = getattr(state, "account_identity", None)
    try:
        with identity.db.control.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        with identity.db.data.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["databases"] = {"status": "ok"}
    except Exception:
        checks["databases"] = {"status": "error"}

    event_status = state.tenant_event_hub.status()
    checks["private_events"] = {
        "status": "ok" if event_status["listener_connected"] else "error",
        **event_status,
    }
    revocation_status = state.stream_revocations.status()
    checks["revocations"] = {
        "status": "ok" if revocation_status["connected"] else "error",
        **revocation_status,
    }
    marker_status = state.quote_service.status()
    checks["public_market_events"] = {
        "status": "ok" if marker_status["connected"] else "error",
        **marker_status,
    }
    ready = all(check["status"] == "ok" for check in checks.values())
    return {"status": "ok" if ready else "error", "ready": ready, "checks": checks}
