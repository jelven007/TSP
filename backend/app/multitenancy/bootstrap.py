"""Minimal full-product startup information; platform credentials stay private."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import __version__
from app.cell.context import AccessError
from app.multitenancy.preferences import EDITABLE, UserPreferences
from app.multitenancy.routes import require

router = APIRouter(prefix="/api/business", tags=["business-bootstrap"])
BUSINESS_MENUS = (
    "/", "/watchlist", "/screener", "/factors", "/review", "/backtest", "/signals",
    "/lots", "/abnormal", "/limit-ladder", "/stock-analysis", "/concept-analysis",
    "/industry-analysis", "/financials", "/monitor", "/regime", "/paper", "/indices",
)


def preferences(request: Request) -> UserPreferences:
    return UserPreferences(request.app.state.account_identity.db)


def _settings(account: dict) -> dict:
    from app.services import ai_provider
    from app.services import preferences as platform

    return {
        "onboarding_completed": True if account["role"] == "tenant" else platform.get_onboarding_completed(),
        "ai_configured": ai_provider.ai_configured(),
        "ai_round_checkpoint": ai_provider.current_ai_round_checkpoint(),
    }


def _preferences(request: Request, account: dict) -> dict:
    from app.api.settings import _minute_history_days, _realtime_allowed, get_preferences
    from app.multitenancy.documents import private_storage
    from app.services import preferences as platform

    documents = private_storage(request) if getattr(request.state, "tenant_context", None) else None
    notification_preferences = documents.get("notification_preferences", "default", {}) if documents else {}
    review_schedule = documents.get("review_schedule", "default", {}) if documents else {}
    personal = preferences(request).load(account["id"])
    if account["role"] == "admin":
        personal = {key: personal[key] for key in EDITABLE}
    return {
        **(get_preferences(request) if account["role"] == "admin" else {}),
        **personal,
        "nav_order": platform.get_nav_order(),
        "nav_hidden": platform.get_nav_hidden(),
        "realtime_quotes_enabled": platform.get_realtime_quotes_enabled(),
        "realtime_allowed": _realtime_allowed(),
        "minute_history_days": _minute_history_days(),
        "minute_refresh_interval": platform.get_minute_refresh_interval(),
        "minute_intraday_refresh": platform.get_minute_intraday_refresh(),
        "minute_intraday_refresh_interval": platform.get_minute_intraday_refresh_interval(),
        "review_schedule": {key: review_schedule.get(key, default) for key, default in
                            (("enabled", False), ("hour", 15), ("minute", 40))},
        "review_push_channels": notification_preferences.get("review_channels", []),
        "review_push_mode": notification_preferences.get("review_push_mode", "manual"),
        "webhook_default_channels": notification_preferences.get("default_channels", []),
    }


@router.get("/bootstrap")
def bootstrap(request: Request) -> dict:
    account = require(request, "business:read")
    return {
        "user": account,
        "menus": [*BUSINESS_MENUS, *(["/data"] if "data:manage" in account["permissions"] else [])],
        "settings_tabs": (
            ["account", "data-sources", "ai", "monitoring", "ext-pages", "timeout", "menus", "api-tokens", "system"]
            if "settings:manage" in account["permissions"] else ["account"]
        ),
        "preferences": _preferences(request, account),
        "business": _settings(account),
        "version": __version__,
    }


@router.get("/settings")
def business_settings(request: Request) -> dict:
    return _settings(require(request, "business:read"))


@router.get("/preferences")
def business_preferences(request: Request) -> dict:
    return _preferences(request, require(request, "business:read"))


@router.get("/capabilities")
def business_capabilities(request: Request) -> dict:
    require(request, "business:read")
    from app.api.settings import get_capability_matrix

    matrix = get_capability_matrix()
    return {
        "capabilities": [
            {key: row[key] for key in ("id", "label", "desc", "usable", "effective", "effective_display")}
            for row in matrix["capabilities"]
        ],
    }


@router.get("/version")
def version(request: Request) -> dict:
    require(request, "business:read")
    return {"version": __version__}


@router.get("/data-availability")
def data_availability(request: Request) -> dict:
    require(request, "business:read")
    from app.api import data

    repo = request.app.state.repo
    def table_stats(name, fetch):
        return data._get_table_stats(name, fetch, data_dir=repo.store.data_dir)

    response = {
        "enriched": table_stats("enriched", lambda: data._safe_aggregate_enriched(repo)),
        "etf_enriched": table_stats("etf_enriched", lambda: data._safe_aggregate_etf_enriched(repo)),
        "indicators_ready": getattr(request.app.state, "indicators_ready", False),
    }
    from app.services.data_quality import quality_summary

    response["quality"] = table_stats("quality", lambda: quality_summary(
        repo.store.data_dir, {
            **response,
            "daily": table_stats("daily", lambda: data._safe_aggregate_daily(repo)),
            "instruments": table_stats(
                "instruments", lambda: data._safe_aggregate_instruments(repo),
            ),
            "financials": table_stats(
                "financials", lambda: data._safe_aggregate_financials(repo),
            ),
        },
    ))
    return response


class LayoutIn(BaseModel):
    layout: dict | None = None


class ColumnsIn(BaseModel):
    columns: list[dict]


@router.put("/preferences/dashboard-layout")
def layout(body: LayoutIn, request: Request) -> dict:
    account = require(request, "business:write")
    return preferences(request).save_display(account["id"], "dashboard_layout", body.layout)


@router.get("/preferences/{name}")
def columns(name: str, request: Request) -> dict:
    account = require(request, "business:read")
    key = name.replace("-", "_")
    if key not in {"watchlist_columns", "screener_result_columns"}:
        raise AccessError(404, "配置不存在")
    return {"columns": preferences(request).load(account["id"])[key]}


@router.put("/preferences/{name}")
def save_columns(name: str, body: ColumnsIn, request: Request) -> dict:
    account = require(request, "business:write")
    key = name.replace("-", "_")
    if key not in {"watchlist_columns", "screener_result_columns"}:
        raise AccessError(403, "无权修改该设置")
    saved = preferences(request).save_display(account["id"], key, body.columns)
    return {"columns": saved[key]}
