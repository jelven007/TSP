CREATE SCHEMA control;
CREATE TABLE control.schema_version (version integer PRIMARY KEY, checksum text NOT NULL);
CREATE TABLE control.users (
    id text PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_salt text NOT NULL,
    password_hash text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE control.cells (
    id text PRIMARY KEY,
    api_url text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'draining', 'disabled'))
);
CREATE TABLE control.tenants (
    id text PRIMARY KEY,
    name text NOT NULL,
    cell_id text NOT NULL REFERENCES control.cells(id),
    routing_epoch bigint NOT NULL DEFAULT 1 CHECK (routing_epoch > 0),
    policy_version bigint NOT NULL DEFAULT 1 CHECK (policy_version > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'moving')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tenants_cell_idx ON control.tenants(cell_id, status);
CREATE TABLE control.memberships (
    tenant_id text NOT NULL REFERENCES control.tenants(id),
    user_id text NOT NULL REFERENCES control.users(id),
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX memberships_user_idx ON control.memberships(user_id, tenant_id);
CREATE TABLE control.sessions (
    digest text PRIMARY KEY,
    user_id text NOT NULL REFERENCES control.users(id),
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_expiry_idx ON control.sessions(expires_at);
CREATE TABLE control.api_tokens (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    digest text NOT NULL UNIQUE,
    name text NOT NULL,
    permissions text[] NOT NULL,
    policy_version bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, user_id) REFERENCES control.memberships(tenant_id, user_id)
);
CREATE INDEX tokens_tenant_idx ON control.api_tokens(tenant_id, user_id);
CREATE TABLE control.login_attempts (
    email_digest text PRIMARY KEY,
    failures integer NOT NULL DEFAULT 0,
    window_started timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE control.audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    actor_id text NOT NULL,
    action text NOT NULL,
    target_id text NOT NULL,
    request_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_tenant_idx ON control.audit(tenant_id, id);
