"""A dated review must remain distinguishable from the original realtime attempt."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_full_entry_expiry import SID, request_for, saved_result
from test_full_entry_expiry import late as late
from test_full_entry_expiry import upgrade as upgrade
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_runtime import owned as owned

from app.api import screener
from app.market_time import cn_today
from app.services.strategy_results import StrategyResults


@pytest.mark.parametrize("invalid", [None, "marker", "buy", "status", "identity"])
def test_postclose_review_keeps_provenance_and_original_expiry(late, monkeypatch, invalid):
    account, identity = late
    store = StrategyResults(account.documents)
    original = store.save(identity, saved_result(identity), started_at=1)
    account.worker.evaluate(account.documents.context.tenant_id,
                            account.snapshot(status={"is_trading_hours": False}))
    attempt = account.documents.get("strategy_execution_status", SID)
    assert attempt["state"] == "expired"
    provenance = {"kind": "post_close_signal_review_v1", "as_of": str(cn_today())}
    review_identity = replace(identity, overrides_json=json.dumps({"data_repair": provenance}))
    review = {**saved_result(identity, buy="success"),
              "repair_provenance": provenance,
              "execution_status": {"state": "success", "reason": "盘后复核; 当天未完成盘中确认",
                                   "updated_at": 123}}
    if invalid == "marker":
        review.pop("repair_provenance")
    elif invalid == "buy":
        review["section_statuses"]["buy"] = "failed"
    elif invalid == "status":
        review["execution_status"]["state"] = "failed"
    elif invalid == "identity":
        review["runtime_identity"] = "other-definition"
    reviewed = store.save(review_identity, review, started_at=2)["result"]
    records = deepcopy(account.documents.records("strategy_result_history"))
    request = request_for(monkeypatch, account.documents)
    shown = screener._with_execution_status(
        request, {SID: reviewed}, str(cn_today()), identities=[identity])[SID]
    if invalid is None:
        assert shown["execution_status"] == reviewed["execution_status"]
        assert shown["section_statuses"]["buy"] == "success"
        assert shown["sections"] == reviewed["sections"]
    else:
        assert shown["execution_status"]["state"] == "expired"
    assert account.documents.get("strategy_execution_status", SID) == attempt
    assert account.documents.records("strategy_result_history") == records
    assert store.detail(SID, original["record_id"])["result"] == original["result"]
