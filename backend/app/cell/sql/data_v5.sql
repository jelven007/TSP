-- Tenant-owned full-product state. Public market files stay outside this schema.
-- Ordering is independent of the date fact shown in the original watchlist.
ALTER TABLE business.watchlist_items ADD COLUMN position bigint NOT NULL DEFAULT 0;
CREATE INDEX watchlist_position_idx ON business.watchlist_items(tenant_id, position, added_at DESC);

CREATE TABLE business.documents (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    namespace text NOT NULL,
    id text NOT NULL,
    value jsonb NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, namespace, id)
);
ALTER TABLE business.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.documents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.documents
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));

-- Full product uses the original complete paper contract. V2 Cell paper
-- tables are retained for rollback; they cannot express delayed orders,
-- corporate actions, FIFO lots, or the original fee/settings contract.
CREATE TABLE business.full_paper_accounts (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    value jsonb NOT NULL,
    positions jsonb,
    revision bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, id),
    CHECK ((value->>'initial_cash')::numeric > 0),
    CHECK ((value->>'cash')::numeric >= 0),
    CHECK (value->>'status' IN ('active','frozen'))
);
CREATE TABLE business.full_paper_orders (
    tenant_id text NOT NULL,
    account_id text NOT NULL,
    id text NOT NULL,
    value jsonb NOT NULL,
    idempotency_key text,
    input_hash text,
    PRIMARY KEY (tenant_id, account_id, id),
    UNIQUE (tenant_id, account_id, idempotency_key),
    FOREIGN KEY (tenant_id, account_id) REFERENCES business.full_paper_accounts(tenant_id, id),
    CHECK (value->>'status' IN ('pending','filled','cancelled','expired'))
);
CREATE INDEX full_paper_pending_idx ON business.full_paper_orders(tenant_id, account_id)
    WHERE value->>'status' = 'pending';
CREATE TABLE business.full_paper_fills (
    tenant_id text NOT NULL,
    account_id text NOT NULL,
    id text NOT NULL,
    ordinal bigint NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY (tenant_id, account_id, id),
    UNIQUE (tenant_id, account_id, ordinal),
    FOREIGN KEY (tenant_id, account_id) REFERENCES business.full_paper_accounts(tenant_id, id)
);
CREATE TABLE business.full_paper_nav (
    tenant_id text NOT NULL,
    account_id text NOT NULL,
    day date NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY (tenant_id, account_id, day),
    FOREIGN KEY (tenant_id, account_id) REFERENCES business.full_paper_accounts(tenant_id, id)
);
DO $full_paper_rls$
DECLARE tab text;
BEGIN
    FOREACH tab IN ARRAY ARRAY['full_paper_accounts','full_paper_orders','full_paper_fills','full_paper_nav']
    LOOP
        EXECUTE format('ALTER TABLE business.%I ENABLE ROW LEVEL SECURITY', tab);
        EXECUTE format('ALTER TABLE business.%I FORCE ROW LEVEL SECURITY', tab);
        EXECUTE format('CREATE POLICY tenant_isolation ON business.%I
            USING (tenant_id = nullif(current_setting(''app.tenant_id'', true), ''''))
            WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'', true), ''''))', tab);
    END LOOP;
END
$full_paper_rls$;

CREATE TABLE business.research_jobs (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    user_id text NOT NULL,
    kind text NOT NULL,
    input jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'cancelling', 'succeeded',
                         'succeeded_with_budget_exhausted', 'failed', 'cancelled',
                         'interrupted', 'skipped_prerequisite')),
    result jsonb,
    signature text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    events jsonb NOT NULL DEFAULT '[]',
    event_seq bigint NOT NULL DEFAULT 0,
    error text,
    lease_owner text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    auth_version bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX research_claim_idx ON business.research_jobs(state, created_at);
CREATE UNIQUE INDEX research_schedule_once_idx
    ON business.research_jobs(tenant_id,(input->>'source_claim'))
    WHERE input ? 'source_claim';
ALTER TABLE business.research_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.research_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.research_jobs
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));

CREATE TABLE business.research_artifacts (
    tenant_id text NOT NULL,
    job_id text NOT NULL,
    name text NOT NULL,
    content bytea NOT NULL CHECK (octet_length(content) <= 67108864),
    PRIMARY KEY (tenant_id, job_id, name),
    FOREIGN KEY (tenant_id, job_id) REFERENCES business.research_jobs(tenant_id, id) ON DELETE CASCADE
);
ALTER TABLE business.research_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.research_artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.research_artifacts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));

CREATE TABLE business.full_events (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, sequence)
);
ALTER TABLE business.full_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.full_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.full_events
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));

-- Finished deliveries disappear with expired history; pending deliveries keep
-- their alert alive until sent, cancelled, or exhausted.
ALTER TABLE business.notification_channels ADD COLUMN auth_version bigint NOT NULL DEFAULT 1;
ALTER TABLE business.notification_outbox
    DROP CONSTRAINT notification_outbox_tenant_id_alert_id_fkey,
    ADD FOREIGN KEY (tenant_id,alert_id) REFERENCES business.alerts(tenant_id,id) ON DELETE CASCADE;

CREATE TRIGGER full_accounts_wake AFTER INSERT OR UPDATE ON business.full_paper_accounts
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('realtime');
CREATE TRIGGER full_orders_wake AFTER INSERT OR UPDATE ON business.full_paper_orders
    FOR EACH ROW EXECUTE FUNCTION scheduler.wake('realtime');
ALTER TABLE scheduler.work DROP CONSTRAINT work_kind_check,
    ADD CHECK (kind IN ('realtime','strategies','notifications','automation','scheduled'));
INSERT INTO scheduler.slots(kind,slot) VALUES ('scheduled',1),('scheduled',2);
CREATE FUNCTION scheduler.full_documents_wake() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE unit text; space text; tenant text;
BEGIN
    space := CASE WHEN TG_OP='DELETE' THEN OLD.namespace ELSE NEW.namespace END;
    tenant := CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    unit := CASE WHEN space='notification_requests' THEN 'notifications'
                 WHEN space IN ('mining_schedule','review_schedule') THEN 'scheduled'
                 WHEN space LIKE 'paper_auto:%' THEN 'realtime' ELSE NULL END;
    IF unit IS NOT NULL THEN
        INSERT INTO scheduler.work(tenant_id,kind,due_at) VALUES(tenant,unit,clock_timestamp())
        ON CONFLICT(tenant_id,kind) DO UPDATE SET
            due_at=least(scheduler.work.due_at,excluded.due_at),revision=scheduler.work.revision+1;
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION scheduler.full_documents_wake() FROM PUBLIC;
CREATE TRIGGER full_documents_wake AFTER INSERT OR UPDATE OR DELETE ON business.documents
    FOR EACH ROW EXECUTE FUNCTION scheduler.full_documents_wake();

CREATE FUNCTION scheduler.guard(tenant text, unit text, claimant text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM 1 FROM scheduler.work
        WHERE tenant_id=tenant AND kind=unit AND owner=claimant AND lease_until>clock_timestamp()
        FOR SHARE;
    RETURN FOUND;
END;
$$;
REVOKE ALL ON FUNCTION scheduler.guard(text,text,text) FROM PUBLIC;
CREATE FUNCTION scheduler.renew_full(tenant text, unit text, claimant text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    UPDATE scheduler.work SET lease_until=clock_timestamp()+interval '120 seconds'
        WHERE tenant_id=tenant AND kind=unit AND owner=claimant AND lease_until>clock_timestamp();
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE scheduler.slots SET lease_until=clock_timestamp()+interval '120 seconds'
        WHERE tenant_id=tenant AND kind=unit AND owner=claimant;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION scheduler.renew_full(text,text,text) FROM PUBLIC;

-- Only this narrow operation may create a binding at runtime. No owner DSN or
-- INSERT privilege on tenant_bindings is given to the API.
CREATE FUNCTION business.initialize_tenant(p_id text, p_epoch bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, business AS $$
DECLARE existing_epoch bigint; existing_enabled boolean;
BEGIN
    IF p_id IS NULL OR p_id !~ '^[a-zA-Z0-9_-]{1,128}$' OR p_epoch <> 1 THEN
        RAISE EXCEPTION 'invalid tenant initialization';
    END IF;
    PERFORM set_config('app.tenant_id', p_id, true);
    INSERT INTO business.tenant_bindings(tenant_id, routing_epoch)
        VALUES (p_id, p_epoch) ON CONFLICT DO NOTHING;
    SELECT routing_epoch, enabled INTO existing_epoch, existing_enabled
        FROM business.tenant_bindings WHERE tenant_id = p_id FOR UPDATE;
    IF existing_epoch <> p_epoch OR NOT existing_enabled THEN
        RAISE EXCEPTION 'binding cannot be changed by initialization';
    END IF;
    INSERT INTO business.documents(tenant_id, namespace, id, value)
        VALUES (p_id, 'preferences', 'default', '{}') ON CONFLICT DO NOTHING;
END;
$$;
REVOKE ALL ON FUNCTION business.initialize_tenant(text, bigint) FROM PUBLIC;

-- Offline import ownership is unavailable to every application runtime.
CREATE SCHEMA migration;
REVOKE ALL ON SCHEMA migration FROM PUBLIC;
CREATE TABLE migration.full_import_records (
    tenant_id text NOT NULL,
    table_name text NOT NULL,
    key_hash text NOT NULL,
    key jsonb NOT NULL,
    columns text[] NOT NULL,
    target_hash text NOT NULL,
    PRIMARY KEY (tenant_id,table_name,key_hash)
);
CREATE TABLE migration.full_import_batches (
    tenant_id text PRIMARY KEY,
    source_hash text NOT NULL,
    inventory jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- Historical originals remain downloadable, including incomplete manifests.
-- Credentials, sessions and verification records never enter this archive.
CREATE TABLE business.legacy_artifacts (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    path text NOT NULL,
    content bytea NOT NULL CHECK (octet_length(content) <= 67108864),
    sha256 text NOT NULL,
    PRIMARY KEY (tenant_id,path)
);
ALTER TABLE business.legacy_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.legacy_artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.legacy_artifacts
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));
