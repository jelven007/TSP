"""Full-product API resources without platform collectors or legacy private state."""
from __future__ import annotations

import logging
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.cell.config import CellSettings
from app.cell.database import Databases
from app.config import settings
from app.multitenancy.identity import AccountIdentity

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.service_role != "api":
        raise RuntimeError("multitenant full-product service requires SERVICE_ROLE=api")
    db = Databases(CellSettings())
    db.open()
    with ExitStack() as resources:
        resources.callback(db.close)
        account_identity = AccountIdentity(db)
        # Admin initialization and legacy import are offline operations. API
        # startup must never reset a password or grant itself DDL credentials.
        app.state.account_identity = account_identity
        from app.multitenancy.stream_auth import StreamTicketCodec

        app.state.stream_tickets = StreamTicketCodec(db.settings.stream_ticket_secret)
        account_identity.recover_pending()

        from app.data_providers import custom as custom_sources
        from app.multitenancy.events import EventHub
        from app.multitenancy.market import PublicMarket
        from app.multitenancy.quote_bridge import PublicQuoteBridge
        from app.multitenancy.quote_markers import QuoteMarkerBridge
        from app.tickflow.policy import detect_capabilities
        from app.tickflow.repository import DataStore, KlineRepository

        custom_sources.load_all()
        resources.callback(custom_sources.close_all)
        store = DataStore(settings.data_dir.resolve(), read_only=True)
        resources.callback(store.db.close)
        repo = KlineRepository(store)
        app.state.datastore = store
        app.state.repo = repo
        app.state.indicators_ready = False
        repo._refresh_instruments()
        repo._refresh_index_instruments()
        repo._refresh_etf_instruments()
        app.state.capabilities = detect_capabilities()
        # This instance supplies the existing public read contracts only.
        # boot_check/start_polling/schedulers belong to the platform process.
        quotes = PublicQuoteBridge(repo, PublicMarket(db.settings), app.state)
        quotes.attach_markers(QuoteMarkerBridge(
            db.settings.market_url,
            db.settings.market_token.get_secret_value(),
            on_event=quotes.mark_event,
        ))
        app.state.quote_service = quotes
        app.state.depth_service = quotes.depth
        app.state.scheduler = None
        resources.callback(quotes.close)
        quotes.refresh()
        hub = EventHub(db)
        app.state.tenant_event_hub = hub
        hub.start()
        resources.callback(hub.close)
        from app.services.sector_monitor import SectorMonitorService

        app.state.sector_monitor_service = SectorMonitorService(repo)
        quotes.start()

        from app.strategy.engine import StrategyEngine
        app.state.strategy_engine = StrategyEngine(
            strategy_dirs=[Path(__file__).resolve().parents[1] / "strategy" / "builtin"],
        )
        from app.multitenancy.executor import ComputeExecutor

        app.state.compute_executor = ComputeExecutor()
        from app.multitenancy.platform import PlatformAdmin, market_read_path

        admin = PlatformAdmin(db.settings.market_url)
        reads = PlatformAdmin(db.settings.market_url, db.settings.market_token.get_secret_value(), market_read_path)
        app.state.platform_admin = admin
        app.state.platform_reads = reads
        app.state.service_role = "api"
        logger.info("full-product API ready; identity and business schema v5")
        try:
            yield
        finally:
            await admin.close()
            await reads.close()
