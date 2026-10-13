CREATE SCHEMA business;
CREATE TABLE business.schema_version (version integer PRIMARY KEY, checksum text NOT NULL);
CREATE TABLE business.cell_identity (singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), cell_id text NOT NULL);
CREATE TABLE business.tenant_bindings (
    tenant_id text PRIMARY KEY,
    routing_epoch bigint NOT NULL CHECK (routing_epoch > 0),
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE business.watchlist_groups (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 24),
    color text NOT NULL DEFAULT 'sky',
    position integer NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, id)
);
CREATE UNIQUE INDEX watchlist_group_name_idx ON business.watchlist_groups(tenant_id, lower(name));
CREATE TABLE business.watchlist_items (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    symbol text NOT NULL,
    note text NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, symbol)
);
CREATE TABLE business.watchlist_group_items (
    tenant_id text NOT NULL,
    group_id text NOT NULL,
    symbol text NOT NULL,
    PRIMARY KEY (tenant_id, group_id, symbol),
    FOREIGN KEY (tenant_id, group_id) REFERENCES business.watchlist_groups(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, symbol) REFERENCES business.watchlist_items(tenant_id, symbol) ON DELETE CASCADE
);
CREATE TABLE business.audit (
    tenant_id text NOT NULL REFERENCES business.tenant_bindings(tenant_id),
    id text NOT NULL,
    actor_id text NOT NULL,
    action text NOT NULL,
    target_id text NOT NULL,
    request_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX watchlist_order_idx ON business.watchlist_items(tenant_id, added_at DESC, symbol);

ALTER TABLE business.tenant_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.tenant_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.tenant_bindings
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));

ALTER TABLE business.watchlist_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.watchlist_groups FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.watchlist_groups
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));
ALTER TABLE business.watchlist_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.watchlist_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.watchlist_items
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));
ALTER TABLE business.watchlist_group_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.watchlist_group_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.watchlist_group_items
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));
ALTER TABLE business.audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.audit FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.audit
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), ''));
