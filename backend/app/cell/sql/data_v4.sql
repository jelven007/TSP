CREATE TABLE scheduler.capacity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    max_tenants integer NOT NULL DEFAULT 1000 CHECK (max_tenants > 0),
    max_realtime_units integer NOT NULL DEFAULT 200 CHECK (max_realtime_units > 0),
    max_tenant_units integer NOT NULL DEFAULT 100 CHECK (max_tenant_units > 0)
);
INSERT INTO scheduler.capacity DEFAULT VALUES;
CREATE TABLE scheduler.tenant_load (
    tenant_id text PRIMARY KEY REFERENCES business.tenant_bindings(tenant_id),
    units integer NOT NULL CHECK (units >= 0)
);
CREATE FUNCTION scheduler.check_capacity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
DECLARE tenant text; current_units integer; used_units bigint; limits record;
BEGIN
    tenant:=CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    -- Admission writes are uncommon; this row serializes concurrent increases so
    -- two API replicas cannot both consume the last available capacity.
    SELECT * INTO limits FROM scheduler.capacity FOR UPDATE;
    IF TG_TABLE_NAME='tenant_bindings' THEN
        IF (SELECT count(*) FROM scheduler.tenant_load)>=limits.max_tenants THEN
            RAISE EXCEPTION 'cell tenant capacity exceeded' USING ERRCODE='P2001';
        END IF;
        INSERT INTO scheduler.tenant_load VALUES(tenant,0) ON CONFLICT DO NOTHING;
        RETURN NULL;
    END IF;
    SELECT
        coalesce((SELECT sum(jsonb_array_length(r.definition->'symbols'))
                  FROM business.monitor_rules r WHERE r.tenant_id=tenant AND r.enabled),0)
        +coalesce((SELECT count(*)*4 FROM business.paper_orders o
                   WHERE o.tenant_id=tenant AND o.status='queued'),0)
        +coalesce((SELECT sum(a.max_positions*4) FROM business.trade_automations a
                   WHERE a.tenant_id=tenant AND a.enabled),0)
        INTO current_units;
    SELECT coalesce(sum(units),0) INTO used_units FROM scheduler.tenant_load WHERE tenant_id!=tenant;
    IF current_units>limits.max_tenant_units OR used_units+current_units>limits.max_realtime_units THEN
        -- Always permit decreases: an operator lowering a limit must not prevent
        -- cancellation, pausing rules or removing load in an overfull Cell.
        IF current_units>coalesce((SELECT units FROM scheduler.tenant_load WHERE tenant_id=tenant),0) THEN
            RAISE EXCEPTION 'cell realtime capacity exceeded' USING ERRCODE='P2001';
        END IF;
    END IF;
    INSERT INTO scheduler.tenant_load VALUES(tenant,current_units)
        ON CONFLICT(tenant_id) DO UPDATE SET units=excluded.units;
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION scheduler.check_capacity() FROM PUBLIC;
CREATE TRIGGER capacity_bindings AFTER INSERT ON business.tenant_bindings
    FOR EACH ROW EXECUTE FUNCTION scheduler.check_capacity();
CREATE TRIGGER capacity_rules AFTER INSERT OR UPDATE OR DELETE ON business.monitor_rules
    FOR EACH ROW EXECUTE FUNCTION scheduler.check_capacity();
CREATE TRIGGER capacity_orders AFTER INSERT OR UPDATE OR DELETE ON business.paper_orders
    FOR EACH ROW EXECUTE FUNCTION scheduler.check_capacity();
CREATE TRIGGER capacity_automation AFTER INSERT OR UPDATE OR DELETE ON business.trade_automations
    FOR EACH ROW EXECUTE FUNCTION scheduler.check_capacity();
-- Preserve existing load even if it exceeds the new conservative limits.
ALTER TABLE business.monitor_rules NO FORCE ROW LEVEL SECURITY;
ALTER TABLE business.paper_orders NO FORCE ROW LEVEL SECURITY;
ALTER TABLE business.trade_automations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE business.tenant_bindings NO FORCE ROW LEVEL SECURITY;
INSERT INTO scheduler.tenant_load(tenant_id,units)
    SELECT tenant_id,sum(units)::integer FROM (
        SELECT tenant_id,coalesce(jsonb_array_length(definition->'symbols'),0) AS units
            FROM business.monitor_rules WHERE enabled
        UNION ALL SELECT tenant_id,4 FROM business.paper_orders WHERE status='queued'
        UNION ALL SELECT tenant_id,max_positions*4 FROM business.trade_automations WHERE enabled
    ) load GROUP BY tenant_id;
INSERT INTO scheduler.tenant_load SELECT tenant_id,0 FROM business.tenant_bindings ON CONFLICT DO NOTHING;
ALTER TABLE business.monitor_rules FORCE ROW LEVEL SECURITY;
ALTER TABLE business.paper_orders FORCE ROW LEVEL SECURITY;
ALTER TABLE business.trade_automations FORCE ROW LEVEL SECURITY;
ALTER TABLE business.tenant_bindings FORCE ROW LEVEL SECURITY;
