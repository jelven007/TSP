-- Full-product phase one identity. Existing Cell memberships remain a data scope.
ALTER TABLE control.users ALTER COLUMN email DROP NOT NULL;
ALTER TABLE control.users ADD COLUMN username text UNIQUE
    CHECK (username IS NULL OR username ~ '^[a-z][a-z0-9_-]{2,63}$');
ALTER TABLE control.users ADD CONSTRAINT users_login_identifier CHECK (email IS NOT NULL OR username IS NOT NULL);
ALTER TABLE control.users ADD COLUMN default_tenant_id text REFERENCES control.tenants(id);
ALTER TABLE control.users ADD COLUMN auth_version bigint NOT NULL DEFAULT 1 CHECK (auth_version > 0);
ALTER TABLE control.users ADD COLUMN account_state text NOT NULL DEFAULT 'active'
    CHECK (account_state IN ('pending', 'active', 'suspended'));
ALTER TABLE control.sessions ADD COLUMN auth_version bigint NOT NULL DEFAULT 1;
-- Full-product administrators may target a tenant without becoming members.
ALTER TABLE control.api_tokens DROP CONSTRAINT api_tokens_tenant_id_user_id_fkey;
ALTER TABLE control.api_tokens ADD FOREIGN KEY (tenant_id) REFERENCES control.tenants(id);
ALTER TABLE control.api_tokens ADD FOREIGN KEY (user_id) REFERENCES control.users(id);
ALTER TABLE control.api_tokens ADD COLUMN full_scopes text[] NOT NULL DEFAULT '{}';
ALTER TABLE control.api_tokens ADD COLUMN auth_version bigint NOT NULL DEFAULT 0;
ALTER TABLE control.api_tokens ADD COLUMN routing_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE control.api_tokens ADD COLUMN last_used_at timestamptz;

CREATE TABLE control.roles (id text PRIMARY KEY, name text NOT NULL);
CREATE TABLE control.permissions (id text PRIMARY KEY);
CREATE TABLE control.user_roles (
    user_id text NOT NULL REFERENCES control.users(id),
    role_id text NOT NULL REFERENCES control.roles(id),
    PRIMARY KEY (user_id, role_id)
);
CREATE TABLE control.role_permissions (
    role_id text NOT NULL REFERENCES control.roles(id),
    permission_id text NOT NULL REFERENCES control.permissions(id),
    PRIMARY KEY (role_id, permission_id)
);
INSERT INTO control.roles VALUES ('admin', '管理员'), ('tenant', '租户');
INSERT INTO control.permissions VALUES
    ('business:read'), ('business:write'), ('account:manage'),
    ('data:manage'), ('settings:manage'), ('users:manage'), ('tokens:manage');
INSERT INTO control.role_permissions
    SELECT 'admin', id FROM control.permissions;
INSERT INTO control.role_permissions VALUES
    ('tenant', 'business:read'), ('tenant', 'business:write'), ('tenant', 'account:manage');
-- A former owner/admin membership never grants system administration.
INSERT INTO control.user_roles SELECT id, 'tenant' FROM control.users;

CREATE TABLE control.registration_config (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    secret_salt text NOT NULL,
    secret_hash text NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE control.verification_codes (
    email text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'registration'),
    salt text NOT NULL,
    code_hash text NOT NULL,
    secret_version bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    resend_at timestamptz NOT NULL,
    attempts_left integer NOT NULL CHECK (attempts_left BETWEEN 0 AND 5),
    delivered boolean NOT NULL DEFAULT false,
    consumed_at timestamptz,
    PRIMARY KEY (email, purpose)
);
CREATE INDEX verification_expiry_idx ON control.verification_codes(expires_at);
CREATE TABLE control.provisioning_jobs (
    id text PRIMARY KEY,
    user_id text NOT NULL UNIQUE REFERENCES control.users(id),
    tenant_id text NOT NULL UNIQUE REFERENCES control.tenants(id),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'ready')),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE control.rate_limits (
    key text PRIMARY KEY,
    count integer NOT NULL DEFAULT 0,
    window_started timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rate_limits_window_idx ON control.rate_limits(window_started);
CREATE TABLE control.user_preferences (
    user_id text PRIMARY KEY REFERENCES control.users(id),
    value jsonb NOT NULL DEFAULT '{}',
    revision bigint NOT NULL DEFAULT 1
);
CREATE TABLE control.full_worker_health (
    cell_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('research','realtime','notifications','scheduled')),
    owner text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    healthy boolean NOT NULL,
    PRIMARY KEY (cell_id,kind)
);
