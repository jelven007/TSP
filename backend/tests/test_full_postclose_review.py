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


@pytest.mark.parametrize("invalid", [None, "marker", "buy", "status", "identity", "day", "new_success"])
def test_day_view_retains_valid_review_after_later_expired_scan(late, invalid):
    import gzip

    account, identity = late
    store = StrategyResults(account.documents)
    provenance = {"kind": "post_close_signal_review_v1", "as_of": str(cn_today())}
    review_identity = replace(identity, overrides_json=json.dumps({"data_repair": provenance}))
    review = {**saved_result(identity, buy="success", rows=[{"symbol": "600519.SH"}]),
              "repair_provenance": provenance,
              "execution_status": {"state": "success", "reason": "盘后复核", "updated_at": 123}}
    if invalid == "marker":
        review.pop("repair_provenance")
    elif invalid == "buy":
        review["section_statuses"]["buy"] = "failed"
    elif invalid == "status":
        review["execution_status"]["state"] = "failed"
    elif invalid == "identity":
        review_identity = replace(review_identity, runtime_identity="other-definition")
        review["runtime_identity"] = "other-definition"
    elif invalid == "day":
        review["repair_provenance"]["as_of"] = "2026-10-30"
    reviewed = store.save(review_identity, review, started_at=2)
    latest = saved_result(identity, buy="success" if invalid == "new_success" else "expired")
    latest["sections"]["select"] = [{"symbol": "600001.SH"}]
    latest["sections"]["sell"] = [{"symbol": "600002.SH"}]
    from app.services.strategy_day_views import _key
    account.documents.put("strategy_sell_rosters", _key(SID, str(cn_today()), "stock", "1m"),
                          {"rows": latest["sections"]["sell"],
                           "strategy_version": identity.strategy_version})
    stored = store.save(identity, latest, started_at=3)
    records = deepcopy(account.documents.records("strategy_result_history"))
    shown = json.loads(gzip.decompress(store.day_view(SID, str(cn_today()))[1]))["result"]
    assert shown["record_id"] == stored["record_id"]
    assert shown["sections"]["select"] == latest["sections"]["select"]
    assert shown["sections"]["sell"] == latest["sections"]["sell"]
    if invalid is None:
        assert shown["section_statuses"]["buy"] == "success"
        assert shown["sections"]["buy"] == reviewed["result"]["sections"]["buy"]
        assert shown["execution_status"] == review["execution_status"]
        assert shown["repair_provenance"] == provenance
        assert shown["buy_review_record_id"] == reviewed["record_id"]
        assert "buy" not in shown.get("section_errors", {})
        assert shown["rows"] == shown["sections"]["buy"]
        assert shown["total"] == 1
    else:
        assert shown["section_statuses"]["buy"] == latest["section_statuses"]["buy"]
        assert shown["sections"]["buy"] == latest["sections"]["buy"]
        assert "buy_review_record_id" not in shown
    assert account.documents.records("strategy_result_history") == records
    assert store.detail(SID, reviewed["record_id"])["result"] == reviewed["result"]
