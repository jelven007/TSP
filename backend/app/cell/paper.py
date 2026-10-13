import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.cell.alerts import append_alert
from app.cell.context import AccessError, TenantContext
from app.cell.database import Databases
from app.cell.market import MarketQuote
from app.cell.strategies import input_hash
from app.cell.watchlist import Watchlist
from app.strategy.paper_math import apply_slippage, buy_fee, sell_fee

CN_TZ = ZoneInfo("Asia/Shanghai")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def positions(fills: list[dict], today: date) -> dict[str, dict]:
    result = {}
    for fill in fills:
        pos = result.setdefault(fill["symbol"], {"qty": 0, "cost": Decimal(0), "lots": []})
        qty, price, fee = fill["qty"], fill["price"], fill["fee"]
        if fill["side"] == "buy":
            pos["cost"] += price * qty + fee
            pos["qty"] += qty
            pos["lots"].append({"qty": qty, "date": fill["trade_date"]})
        else:
            average = pos["cost"] / pos["qty"] if pos["qty"] else Decimal(0)
            pos["qty"] -= qty
            pos["cost"] -= average * qty
            remain = qty
            for lot in pos["lots"]:
                if lot["date"] >= today:
                    continue
                take = min(lot["qty"], remain)
                lot["qty"] -= take
                remain -= take
                if not remain:
                    break
        pos["available_qty"] = sum(lot["qty"] for lot in pos["lots"] if lot["date"] < today)
    return result


class Paper:
    def __init__(self, db: Databases):
        self.db = db

    @staticmethod
    def _account(conn, account_id: str, lock: bool = False) -> dict:
        row = conn.execute(
            "SELECT * FROM business.paper_accounts WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (account_id,),
        ).fetchone()
        if not row:
            raise AccessError(404, "paper_account_not_found")
        return row

    @staticmethod
    def _fills(conn, account_id: str) -> list[dict]:
        return conn.execute(
            "SELECT * FROM business.paper_fills WHERE account_id=%s ORDER BY created_at,id", (account_id,)
        ).fetchall()

    def accounts(self, ctx: TenantContext) -> list[dict]:
        with self.db.business_transaction(ctx, permission="paper:read") as conn:
            return conn.execute("SELECT * FROM business.paper_accounts ORDER BY created_at,id").fetchall()

    def create(self, ctx: TenantContext, body: dict) -> dict:
        with self.db.business_transaction(ctx, write=True, permission="paper:write") as conn:
            if conn.execute("SELECT count(*) AS n FROM business.paper_accounts").fetchone()["n"] >= 20:
                raise AccessError(429, "paper_account_limit")
            account = conn.execute(
                """INSERT INTO business.paper_accounts
                   (tenant_id,id,name,initial_cash,cash,commission_pct,stamp_tax_pct,slippage_bps)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (ctx.tenant_id, uuid.uuid4().hex, body["name"], body["initial_cash"], body["initial_cash"],
                 body["commission_pct"], body["stamp_tax_pct"], body["slippage_bps"]),
            ).fetchone()
            Watchlist._audit(conn, ctx, "paper.account.create", account["id"])
            return account

    def freeze(self, ctx: TenantContext, account_id: str, frozen: bool) -> None:
        with self.db.business_transaction(ctx, write=True, permission="paper:write") as conn:
            self._account(conn, account_id, True)
            conn.execute("UPDATE business.paper_accounts SET status=%s WHERE id=%s", ("frozen" if frozen else "active", account_id))
            Watchlist._audit(conn, ctx, "paper.account.freeze", account_id)

    def overview(self, ctx: TenantContext, account_id: str, quotes: dict[str, MarketQuote] | None = None) -> dict:
        with self.db.business_transaction(ctx, permission="paper:read") as conn:
            account = self._account(conn, account_id)
            fills = self._fills(conn, account_id)
            pos = positions(fills, datetime.now(CN_TZ).date())
            holdings = []
            for symbol, position in pos.items():
                if position["qty"] <= 0:
                    continue
                quote = (quotes or {}).get(symbol)
                holdings.append({
                    "symbol": symbol, "qty": position["qty"], "available_qty": position["available_qty"],
                    "avg_cost": round(position["cost"] / position["qty"], 6),
                    "price": quote.price if quote else None,
                    "market_value": money(quote.price * position["qty"]) if quote else None,
                })
            return {
                "account": account, "holdings": holdings, "fills": fills,
                "orders": conn.execute(
                    "SELECT * FROM business.paper_orders WHERE account_id=%s ORDER BY created_at DESC LIMIT 100",
                    (account_id,),
                ).fetchall(),
            }

    @staticmethod
    def _existing(conn, account_id: str, body: dict) -> dict | None:
        fingerprint = input_hash({k: v for k, v in body.items() if k != "idempotency_key"})
        existing = conn.execute(
            "SELECT * FROM business.paper_orders WHERE account_id=%s AND idempotency_key=%s",
            (account_id, body["idempotency_key"]),
        ).fetchone()
        if existing and existing["input_hash"] != fingerprint:
            raise AccessError(409, "idempotency_conflict")
        return existing

    def existing_order(self, ctx: TenantContext, account_id: str, body: dict) -> dict | None:
        with self.db.business_transaction(ctx, permission="paper:write") as conn:
            self._account(conn, account_id)
            return self._existing(conn, account_id, body)

    @staticmethod
    def expire(conn) -> None:
        # DAY orders never carry over into the next local trading date, including frozen accounts.
        conn.execute(
            """UPDATE business.paper_orders SET status='expired',reserved_cash=0,reason='paper_day_expired'
               WHERE status='queued' AND (created_at AT TIME ZONE 'Asia/Shanghai')::date < %s""",
            (datetime.now(CN_TZ).date(),),
        )

    def order(self, ctx: TenantContext, account_id: str, body: dict, quote: MarketQuote) -> dict:
        with self.db.business_transaction(ctx, write=True, permission="paper:write") as conn:
            return self.submit(conn, ctx, account_id, body, quote)

    @staticmethod
    def submit(conn, ctx, account_id, body, quote, automation=None):
        fingerprint = input_hash({k: v for k, v in body.items() if k != "idempotency_key"})
        account = Paper._account(conn, account_id, True)
        existing = Paper._existing(conn, account_id, body)
        if existing:
            return existing
        if not quote.tradable() or quote.symbol != body["symbol"]:
            raise AccessError(409, "quote_not_tradable")
        if account["status"] != "active":
            raise AccessError(409, "paper_account_frozen")
        Paper.expire(conn)
        qty = body["qty"]
        if qty <= 0 or qty > 10000000 or qty % 100:
            raise AccessError(422, "paper_lot_size")
        side = body["side"]
        price = apply_slippage(quote.price, side, float(account["slippage_bps"]))
        fee = buy_fee(qty, price, float(account["commission_pct"])) if side == "buy" else sell_fee(
            qty, price, float(account["commission_pct"]), float(account["stamp_tax_pct"])
        )
        reserved = money(qty * price + fee) if side == "buy" else money(0)
        pending = conn.execute(
            "SELECT * FROM business.paper_orders WHERE account_id=%s AND status='queued' FOR UPDATE", (account_id,)
        ).fetchall()
        if side == "buy" and reserved + sum((o["reserved_cash"] for o in pending), Decimal(0)) > account["cash"]:
            raise AccessError(409, "paper_insufficient_cash")
        if side == "sell":
            today = quote.source_time.astimezone(CN_TZ).date()
            pos = positions(Paper._fills(conn, account_id), today).get(body["symbol"], {"available_qty": 0})
            queued = sum(o["qty"] for o in pending if o["side"] == "sell" and o["symbol"] == body["symbol"])
            if queued + qty > pos["available_qty"]:
                raise AccessError(409, "paper_t1_or_insufficient_position")
        order = conn.execute(
            """INSERT INTO business.paper_orders
               (tenant_id,account_id,id,idempotency_key,input_hash,symbol,side,qty,reserved_cash,ref_price,
                quote_sequence,automation_id,automation_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (ctx.tenant_id, account_id, uuid.uuid4().hex, body["idempotency_key"], fingerprint,
             body["symbol"], side, qty, reserved, quote.price, quote.sequence,
             automation["id"] if automation else None, automation["version"] if automation else None),
        ).fetchone()
        Watchlist._audit(conn, ctx, "paper.order.create", order["id"])
        return order

    def cancel(self, ctx: TenantContext, account_id: str, order_id: str) -> None:
        with self.db.business_transaction(ctx, write=True, permission="paper:write") as conn:
            self._account(conn, account_id, True)
            order = conn.execute(
                "SELECT status FROM business.paper_orders WHERE account_id=%s AND id=%s FOR UPDATE", (account_id, order_id)
            ).fetchone()
            if not order:
                raise AccessError(404, "paper_order_not_found")
            if order["status"] != "queued":
                raise AccessError(409, "paper_order_not_cancellable")
            conn.execute(
                "UPDATE business.paper_orders SET status='cancelled',reserved_cash=0 WHERE account_id=%s AND id=%s",
                (account_id, order_id),
            )
            Watchlist._audit(conn, ctx, "paper.order.cancel", order_id)

    @staticmethod
    def match(conn, ctx: TenantContext, quotes: dict[str, MarketQuote]) -> int:
        filled = 0
        Paper.expire(conn)
        accounts = conn.execute("SELECT * FROM business.paper_accounts WHERE status='active' ORDER BY id FOR UPDATE").fetchall()
        for account in accounts:
            orders = conn.execute(
                "SELECT * FROM business.paper_orders WHERE account_id=%s AND status='queued' ORDER BY created_at,id FOR UPDATE",
                (account["id"],),
            ).fetchall()
            for order in orders:
                if order["automation_id"]:
                    automation = conn.execute(
                        "SELECT enabled,version,budget_per_order FROM business.trade_automations WHERE id=%s", (order["automation_id"],)
                    ).fetchone()
                    if not automation or not automation["enabled"] or automation["version"] != order["automation_version"]:
                        conn.execute(
                            """UPDATE business.paper_orders SET status='cancelled',reserved_cash=0,
                               reason='automation_changed' WHERE account_id=%s AND id=%s""",
                            (account["id"], order["id"]),
                        )
                        order["status"] = "cancelled"
                        continue
                quote = quotes.get(order["symbol"])
                if not quote or not quote.tradable() or quote.sequence <= order["quote_sequence"]:
                    continue
                if quote.source_time < order["created_at"]:
                    continue
                qty, side = order["qty"], order["side"]
                price = apply_slippage(quote.price, side, float(account["slippage_bps"]))
                fee = buy_fee(qty, price, float(account["commission_pct"])) if side == "buy" else sell_fee(
                    qty, price, float(account["commission_pct"]), float(account["stamp_tax_pct"])
                )
                # Apply the existing conservative rule: slippage reaching the band cannot fill.
                blocked = (side == "buy" and quote.limit_up is not None and price >= quote.limit_up) or (
                    side == "sell" and quote.limit_down is not None and price <= quote.limit_down
                )
                if blocked:
                    continue
                today = quote.source_time.astimezone(CN_TZ).date()
                reason = None
                if order["automation_id"] and side == "buy" and money(qty * price + fee) > automation["budget_per_order"]:
                    reason = "automation_budget_exceeded"
                other_reserved = sum((o["reserved_cash"] for o in orders if o["id"] != order["id"] and o["status"] == "queued"), Decimal(0))
                if side == "buy" and money(qty * price + fee) + other_reserved > account["cash"]:
                    reason = "paper_insufficient_cash"
                if side == "sell":
                    pos = positions(Paper._fills(conn, account["id"]), today).get(order["symbol"], {"available_qty": 0})
                    if qty > pos["available_qty"]:
                        reason = "paper_t1_or_insufficient_position"
                if reason:
                    conn.execute(
                        "UPDATE business.paper_orders SET status='expired',reserved_cash=0,reason=%s WHERE account_id=%s AND id=%s",
                        (reason, account["id"], order["id"]),
                    )
                    order["status"] = "expired"
                    continue
                conn.execute(
                    """INSERT INTO business.paper_fills
                       (tenant_id,account_id,id,order_id,symbol,side,qty,price,fee,trade_date,quote_sequence)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ctx.tenant_id, account["id"], uuid.uuid4().hex, order["id"], order["symbol"],
                     side, qty, price, fee, today, quote.sequence),
                )
                account["cash"] += money(-qty * price - fee if side == "buy" else qty * price - fee)
                conn.execute("UPDATE business.paper_accounts SET cash=%s WHERE id=%s", (account["cash"], account["id"]))
                conn.execute(
                    "UPDATE business.paper_orders SET status='filled',reserved_cash=0 WHERE account_id=%s AND id=%s",
                    (account["id"], order["id"]),
                )
                order["status"] = "filled"
                append_alert(conn, ctx.tenant_id, "paper:" + order["id"], {
                    "source": "paper", "symbol": order["symbol"], "severity": "info",
                    "message": f"{account['name']} {'买入' if side == 'buy' else '卖出'} {qty}股 @ {price:.4f}",
                    "price": price,
                })
                filled += 1
        return filled
