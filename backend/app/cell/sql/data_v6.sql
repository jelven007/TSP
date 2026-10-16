-- Bounded research discovery and keyset history access for the 100-user phase.
ALTER TABLE scheduler.work DROP CONSTRAINT work_kind_check,
    ADD CHECK (kind IN ('realtime','strategies','notifications','automation','scheduled','research'));
INSERT INTO scheduler.slots(kind,slot)
    SELECT 'research',generate_series(1,4);

CREATE INDEX full_paper_orders_history_idx
    ON business.full_paper_orders(
        tenant_id,account_id,(coalesce(value->>'created_at','')) DESC,id DESC
    );

CREATE FUNCTION scheduler.research_wake() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE tenant text; next_due timestamptz;
BEGIN
    tenant := CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    PERFORM set_config('app.tenant_id',tenant,true);
    SELECT min(CASE WHEN state='queued' THEN clock_timestamp() ELSE lease_until END)
        INTO next_due
        FROM business.research_jobs
        WHERE tenant_id=tenant AND state IN ('queued','running','cancelling');
    INSERT INTO scheduler.work(tenant_id,kind,due_at)
        VALUES(tenant,'research',next_due)
        ON CONFLICT(tenant_id,kind) DO UPDATE SET
            due_at=excluded.due_at,revision=scheduler.work.revision+1;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION scheduler.research_wake() FROM PUBLIC;
CREATE TRIGGER research_jobs_wake
    AFTER INSERT OR DELETE OR UPDATE OF state,lease_until ON business.research_jobs
    FOR EACH ROW EXECUTE FUNCTION scheduler.research_wake();

-- The migration owner temporarily reads all rows only for this bounded backfill.
ALTER TABLE business.research_jobs NO FORCE ROW LEVEL SECURITY;
INSERT INTO scheduler.work(tenant_id,kind,due_at)
    SELECT tenant_id,'research',
           min(CASE WHEN state='queued' THEN now() ELSE lease_until END)
    FROM business.research_jobs
    WHERE state IN ('queued','running','cancelling')
    GROUP BY tenant_id
    ON CONFLICT(tenant_id,kind) DO UPDATE SET
        due_at=excluded.due_at,revision=scheduler.work.revision+1;
ALTER TABLE business.research_jobs FORCE ROW LEVEL SECURITY;
