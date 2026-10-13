CREATE TABLE business.notification_channels (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('email','webhook')),
    destination text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    version bigint NOT NULL DEFAULT 1,
    user_id text NOT NULL,
    token_id text,
    policy_version bigint NOT NULL,
    routing_epoch bigint NOT NULL,
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE business.notification_outbox (
    tenant_id text NOT NULL,
    id text NOT NULL,
    channel_id text NOT NULL,
    channel_version bigint NOT NULL,
    alert_id text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','sent','dead','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    due_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    execution_epoch bigint NOT NULL DEFAULT 0,
    error text,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,channel_id,alert_id),
    FOREIGN KEY (tenant_id,channel_id) REFERENCES business.notification_channels(tenant_id,id),
    FOREIGN KEY (tenant_id,alert_id) REFERENCES business.alerts(tenant_id,id)
);
CREATE INDEX notification_due_idx ON business.notification_outbox(tenant_id,due_at)
    WHERE status IN ('queued','sending');
CREATE TABLE business.trade_automations (
    tenant_id text NOT NULL,
    id text NOT NULL,
    name text NOT NULL,
    strategy_id text NOT NULL,
    account_id text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    version bigint NOT NULL DEFAULT 1,
    run_time time NOT NULL,
    max_positions integer NOT NULL CHECK (max_positions BETWEEN 1 AND 20),
    budget_per_order numeric(20,2) NOT NULL CHECK (budget_per_order > 0),
    stop_loss_pct numeric(10,7) NOT NULL CHECK (stop_loss_pct > 0 AND stop_loss_pct < 1),
    take_profit_pct numeric(10,7) NOT NULL CHECK (take_profit_pct > 0 AND take_profit_pct < 10),
    max_daily_orders integer NOT NULL CHECK (max_daily_orders BETWEEN 1 AND 100),
    user_id text NOT NULL,
    token_id text,
    policy_version bigint NOT NULL,
    routing_epoch bigint NOT NULL,
    last_run_date date,
    last_job_id text,
    error text,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,account_id),
    FOREIGN KEY (tenant_id,strategy_id) REFERENCES business.strategies(tenant_id,id),
    FOREIGN KEY (tenant_id,account_id) REFERENCES business.paper_accounts(tenant_id,id)
);
CREATE TABLE business.trade_intents (
    tenant_id text NOT NULL,
    id text NOT NULL,
    automation_id text NOT NULL,
    automation_version bigint NOT NULL,
    job_id text,
    symbol text NOT NULL,
    side text NOT NULL CHECK (side IN ('buy','sell')),
    signal_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','submitted','skipped','cancelled')),
    reason text,
    order_id text,
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,automation_id) REFERENCES business.trade_automations(tenant_id,id),
    FOREIGN KEY (tenant_id,job_id) REFERENCES business.strategy_jobs(tenant_id,id)
);
CREATE INDEX trade_intents_queue_idx ON business.trade_intents(tenant_id,signal_at) WHERE status='queued';
ALTER TABLE business.strategy_jobs ADD COLUMN automation_id text;
ALTER TABLE business.strategy_jobs ADD COLUMN automation_version bigint;
ALTER TABLE business.paper_orders ADD COLUMN automation_id text;
ALTER TABLE business.paper_orders ADD COLUMN automation_version bigint;
CREATE INDEX paper_orders_active_idx ON business.paper_orders(tenant_id,account_id) WHERE status='queued';
CREATE INDEX monitor_rules_active_idx ON business.monitor_rules(tenant_id) WHERE enabled;
CREATE INDEX automations_active_idx ON business.trade_automations(tenant_id) WHERE enabled;
CREATE INDEX strategy_jobs_active_idx ON business.strategy_jobs(tenant_id,lease_until)
    WHERE status IN ('queued','running','cancelling');
DO $migration$
DECLARE tab text;
BEGIN
    FOREACH tab IN ARRAY ARRAY['notification_channels','notification_outbox','trade_automations','trade_intents']
    LOOP
        EXECUTE format('ALTER TABLE business.%I ENABLE ROW LEVEL SECURITY', tab);
        EXECUTE format('ALTER TABLE business.%I FORCE ROW LEVEL SECURITY', tab);
        EXECUTE format('CREATE POLICY tenant_isolation ON business.%I
            USING (tenant_id = nullif(current_setting(''app.tenant_id'', true), ''''))
            WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'', true), ''''))', tab);
    END LOOP;
END
$migration$;

-- This schema contains scheduling metadata only, never tenant payloads or credentials.
-- Runtime cannot mutate it except through narrowly scoped functions.
CREATE SCHEMA scheduler;
REVOKE ALL ON SCHEMA scheduler FROM PUBLIC;
CREATE TABLE scheduler.work (
    tenant_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('realtime','strategies','notifications','automation')),
    due_at timestamptz,
    revision bigint NOT NULL DEFAULT 1,
    lease_until timestamptz,
    owner text,
    PRIMARY KEY (tenant_id,kind)
);
CREATE INDEX work_due_idx ON scheduler.work(kind,due_at,tenant_id) WHERE due_at IS NOT NULL;
CREATE TABLE scheduler.slots (
    kind text NOT NULL,
    slot integer NOT NULL,
    tenant_id text,
    owner text,
    lease_until timestamptz,
    PRIMARY KEY (kind,slot)
);
INSERT INTO scheduler.slots(kind,slot)
    SELECT kind,generate_series(1,budget) FROM
    (VALUES ('realtime',32),('strategies',4),('notifications',8),('automation',8)) AS limits(kind,budget);

CREATE FUNCTION scheduler.wake() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
DECLARE tenant text; unit text;
BEGIN
    tenant := CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    FOREACH unit IN ARRAY TG_ARGV LOOP
        INSERT INTO scheduler.work(tenant_id,kind,due_at) VALUES (tenant,unit,clock_timestamp())
        ON CONFLICT(tenant_id,kind) DO UPDATE SET
            due_at=least(scheduler.work.due_at,excluded.due_at),revision=scheduler.work.revision+1;
    END LOOP;
    RETURN NULL;
END
$function$;
CREATE TRIGGER jobs_wake AFTER INSERT OR UPDATE OR DELETE ON business.strategy_jobs
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('strategies','automation');
CREATE TRIGGER rules_wake AFTER INSERT OR UPDATE OR DELETE ON business.monitor_rules
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('realtime');
CREATE TRIGGER orders_wake AFTER INSERT OR UPDATE OR DELETE ON business.paper_orders
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('realtime');
CREATE TRIGGER notifications_wake AFTER INSERT OR UPDATE OR DELETE ON business.notification_outbox
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('notifications');
CREATE TRIGGER automation_wake AFTER INSERT OR UPDATE OR DELETE ON business.trade_automations
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('automation','realtime');
CREATE TRIGGER intents_wake AFTER INSERT OR UPDATE OR DELETE ON business.trade_intents
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('automation');

CREATE FUNCTION scheduler.claim(unit text, capacity integer, seconds integer, claimant text)
RETURNS TABLE(tenant_id text, kind text, revision bigint, owner text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
DECLARE permit record; item record; deadline timestamptz;
BEGIN
    IF capacity NOT BETWEEN 1 AND 128 OR seconds NOT BETWEEN 30 AND 600
       OR length(claimant) NOT BETWEEN 1 AND 80 THEN RAISE EXCEPTION 'invalid claim'; END IF;
    deadline := clock_timestamp() + seconds * interval '1 second';
    FOR permit IN SELECT s.* FROM scheduler.slots s
        WHERE s.kind=unit AND (s.lease_until IS NULL OR s.lease_until<clock_timestamp())
        ORDER BY s.slot FOR UPDATE SKIP LOCKED LIMIT capacity
    LOOP
        SELECT w.* INTO item FROM scheduler.work w
            WHERE w.kind=unit AND w.due_at<=clock_timestamp()
              AND (w.lease_until IS NULL OR w.lease_until<clock_timestamp())
            ORDER BY w.due_at,w.tenant_id FOR UPDATE SKIP LOCKED LIMIT 1;
        IF NOT FOUND THEN EXIT; END IF;
        UPDATE scheduler.work w SET owner=claimant,lease_until=deadline
            WHERE w.tenant_id=item.tenant_id AND w.kind=unit;
        UPDATE scheduler.slots s SET tenant_id=item.tenant_id,owner=claimant,lease_until=deadline
            WHERE s.kind=unit AND s.slot=permit.slot;
        tenant_id:=item.tenant_id; kind:=unit; revision:=item.revision; owner:=claimant;
        RETURN NEXT;
    END LOOP;
END
$function$;
CREATE FUNCTION scheduler.finish(tenant text, unit text, claimant text, observed bigint, next_due timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
BEGIN
    UPDATE scheduler.work SET owner=NULL,lease_until=NULL,
        due_at=CASE WHEN revision=observed THEN next_due ELSE least(due_at,next_due) END
        WHERE tenant_id=tenant AND kind=unit AND owner=claimant AND lease_until>clock_timestamp();
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE scheduler.slots SET owner=NULL,lease_until=NULL,tenant_id=NULL
        WHERE kind=unit AND tenant_id=tenant AND owner=claimant;
    RETURN true;
END
$function$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA scheduler FROM PUBLIC;
-- Backfill v2 work atomically as part of the offline migration.
-- Temporarily allow the table owner to read all tenants inside this DDL transaction;
-- FORCE RLS is restored before commit, and runtime is never an owner.
ALTER TABLE business.monitor_rules NO FORCE ROW LEVEL SECURITY;
ALTER TABLE business.paper_orders NO FORCE ROW LEVEL SECURITY;
ALTER TABLE business.strategy_jobs NO FORCE ROW LEVEL SECURITY;
INSERT INTO scheduler.work(tenant_id,kind,due_at)
    SELECT tenant_id,'realtime',now() FROM business.monitor_rules WHERE enabled
    UNION SELECT tenant_id,'realtime',now() FROM business.paper_orders WHERE status='queued';
INSERT INTO scheduler.work(tenant_id,kind,due_at)
    SELECT DISTINCT tenant_id,'strategies',now() FROM business.strategy_jobs
    WHERE status IN ('queued','running','cancelling');
ALTER TABLE business.monitor_rules FORCE ROW LEVEL SECURITY;
ALTER TABLE business.paper_orders FORCE ROW LEVEL SECURITY;
ALTER TABLE business.strategy_jobs FORCE ROW LEVEL SECURITY;
