-- Control-plane lookup remains bounded by one tenant per claimed unit of work.
CREATE INDEX tenants_cell_status_idx ON control.tenants(cell_id, status, id);
