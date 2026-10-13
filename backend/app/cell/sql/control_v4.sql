ALTER TABLE control.cells ADD COLUMN tenant_limit integer NOT NULL DEFAULT 1000 CHECK (tenant_limit > 0);
ALTER TABLE control.cells ADD COLUMN realtime_limit integer NOT NULL DEFAULT 200 CHECK (realtime_limit > 0);
ALTER TABLE control.tenants ADD COLUMN realtime_reservation integer NOT NULL DEFAULT 0 CHECK (realtime_reservation >= 0);
-- Placement remains fixed. These reservations govern new provisioning only.
