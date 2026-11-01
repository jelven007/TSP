-- Full scope pools are separate from strategy candidates and trading state.
CREATE TABLE business.daily_stock_pools (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    strategy_id text NOT NULL,
    day date NOT NULL,
    started_at bigint NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY (tenant_id,id),
    CHECK (jsonb_typeof(value->'symbols')='array'),
    CHECK ((value->>'total')::integer = jsonb_array_length(value->'symbols'))
);
CREATE INDEX daily_stock_pools_day_idx
    ON business.daily_stock_pools(tenant_id,strategy_id,day,started_at DESC,id DESC);
ALTER TABLE business.daily_stock_pools ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.daily_stock_pools FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.daily_stock_pools
    USING (tenant_id=nullif(current_setting('app.tenant_id',true),''))
    WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),''));

CREATE FUNCTION scheduler.stock_pool_wake() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE tenant text;
BEGIN
    tenant := CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    IF TG_TABLE_NAME='documents' THEN
        IF (CASE WHEN TG_OP='DELETE' THEN OLD.namespace ELSE NEW.namespace END)
              NOT IN ('preferences','strategy_sources','strategy_overrides') THEN
            RETURN NULL;
        END IF;
    END IF;
    INSERT INTO scheduler.work(tenant_id,kind,due_at) VALUES(tenant,'scheduled',clock_timestamp())
        ON CONFLICT(tenant_id,kind) DO UPDATE SET
          due_at=least(scheduler.work.due_at,excluded.due_at),revision=scheduler.work.revision+1;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION scheduler.stock_pool_wake() FROM PUBLIC;
CREATE TRIGGER stock_pool_documents_wake AFTER INSERT OR UPDATE OR DELETE ON business.documents
    FOR EACH ROW EXECUTE FUNCTION scheduler.stock_pool_wake();
CREATE TRIGGER stock_pool_groups_wake AFTER INSERT OR UPDATE OR DELETE ON business.watchlist_group_items
    FOR EACH ROW EXECUTE FUNCTION scheduler.stock_pool_wake();

-- Initialization creates preferences, which wakes new tenants. Backfill existing
-- bindings under the migration owner only; FORCE RLS is restored in this transaction.
ALTER TABLE business.tenant_bindings NO FORCE ROW LEVEL SECURITY;
INSERT INTO scheduler.work(tenant_id,kind,due_at)
    SELECT tenant_id,'scheduled',now() FROM business.tenant_bindings WHERE enabled
    ON CONFLICT(tenant_id,kind) DO UPDATE SET due_at=excluded.due_at,revision=scheduler.work.revision+1;
ALTER TABLE business.tenant_bindings FORCE ROW LEVEL SECURITY;
