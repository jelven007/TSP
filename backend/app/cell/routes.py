from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, model_validator

from app.cell.alerts import Alerts
from app.cell.automation import Automations
from app.cell.context import AccessError
from app.cell.main import SYMBOL_PATTERN, Credential, Epoch
from app.cell.notifications import Notifications
from app.cell.paper import Paper
from app.cell.strategies import Strategies


class StrategyInput(BaseModel):
    template_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    overrides: dict = Field(default_factory=dict)
    version: int | None = None


class RunInput(BaseModel):
    as_of: date
    idempotency_key: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def valid_date(self):
        if self.as_of > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("future_strategy_date")
        return self


class RuleInput(BaseModel):
    id: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    symbols: list[Annotated[str, Field(pattern=SYMBOL_PATTERN)]] = Field(min_length=1, max_length=1000)
    field: Literal["price", "change_pct"]
    op: Literal[">", ">=", "<", "<="]
    value: float = Field(allow_inf_nan=False)
    enabled: bool = True
    cooldown_seconds: int = Field(default=300, ge=1, le=86400)
    severity: Literal["info", "warn", "critical"] = "info"


class AccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    initial_cash: float = Field(gt=0, le=1e10, allow_inf_nan=False)
    commission_pct: float = Field(default=0.00025, ge=0, le=0.1, allow_inf_nan=False)
    stamp_tax_pct: float = Field(default=0.001, ge=0, le=0.1, allow_inf_nan=False)
    slippage_bps: float = Field(default=5, ge=0, le=1000, allow_inf_nan=False)


class OrderInput(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0, le=10000000)
    idempotency_key: str = Field(min_length=1, max_length=80)


class ChannelInput(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    version: int | None = None
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["email", "webhook"]
    destination: str = Field(min_length=3, max_length=2000)
    enabled: bool = False


class AutomationInput(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    version: int | None = None
    name: str = Field(min_length=1, max_length=80)
    strategy_id: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    run_time: str = Field(default="09:35", pattern=r"^(?:09:(?:3[0-9]|[45][0-9])|1[0-4]:[0-5][0-9])$")
    max_positions: int = Field(default=5, ge=1, le=20)
    budget_per_order: float = Field(default=10000, gt=0, le=1e9, allow_inf_nan=False)
    stop_loss_pct: float = Field(default=0.05, gt=0, lt=1, allow_inf_nan=False)
    take_profit_pct: float = Field(default=0.1, gt=0, lt=10, allow_inf_nan=False)
    max_daily_orders: int = Field(default=10, ge=1, le=100)


def router_for(db, identity, market) -> APIRouter:
    router = APIRouter(prefix="/api/tenants/{tenant_id}")
    strategies, alerts, paper = Strategies(db), Alerts(db), Paper(db)
    notifications, automations = Notifications(db), Automations(db, market)

    def access(request, auth, tenant_id, epoch, permission):
        return identity.authorized(auth, tenant_id, epoch, request.state.request_id, permission)

    @router.get("/strategy-templates")
    def templates(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:read") as (_, conn):
            return {"templates": strategies.catalog(conn)}

    @router.get("/strategies")
    def list_strategies(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:read") as (ctx, _):
            return {"strategies": strategies.list(ctx)}

    @router.post("/strategies", status_code=201)
    def create_strategy(tenant_id: str, body: StrategyInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:write") as (ctx, conn):
            return {"strategy": strategies.save(ctx, strategies.catalog(conn), body.template_id, body.name, body.overrides)}

    @router.put("/strategies/{strategy_id}")
    def update_strategy(tenant_id: str, strategy_id: str, body: StrategyInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:write") as (ctx, conn):
            return {"strategy": strategies.save(ctx, strategies.catalog(conn), body.template_id, body.name, body.overrides, strategy_id, body.version)}

    @router.delete("/strategies/{strategy_id}", status_code=204)
    def delete_strategy(tenant_id: str, strategy_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:write") as (ctx, _):
            strategies.delete(ctx, strategy_id)

    @router.post("/strategies/{strategy_id}/run", status_code=202)
    def run_strategy(tenant_id: str, strategy_id: str, body: RunInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:write") as (ctx, control):
            return {"job": strategies.enqueue(ctx, strategy_id, body.as_of, body.idempotency_key, control)}

    @router.get("/strategy-jobs")
    def jobs(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:read") as (ctx, _):
            return {"jobs": strategies.jobs(ctx)}

    @router.post("/strategy-jobs/{job_id}/cancel", status_code=204)
    def cancel_job(tenant_id: str, job_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "strategies:write") as (ctx, _):
            strategies.cancel(ctx, job_id)

    @router.get("/monitor-rules")
    def rules(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:read") as (ctx, _):
            return {"rules": alerts.rules(ctx)}

    @router.post("/monitor-rules")
    def save_rule(tenant_id: str, body: RuleInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:write") as (ctx, _):
            return {"rule": alerts.save_rule(ctx, body.model_dump())}

    @router.delete("/monitor-rules/{rule_id}", status_code=204)
    def delete_rule(tenant_id: str, rule_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:write") as (ctx, _):
            alerts.delete_rule(ctx, rule_id)

    @router.get("/alerts")
    def inbox(tenant_id: str, epoch: Epoch, auth: Credential, request: Request, limit: int = Query(100, ge=1, le=1000)):
        with access(request, auth, tenant_id, epoch, "alerts:read") as (ctx, _):
            return {"alerts": alerts.inbox(ctx, limit)}

    @router.post("/alerts/{alert_id}/read", status_code=204)
    def mark_read(tenant_id: str, alert_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:write") as (ctx, _):
            alerts.mark_read(ctx, alert_id)

    @router.get("/paper/accounts")
    def accounts(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:read") as (ctx, _):
            return {"accounts": paper.accounts(ctx)}

    @router.post("/paper/accounts", status_code=201)
    def create_account(tenant_id: str, body: AccountInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:write") as (ctx, _):
            return {"account": paper.create(ctx, body.model_dump())}

    @router.get("/paper/accounts/{account_id}")
    def overview(tenant_id: str, account_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:read") as (ctx, _):
            return paper.overview(ctx, account_id)

    @router.post("/paper/accounts/{account_id}/freeze", status_code=204)
    def freeze(tenant_id: str, account_id: str, epoch: Epoch, auth: Credential, request: Request, frozen: bool = True):
        with access(request, auth, tenant_id, epoch, "paper:write") as (ctx, _):
            paper.freeze(ctx, account_id, frozen)

    @router.post("/paper/accounts/{account_id}/orders", status_code=201)
    def order(tenant_id: str, account_id: str, body: OrderInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:write") as (ctx, _):
            # Establish resource ownership before fetching the public quote.
            existing = paper.existing_order(ctx, account_id, body.model_dump())
            if existing:
                return {"order": existing}
            quote = market.quotes([body.symbol]).get(body.symbol)
            if quote is None:
                raise AccessError(409, "quote_not_available")
            return {"order": paper.order(ctx, account_id, body.model_dump(), quote)}

    @router.delete("/paper/accounts/{account_id}/orders/{order_id}", status_code=204)
    def cancel_order(tenant_id: str, account_id: str, order_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:write") as (ctx, _):
            paper.cancel(ctx, account_id, order_id)

    @router.get("/notification-channels")
    def channels(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:read") as (ctx, _):
            return {"channels": notifications.channels(ctx)}

    @router.post("/notification-channels")
    def save_channel(tenant_id: str, body: ChannelInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:write") as (ctx, _):
            return {"channel": notifications.save(ctx, body.model_dump())}

    @router.get("/notification-deliveries")
    def deliveries(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:read") as (ctx, _):
            return {"deliveries": notifications.deliveries(ctx)}

    @router.post("/notification-deliveries/{delivery_id}/retry", status_code=204)
    def retry_delivery(tenant_id: str, delivery_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "alerts:write") as (ctx, _):
            notifications.retry(ctx, delivery_id)

    @router.get("/trade-automations")
    def list_automations(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:read") as (ctx, _):
            return {"automations": automations.list(ctx)}

    @router.post("/trade-automations")
    def save_automation(tenant_id: str, body: AutomationInput, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:write") as (ctx, _):
            return {"automation": automations.save(ctx, body.model_dump())}

    @router.get("/trade-intents")
    def trade_intents(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with access(request, auth, tenant_id, epoch, "paper:read") as (ctx, _):
            return {"intents": automations.intents(ctx)}

    return router
