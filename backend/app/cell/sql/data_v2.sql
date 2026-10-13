CREATE TABLE business.strategies (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    template_id text NOT NULL,
    template_hash text NOT NULL,
    name text NOT NULL,
    overrides jsonb NOT NULL DEFAULT '{}',
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE business.strategy_jobs (
    tenant_id text NOT NULL,
    id text NOT NULL,
    strategy_id text NOT NULL,
    user_id text NOT NULL,
    routing_epoch bigint NOT NULL,
    policy_version bigint NOT NULL,
    strategy_version bigint NOT NULL,
    idempotency_key text NOT NULL,
    input_hash text NOT NULL,
    input jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','cancelling','succeeded','failed','cancelled','interrupted')),
    execution_epoch bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    result jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, strategy_id) REFERENCES business.strategies(tenant_id, id)
);
CREATE INDEX strategy_jobs_queue_idx ON business.strategy_jobs(tenant_id, status, created_at);
CREATE TABLE business.monitor_rules (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    definition jsonb NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE business.monitor_state (
    tenant_id text NOT NULL,
    rule_id text NOT NULL,
    symbol text NOT NULL,
    rule_version bigint NOT NULL,
    last_sequence bigint NOT NULL DEFAULT -1,
    last_triggered_at timestamptz,
    PRIMARY KEY (tenant_id, rule_id, symbol),
    FOREIGN KEY (tenant_id, rule_id) REFERENCES business.monitor_rules(tenant_id, id) ON DELETE CASCADE
);
CREATE TABLE business.alerts (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    dedupe_key text NOT NULL,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, dedupe_key)
);
CREATE INDEX alerts_time_idx ON business.alerts(tenant_id, created_at DESC);
CREATE TABLE business.paper_accounts (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    name text NOT NULL,
    initial_cash numeric(20,2) NOT NULL CHECK (initial_cash > 0),
    cash numeric(20,2) NOT NULL CHECK (cash >= 0),
    commission_pct numeric(10,7) NOT NULL,
    stamp_tax_pct numeric(10,7) NOT NULL,
    slippage_bps numeric(10,4) NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','frozen')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE business.paper_orders (
    tenant_id text NOT NULL,
    account_id text NOT NULL,
    id text NOT NULL,
    idempotency_key text NOT NULL,
    input_hash text NOT NULL,
    symbol text NOT NULL,
    side text NOT NULL CHECK (side IN ('buy','sell')),
    qty integer NOT NULL CHECK (qty > 0 AND qty % 100 = 0),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','filled','cancelled','expired')),
    reserved_cash numeric(20,2) NOT NULL DEFAULT 0,
    ref_price numeric(20,4) NOT NULL,
    quote_sequence bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    reason text,
    PRIMARY KEY (tenant_id, account_id, id),
    UNIQUE (tenant_id, account_id, idempotency_key),
    FOREIGN KEY (tenant_id, account_id) REFERENCES business.paper_accounts(tenant_id, id)
);
CREATE TABLE business.paper_fills (
    tenant_id text NOT NULL,
    account_id text NOT NULL,
    id text NOT NULL,
    order_id text NOT NULL,
    symbol text NOT NULL,
    side text NOT NULL CHECK (side IN ('buy','sell')),
    qty integer NOT NULL,
    price numeric(20,4) NOT NULL,
    fee numeric(20,2) NOT NULL,
    trade_date date NOT NULL,
    quote_sequence bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, account_id, id),
    UNIQUE (tenant_id, account_id, order_id),
    FOREIGN KEY (tenant_id, account_id, order_id) REFERENCES business.paper_orders(tenant_id, account_id, id)
);
CREATE INDEX paper_fill_order_idx ON business.paper_fills(tenant_id, account_id, created_at, id);

DO $migration$
DECLARE tab text;
BEGIN
    FOREACH tab IN ARRAY ARRAY['strategies','strategy_jobs','monitor_rules','monitor_state','alerts',
                              'paper_accounts','paper_orders','paper_fills']
    LOOP
        EXECUTE format('ALTER TABLE business.%I ENABLE ROW LEVEL SECURITY', tab);
        EXECUTE format('ALTER TABLE business.%I FORCE ROW LEVEL SECURITY', tab);
        EXECUTE format('CREATE POLICY tenant_isolation ON business.%I
            USING (tenant_id = nullif(current_setting(''app.tenant_id'', true), ''''))
            WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'', true), ''''))', tab);
    END LOOP;
END
$migration$;
