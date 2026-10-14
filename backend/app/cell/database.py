from contextlib import contextmanager

from psycopg import Error
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.cell.config import CellSettings
from app.cell.context import AccessError, TenantContext


class Databases:
    def __init__(self, settings: CellSettings):
        self.settings = settings
        self.control = self._pool(settings.control_dsn.get_secret_value(), "cell-control")
        self.data = self._pool(settings.data_dsn.get_secret_value(), "cell-data")

    def _pool(self, dsn: str, name: str) -> ConnectionPool:
        return ConnectionPool(
            dsn,
            name=name,
            min_size=0,
            max_size=self.settings.pool_size,
            timeout=self.settings.pool_timeout,
            open=False,
            # Do not log connection strings or attempt endless reconnects.
            kwargs={"row_factory": dict_row, "autocommit": True, "connect_timeout": 5},
            reconnect_timeout=5,
        )

    def open(self) -> None:
        self.control.open()
        self.data.open()
        try:
            self.check()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.data.close()
        self.control.close()

    def check(self) -> None:
        runtime_roles = []
        for pool, schema in ((self.control, "control"), (self.data, "business")):
            with pool.connection() as conn:
                role = conn.execute(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
                runtime_roles.append(role["rolname"])
                owners = conn.execute(
                    """SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = %s AND pg_has_role(current_user, c.relowner, 'MEMBER')
                       LIMIT 1""",
                    (schema,),
                ).fetchone()
                if role["rolsuper"] or role["rolbypassrls"] or owners:
                    raise RuntimeError("Cell runtime must use a non-owner, non-BYPASSRLS role")
                version = conn.execute(
                    f"SELECT version FROM {schema}.schema_version ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if not version or version["version"] != self.settings.schema_version:
                    raise RuntimeError("unsupported Cell database schema")
        if self.settings.schema_version >= 5:
            if runtime_roles[0] == runtime_roles[1]:
                raise RuntimeError("full-product identity and business runtime roles must be separate")
            for pool, other in ((self.control, runtime_roles[1]), (self.data, runtime_roles[0])):
                with pool.connection() as conn:
                    if conn.execute("SELECT pg_has_role(current_user,%s,'MEMBER') AS inherited", (other,)).fetchone()["inherited"]:
                        raise RuntimeError("full-product runtime roles must not inherit each other")
        with self.data.connection() as conn:
            identity = conn.execute("SELECT cell_id FROM business.cell_identity").fetchone()
            if not identity or identity["cell_id"] != self.settings.id:
                raise RuntimeError("data database belongs to a different Cell")
            expected = {
                "tenant_bindings", "watchlist_items", "watchlist_groups", "watchlist_group_items", "audit",
                "strategies", "strategy_jobs", "monitor_rules", "monitor_state", "alerts",
                "paper_accounts", "paper_orders", "paper_fills",
                "notification_channels", "notification_outbox", "trade_automations", "trade_intents",
            }
            if self.settings.schema_version >= 5:
                expected.update({"documents", "research_jobs", "research_artifacts", "full_events",
                                 "full_paper_accounts", "full_paper_orders", "full_paper_fills", "full_paper_nav",
                                 "legacy_artifacts"})
            unsafe = conn.execute(
                """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'business' AND c.relname = ANY(%s)
                   AND c.relrowsecurity AND c.relforcerowsecurity""", (sorted(expected),)
            ).fetchall()
            if {row["relname"] for row in unsafe} != expected:
                raise RuntimeError("Cell tenant tables must enforce RLS")

    @contextmanager
    def business_transaction(
        self, context: TenantContext, *, write: bool = False, permission: str | None = None,
    ):
        context.require(permission or ("watchlist:write" if write else "watchlist:read"))
        if context.cell_id != self.settings.id:
            raise AccessError(409, "wrong_cell")
        with self.data.connection() as conn, conn.transaction():
            conn.execute(
                """SELECT set_config('app.tenant_id', %s, true),set_config('app.user_id', %s, true),
                          set_config('statement_timeout', '10000', true)""", (context.tenant_id, context.user_id),
            )
            lock = "UPDATE" if write else "SHARE"
            binding = conn.execute(
                f"SELECT routing_epoch, enabled FROM business.tenant_bindings FOR {lock}"
            ).fetchone()
            if not binding or not binding["enabled"]:
                raise AccessError(409, "tenant_binding_unavailable")
            if binding["routing_epoch"] != context.routing_epoch:
                raise AccessError(409, "routing_epoch_stale")
            try:
                yield conn
            except Error as exc:
                if exc.sqlstate == "P2001":
                    raise AccessError(429, "cell_capacity_exceeded") from exc
                raise
