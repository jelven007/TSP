"""Platform-only collectors and authenticated forwarding of administration."""
from __future__ import annotations

import os
import secrets
from contextlib import ExitStack, asynccontextmanager

import httpx
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

READ_METHODS = frozenset({"GET", "HEAD"})
QUERY_POSTS = frozenset({
    "/api/kline/instruments/names", "/api/kline/daily-batch", "/api/kline/minute-batch",
})
PUBLIC_MARKET_GETS = frozenset({
    "/api/kline/instruments/search", "/api/kline/daily", "/api/kline/daily/latest",
    "/api/kline/minute", "/api/kline/minute-range", "/api/index/daily", "/api/index/minute",
    "/api/intraday/status", "/api/intraday/indices", "/api/overview/market",
    "/api/abnormal/auction", "/api/abnormal/intraday", "/api/abnormal/overview",
    "/api/screener/market-snapshot", "/api/screener/limit-ladder",
    "/api/regime/history", "/api/regime/latest", "/api/regime/states", "/api/regime/coverage",
    "/api/regime/phases", "/api/regime/mainline", "/api/sector-rotation",
})

def market_read_path(method: str, path: str) -> bool:
    return (
        (method in READ_METHODS and path in PUBLIC_MARKET_GETS)
        or (method == "POST" and path in QUERY_POSTS)
    )


def market_read_access(request) -> bool:
    expected = os.environ.get("TSP_PLATFORM_MARKET_TOKEN", "")
    return bool(expected and secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected)
                and market_read_path(request.method, request.url.path))


def management_path(method: str, path: str) -> bool:
    """Same allowlist at both ends; identity and tenant tokens always stay local."""
    if path == "/api/settings/api-tokens" or path.startswith("/api/settings/api-tokens/"):
        return False
    if any(path == prefix or path.startswith(prefix + "/") for prefix in (
        "/api/settings", "/api/data", "/api/pipeline",
    )):
        return True
    if path == "/api/capabilities/redetect" or path.startswith("/api/financials/sync/"):
        return True
    if path.startswith("/api/ext-data/") and path.endswith("/api-key"):
        return True
    if method in READ_METHODS:
        return path == "/api/ext-data"
    return (
        (path.startswith("/api/kline/") and path not in QUERY_POSTS)
        or path.startswith("/api/index/sync")
        or path in {"/api/regime/recompute", "/api/regime/mainline/recompute"}
        or path == "/api/ext-data" or path.startswith("/api/ext-data/")
        or path.startswith("/api/analysis-menus/")
    )


def platform_access(request) -> bool:
    expected = os.environ.get("TSP_PLATFORM_ADMIN_TOKEN", "")
    provided = request.headers.get("authorization", "")
    return bool(expected and secrets.compare_digest(provided, "Bearer " + expected)
                and management_path(request.method, request.url.path))


class PlatformAdmin:
    def __init__(self, url: str, token=None, allowed=management_path):
        token = token or os.environ.get("TSP_PLATFORM_ADMIN_TOKEN", "")
        if not url or not token:
            raise RuntimeError("platform administration gateway is not configured")
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/"), headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(300, connect=5), trust_env=False, follow_redirects=False,
        )
        self.allowed = allowed

    async def close(self):
        await self.client.aclose()

    async def forward(self, request):
        if not self.allowed(request.method, request.url.path):
            return JSONResponse({"detail": "该操作不属于平台管理"}, status_code=403)
        # Never forward the browser cookie, tenant ID or an external API token.
        headers = {key: request.headers[key] for key in ("content-type", "accept") if key in request.headers}
        if self.allowed is management_path:
            headers["x-tsp-admin-email"] = request.state.account.get("email") or ""
        try:
            response = await self.client.send(self.client.build_request(
                request.method, request.url.path, params=request.query_params,
                content=None if request.method in READ_METHODS else request.stream(),
                headers=headers,
            ), stream=True)
        except httpx.HTTPError:
            return JSONResponse({"detail": "平台管理服务暂不可用"}, status_code=503)
        kept = {key: value for key, value in response.headers.items()
                if key in {"content-type", "content-disposition", "x-request-id"}}
        return StreamingResponse(
            response.aiter_bytes(), status_code=response.status_code, headers=kept,
            background=BackgroundTask(response.aclose),
        )


@asynccontextmanager
async def lifespan(app):
    """No legacy account, private factor, strategy, monitor, paper or AI jobs."""
    from app.data_providers import custom
    from app.jobs import daily_pipeline
    from app.multitenancy.publication import PublicPublisher
    from app.services.depth_service import DepthService
    from app.services.ext_pull import pull_scheduler
    from app.services.financial_sync import financial_scheduler
    from app.services.quote_service import QuoteService
    from app.services.sector_monitor import SectorMonitorService
    from app.tickflow.policy import detect_capabilities
    from app.tickflow.repository import DataStore, KlineRepository

    with ExitStack() as resources:
        store = DataStore()
        resources.callback(store.db.close)
        repo = KlineRepository(store)
        app.state.datastore, app.state.repo = store, repo
        from app.services.initial_sync import prepare_initial_sync

        prepare_initial_sync(store.data_dir)
        app.state.service_role = "platform"
        custom.load_all()
        resources.callback(custom.close_all)
        app.state.capabilities = detect_capabilities()
        # Create all public generation markers before accepting shared readers.
        for asset in ("stock", "etf", "index"):
            repo.get_matrix_data_generation(asset)
        from app.services.data_release import ensure_current_release

        app.state.data_release = ensure_current_release(store.data_dir)
        app.state.indicators_ready = False
        repo._on_warmup_done = lambda: setattr(app.state, "indicators_ready", True)
        repo.refresh_cache(background=True)
        quotes = QuoteService()
        quotes.set_repo(repo)
        quotes.set_app_state(app.state)
        app.state.quote_service = quotes
        resources.callback(quotes.stop)
        depth = DepthService()
        depth.set_repo(repo)
        depth.set_app_state(app.state)
        app.state.depth_service = depth
        resources.callback(depth.stop_polling)
        app.state.sector_monitor_service = SectorMonitorService(repo)
        daily_pipeline.set_app_state(app.state)
        app.state.scheduler = daily_pipeline.start_scheduler(repo, app.state.capabilities)
        resources.callback(app.state.scheduler.shutdown, wait=False)

        from app.services.ext_presets import ensure_builtin_presets
        await ensure_builtin_presets(store.data_dir)
        pull_scheduler.start(store.data_dir)
        pull_scheduler.refresh(store.data_dir)
        app.state.pull_scheduler = pull_scheduler
        resources.callback(pull_scheduler.stop)
        financial_scheduler.start(
            store.data_dir, app.state.capabilities, write_lock=repo._write_lock,
        )
        app.state.financial_scheduler = financial_scheduler
        resources.callback(financial_scheduler.stop)

        from app.services.minute_refresh import MinuteRefreshService
        minutes = MinuteRefreshService(repo)
        minutes.set_app_state(app.state)
        app.state.minute_refresh = minutes
        minutes.start()
        resources.callback(minutes.stop)
        publisher = PublicPublisher(store.data_dir, os.environ["TSP_PUBLIC_MARKET_DIR"], repo._write_lock)
        app.state.public_publisher = publisher
        publisher.start()
        resources.callback(publisher.close)
        quotes.boot_check()
        depth.boot_check()
        depth.start_polling()
        from app.services.health import ReadinessMonitor

        monitor = ReadinessMonitor(app.state, store.data_dir)
        app.state.readiness_monitor = monitor
        monitor.start()
        resources.callback(monitor.close)
        from app.services.initial_sync import start_initial_sync

        await start_initial_sync(app.state)
        try:
            yield
        finally:
            from app.api.pipeline import stop_initial_sync_job

            await stop_initial_sync_job(getattr(app.state, "initial_sync_job_id", None))
