"""Retirement facade for callers from older releases; no system task execution."""
from app.multitenancy.research import authorize


def reconcile(worker, documents, claim=None):
    """Old startup hooks may retire records, but can never restore monitoring."""
    from app.multitenancy.runtime import valid_claim
    from app.services.strategy_monitor_tasks import retire_legacy

    account, context = worker.account(documents.context.tenant_id)
    with authorize(worker.identity, context, account["auth_version"]), documents.transaction() as unit:
        valid_claim(unit.connection, claim)
        retire_legacy(unit)


def check_completion(worker, documents, claim=None, *, now=None):
    """Legacy completion inspection is retired; immutable old receipts stay stored."""
    return None
