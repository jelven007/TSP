-- Deleting a complete simulated account deletes its owned ledger. Runtime
-- still cannot update or delete individual fills; RLS remains enforced.
ALTER TABLE business.full_paper_orders
    DROP CONSTRAINT full_paper_orders_tenant_id_account_id_fkey,
    ADD FOREIGN KEY (tenant_id,account_id)
        REFERENCES business.full_paper_accounts(tenant_id,id) ON DELETE CASCADE;
ALTER TABLE business.full_paper_fills
    DROP CONSTRAINT full_paper_fills_tenant_id_account_id_fkey,
    ADD FOREIGN KEY (tenant_id,account_id)
        REFERENCES business.full_paper_accounts(tenant_id,id) ON DELETE CASCADE;
ALTER TABLE business.full_paper_nav
    DROP CONSTRAINT full_paper_nav_tenant_id_account_id_fkey,
    ADD FOREIGN KEY (tenant_id,account_id)
        REFERENCES business.full_paper_accounts(tenant_id,id) ON DELETE CASCADE;

-- Imported originals are read-only for runtime. This narrow trigger removes
-- only the deleted account's originals without granting general archive DELETE.
CREATE FUNCTION business.delete_paper_originals() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE prefix text;
BEGIN
    prefix := 'paper/accounts/' || OLD.id || '/';
    DELETE FROM business.legacy_artifacts
        WHERE tenant_id=OLD.tenant_id
          AND (left(path,length(prefix))=prefix
               OR (OLD.id='default'
                   AND (path IN ('paper/account.json','paper/fills.jsonl','paper/positions.json')
                        OR path LIKE 'paper/orders/%' OR path LIKE 'paper/nav/%'
                        OR path LIKE 'paper/auto_rules/%')));
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION business.delete_paper_originals() FROM PUBLIC;
CREATE TRIGGER delete_paper_originals AFTER DELETE ON business.full_paper_accounts
    FOR EACH ROW EXECUTE FUNCTION business.delete_paper_originals();
