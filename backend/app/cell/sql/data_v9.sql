-- Monitoring changes wake the same tenant's nightly work in their transaction.
CREATE FUNCTION scheduler.monitor_documents_wake() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE tenant text;
BEGIN
    IF (CASE WHEN TG_OP='DELETE' THEN OLD.namespace ELSE NEW.namespace END)!='strategy_monitoring' THEN
        RETURN NULL;
    END IF;
    tenant := CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    INSERT INTO scheduler.work(tenant_id,kind,due_at) VALUES(tenant,'scheduled',clock_timestamp())
      ON CONFLICT(tenant_id,kind) DO UPDATE SET
        due_at=least(scheduler.work.due_at,excluded.due_at),revision=scheduler.work.revision+1;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION scheduler.monitor_documents_wake() FROM PUBLIC;
CREATE TRIGGER strategy_monitoring_wake AFTER INSERT OR UPDATE OR DELETE ON business.documents
    FOR EACH ROW EXECUTE FUNCTION scheduler.monitor_documents_wake();

-- A restarted worker checks delayed work immediately, without changing a lease
-- or discovering private documents across tenants. NULL means no pending work.
CREATE FUNCTION scheduler.resume_scheduled() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
    UPDATE scheduler.work SET due_at=clock_timestamp(),revision=revision+1
      WHERE kind='scheduled' AND due_at>clock_timestamp()
        AND (lease_until IS NULL OR lease_until<clock_timestamp());
$$;
REVOKE ALL ON FUNCTION scheduler.resume_scheduled() FROM PUBLIC;
