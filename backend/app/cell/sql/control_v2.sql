CREATE TABLE control.strategy_catalog (
    id text PRIMARY KEY,
    definition jsonb NOT NULL,
    code_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
