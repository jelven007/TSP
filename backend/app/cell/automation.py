"""Daily built-in selection -> durable intents -> next-quote paper orders and risk exits."""

import hashlib
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from app.cell.context import SYMBOL_PATTERN, AccessError
from app.cell.identity import Identity
from app.cell.paper import CN_TZ, Paper, positions
from app.cell.strategies import Strategies
from app.cell.watchlist import Watchlist
from app.strategy.paper_math import apply_slippage, buy_fee


class Automations:
    def __init__(self, db, market):
        self.db, self.market = db, market
        self.identity = Identity(db)

    def list(self, ctx):
        with self.db.business_transaction(ctx, permission="paper:read") as conn:
            return conn.execute("SELECT * FROM business.trade_automations ORDER BY id").fetchall()

    def intents(self, ctx):
        with self.db.business_transaction(ctx, permission="paper:read") as conn:
            return conn.execute(
                "SELECT * FROM business.trade_intents ORDER BY signal_at DESC LIMIT 100"
            ).fetchall()

    def save(self, ctx, body):
        ctx.require("strategies:write")
        if ctx.token_id:
            raise AccessError(403, "automation_session_required")
        with self.db.business_transaction(ctx, write=True, permission="paper:write") as conn:
            Paper._account(conn, body["account_id"], True)
            if not conn.execute("SELECT id FROM business.strategies WHERE id=%s", (body["strategy_id"],)).fetchone():
                raise AccessError(404, "strategy_not_found")
            automation_id = body.get("id") or uuid.uuid4().hex
            current = conn.execute("SELECT version FROM business.trade_automations WHERE id=%s", (automation_id,)).fetchone()
            if body.get("id") and not current:
                raise AccessError(404, "automation_not_found")
            if current and body.get("version") != current["version"]:
                raise AccessError(409, "automation_version_stale")
            conflict = conn.execute(
                "SELECT id FROM business.trade_automations WHERE account_id=%s AND id!=%s",
                (body["account_id"], automation_id),
            ).fetchone()
            if conflict:
                raise AccessError(409, "account_has_automation")
            if not current and conn.execute("SELECT count(*) AS n FROM business.trade_automations").fetchone()["n"] >= 20:
                raise AccessError(429, "automation_limit")
            row = conn.execute(
                """INSERT INTO business.trade_automations
                   (tenant_id,id,name,strategy_id,account_id,enabled,run_time,max_positions,budget_per_order,
                    stop_loss_pct,take_profit_pct,max_daily_orders,user_id,policy_version,routing_epoch)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(tenant_id,id) DO UPDATE SET name=excluded.name,strategy_id=excluded.strategy_id,
                   account_id=excluded.account_id,enabled=excluded.enabled,run_time=excluded.run_time,
                   max_positions=excluded.max_positions,budget_per_order=excluded.budget_per_order,
                   stop_loss_pct=excluded.stop_loss_pct,take_profit_pct=excluded.take_profit_pct,
                   max_daily_orders=excluded.max_daily_orders,user_id=excluded.user_id,
                   policy_version=excluded.policy_version,routing_epoch=excluded.routing_epoch,
                   version=business.trade_automations.version+1,error=NULL RETURNING *""",
                (ctx.tenant_id, automation_id, body["name"], body["strategy_id"], body["account_id"], body["enabled"],
                 body["run_time"], body["max_positions"], body["budget_per_order"], body["stop_loss_pct"],
                 body["take_profit_pct"], body["max_daily_orders"], ctx.user_id, ctx.policy_version, ctx.routing_epoch),
            ).fetchone()
            self.cancel_pending(conn, automation_id, "automation_changed")
            Watchlist._audit(conn, ctx, "automation.save", automation_id)
            return row

    @staticmethod
    def cancel_pending(conn, automation_id, reason):
        conn.execute(
            "UPDATE business.trade_intents SET status='cancelled',reason=%s WHERE automation_id=%s AND status='queued'",
            (reason, automation_id),
        )
        conn.execute(
            """UPDATE business.paper_orders SET status='cancelled',reserved_cash=0,reason=%s
               WHERE automation_id=%s AND status='queued'""", (reason, automation_id),
        )

    @staticmethod
    def authorized_rules(conn, control, ctx):
        rules = conn.execute("SELECT * FROM business.trade_automations WHERE enabled ORDER BY id FOR UPDATE").fetchall()
        valid = []
        for row in rules:
            try:
                Identity.authorize_background(control, ctx, row, {"paper:write", "strategies:write"})
                valid.append(row)
            except AccessError:
                conn.execute(
                    "UPDATE business.trade_automations SET enabled=false,error='job_authorization_changed' WHERE id=%s",
                    (row["id"],),
                )
                Automations.cancel_pending(conn, row["id"], "job_authorization_changed")
        return valid

    @staticmethod
    def capture_result(conn, ctx, job, result):
        if not job.get("automation_id"):
            return
        row = conn.execute(
            "SELECT * FROM business.trade_automations WHERE id=%s", (job["automation_id"],)
        ).fetchone()
        if not row or not row["enabled"] or row["version"] != job["automation_version"]:
            return
        now = datetime.now(UTC)
        for candidate in result.get("rows", [])[:row["max_positions"]]:
            symbol = candidate.get("symbol", "")
            if not re.fullmatch(SYMBOL_PATTERN, symbol) or symbol not in job["input"]["symbols"]:
                continue
            intent_id = hashlib.sha256(f"{job['id']}:{symbol}".encode()).hexdigest()
            conn.execute(
                """INSERT INTO business.trade_intents(tenant_id,id,automation_id,automation_version,
                   job_id,symbol,side,expires_at) VALUES (%s,%s,%s,%s,%s,%s,'buy',%s) ON CONFLICT DO NOTHING""",
                (ctx.tenant_id, intent_id, row["id"], row["version"], job["id"], symbol, now + timedelta(hours=24)),
            )

    def process(self, tenant_id, *, now=None):
        now = now or datetime.now(UTC)
        local = now.astimezone(CN_TZ)
        with self.identity.worker_context(tenant_id) as (ctx, _), self.db.business_transaction(
            ctx, permission="paper:write"
        ) as conn:
            sample = conn.execute("SELECT symbol FROM business.watchlist_items ORDER BY symbol LIMIT 1").fetchone()
        daily_quotes = self.market.quotes([sample["symbol"]]) if sample else {}
        with self.identity.worker_context(tenant_id) as (ctx, control), self.db.business_transaction(
            ctx, write=True, permission="paper:write"
        ) as conn:
            rules = self.authorized_rules(conn, control, ctx)
            for row in rules:
                # Never infer a trading day from weekdays: a fresh platform quote is required.
                if row["last_run_date"] == local.date() or local.time().replace(tzinfo=None) < row["run_time"]:
                    continue
                if not time(9, 30) <= local.time().replace(tzinfo=None) < time(15, 0):
                    continue
                if not sample:
                    continue
                quote = daily_quotes.get(sample["symbol"])
                if not quote or not quote.fresh():
                    continue
                if quote.source_time.astimezone(CN_TZ).date() != local.date():
                    continue
                # Daily signals use yesterday's observation cutoff; gateway excludes future bars.
                cutoff = local.date() - timedelta(days=1)
                try:
                    with conn.transaction():
                        job = Strategies(self.db).enqueue(
                            replace(ctx, user_id=row["user_id"], token_id=row["token_id"]),
                            row["strategy_id"], cutoff, f"auto:{row['id']}:{local.date()}", control,
                            connection=conn, automation=row,
                        )
                    conn.execute(
                        "UPDATE business.trade_automations SET last_run_date=%s,last_job_id=%s,error=NULL WHERE id=%s",
                        (local.date(), job["id"], row["id"]),
                    )
                except AccessError as exc:
                    conn.execute("UPDATE business.trade_automations SET error=%s WHERE id=%s", (exc.code, row["id"]))
            intents = conn.execute("SELECT symbol FROM business.trade_intents WHERE status='queued' LIMIT 1000").fetchall()
        symbols = sorted({row["symbol"] for row in intents})
        quotes = self.market.quotes(symbols) if symbols else {}
        with self.identity.worker_context(tenant_id) as (ctx, control), self.db.business_transaction(
            ctx, write=True, permission="paper:write"
        ) as conn:
            rules = {row["id"]: row for row in self.authorized_rules(conn, control, ctx)}
            intents = conn.execute(
                "SELECT * FROM business.trade_intents WHERE status='queued' ORDER BY signal_at,id LIMIT 1000 FOR UPDATE"
            ).fetchall()
            for intent in intents:
                row = rules.get(intent["automation_id"])
                reason, order = None, None
                if not row or row["version"] != intent["automation_version"]:
                    reason = "automation_changed"
                elif intent["expires_at"] <= now:
                    reason = "automation_signal_expired"
                else:
                    quote = quotes.get(intent["symbol"])
                    if not quote or not quote.tradable() or quote.source_time <= intent["signal_at"]:
                        continue
                    if intent["side"] == "buy":
                        account = Paper._account(conn, row["account_id"], True)
                        holdings = positions(Paper._fills(conn, account["id"]), local.date())
                        pending = conn.execute(
                            "SELECT symbol FROM business.paper_orders WHERE account_id=%s AND status='queued' AND side='buy'",
                            (account["id"],),
                        ).fetchall()
                        occupied = {s for s, p in holdings.items() if p["qty"] > 0} | {p["symbol"] for p in pending}
                        if intent["symbol"] in occupied:
                            reason = "automation_already_held"
                        elif len(occupied) >= row["max_positions"]:
                            reason = "automation_position_limit"
                        price = apply_slippage(quote.price, "buy", float(account["slippage_bps"]))
                        qty = min(10000000, int(float(row["budget_per_order"]) / price / 100) * 100)
                        while qty > 0 and Decimal(str(qty * price + buy_fee(qty, price, float(account["commission_pct"])))) > row["budget_per_order"]:
                            qty -= 100
                    else:
                        qty = positions(Paper._fills(conn, row["account_id"]), local.date()).get(
                            intent["symbol"], {"available_qty": 0}
                        )["available_qty"] // 100 * 100
                    daily = conn.execute(
                        """SELECT count(*) AS n FROM business.paper_orders WHERE account_id=%s
                           AND automation_id=%s AND (created_at AT TIME ZONE 'Asia/Shanghai')::date=%s""",
                        (row["account_id"], row["id"], local.date()),
                    ).fetchone()["n"]
                    if daily >= row["max_daily_orders"]:
                        reason = "automation_daily_limit"
                    if not qty:
                        reason = "automation_quantity_unavailable"
                    if not reason:
                        try:
                            with conn.transaction():
                                order = Paper.submit(conn, ctx, row["account_id"], {
                                    "symbol": intent["symbol"], "side": intent["side"], "qty": qty,
                                    "idempotency_key": "intent:" + intent["id"],
                                }, quote, row)
                        except AccessError as exc:
                            reason = exc.code
                conn.execute(
                    "UPDATE business.trade_intents SET status=%s,reason=%s,order_id=%s WHERE id=%s",
                    ("submitted" if order else "skipped", reason, order["id"] if order else None, intent["id"]),
                )
        return len(intents)

    @staticmethod
    def risk_exits(conn, ctx, rules, quotes):
        today = datetime.now(CN_TZ).date()
        for row in rules:
            account = Paper._account(conn, row["account_id"], True)
            for symbol, pos in positions(Paper._fills(conn, account["id"]), today).items():
                quote = quotes.get(symbol)
                if not quote or not quote.tradable() or pos["available_qty"] < 100 or pos["qty"] <= 0:
                    continue
                change = Decimal(str(quote.price)) / (pos["cost"] / pos["qty"]) - 1
                if -row["stop_loss_pct"] < change < row["take_profit_pct"]:
                    continue
                intent_id = hashlib.sha256(f"risk:{row['id']}:{row['version']}:{symbol}:{today}".encode()).hexdigest()
                conn.execute(
                    """INSERT INTO business.trade_intents(tenant_id,id,automation_id,automation_version,symbol,side,expires_at)
                       VALUES (%s,%s,%s,%s,%s,'sell',%s) ON CONFLICT DO NOTHING""",
                    (ctx.tenant_id, intent_id, row["id"], row["version"], symbol, datetime.now(UTC) + timedelta(hours=8)),
                )
