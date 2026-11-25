from app.services.s02_run_evidence import review_provenance


def test_manual_history_keeps_original_archive_and_next_day_sell_roster(tmp_path, monkeypatch):
    import gzip
    import json
    from datetime import datetime

    from app.market_time import CN_TZ
    from app.services import strategy_results

    sid, day = "custom_mvxxcp88", "2026-11-24"
    identity = strategy_results.ResultIdentity(sid, "s02", "v1", "{}", "{}", "null", "stock", "1m")
    store = strategy_results.StrategyResults(tmp_path)
    original = store.save(identity, {
        "as_of": day, "section_mode": "signals", "total": 1, "rows": [{"symbol": "ORIGINAL"}],
        "sections": {"sell": [], "select": [], "buy": [{"symbol": "ORIGINAL"}]},
        "section_statuses": {"sell": "success", "select": "success", "buy": "success"},
    }, started_at=1)
    next_day = store.day_view(sid, "2026-11-25")
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 11, 25, 4, 30, tzinfo=CN_TZ))
    reviewed = strategy_results.persist_result(tmp_path, identity, {
        "as_of": day, "section_mode": "signals", "total": 0, "rows": [],
        "sections": {"sell": [], "select": [], "buy": []},
        "section_statuses": {"sell": "success", "select": "success", "buy": "success"},
    }, started_at=2)
    assert reviewed["record_id"] != original["record_id"]
    assert store.detail(sid, original["record_id"]) == original
    assert store.day_view(sid, "2026-11-25") == next_day
    restored = json.loads(gzip.decompress(store.day_view(sid, day)[1]))["result"]
    assert restored["diagnostic_review"]["record_id"] == reviewed["record_id"]
    assert restored["diagnostic_review"]["diagnostic_only"]
    assert restored["original_realtime"]["record_id"] == original["record_id"]


def test_manual_current_day_cutoff_is_inclusive_and_other_strategy_is_unchanged(monkeypatch):
    from datetime import datetime

    from app.market_time import CN_TZ
    from app.services.s02_run_evidence import manual_review

    value = {"as_of": "2026-11-25", "section_mode": "signals"}
    at = datetime(2026, 11, 25, 14, 55, tzinfo=CN_TZ)
    monkeypatch.setattr("app.market_time.cn_now", lambda: at)
    assert manual_review("custom_mvxxcp88", value, 1) is value
    monkeypatch.setattr("app.market_time.cn_now", lambda: at.replace(microsecond=1000))
    assert manual_review("custom_mvxxcp88", value, 1)["repair_provenance"]["diagnostic_only"]
    assert manual_review("other", value, 1) is value
    assert manual_review("custom_mvxxcp88", {**value, "selection_only": True}, 1).get(
        "repair_provenance") is None


def test_review_retains_original_status_and_has_a_separate_diagnostic_identity():
    original = {"record_id": "original", "section_statuses": {"buy": "expired"},
                "section_errors": {"buy": "original cutoff"}, "monitor_tasks": {"buy": {
                    "state": "expired", "terminal": True}},
                "run_evidence": {"phases": {"stocks": {"state": "incomplete"}}}}
    reviewed = {"record_id": "review", "repair_provenance": {
        "kind": "post_close_signal_review_v1"}, "sections": {"buy": []},
        "section_statuses": {"buy": "success"}}
    result = review_provenance(original, reviewed)
    assert result["original_realtime"]["record_id"] == "original"
    assert result["original_realtime"]["buy_status"] == "expired"
    assert result["diagnostic_review"]["record_id"] == "review"
    assert result["diagnostic_review"]["diagnostic_only"] is True
    assert original["section_statuses"]["buy"] == "expired"


def test_old_correction_uses_original_first_receipt_without_rewriting_task():
    from app.services.s02_run_evidence import annotate_diagnostic
    original = {"monitor_tasks": {"buy": {"signals": {"late": {"stocks": {
        "state": "rejected", "first_receipt": {"state": "failed", "errors": ["missing bar"]},
        "repair_provenance": {"kind": "startup_minute_diagnostic_correction_v1", "review_only": True},
    }}}}}}
    result = annotate_diagnostic(original)
    assert result["original_realtime"]["buy_status"] == "failed"
    assert result["diagnostic_review"]["diagnostic_only"]
    assert result["monitor_tasks"]["buy"]["signals"]["late"]["stocks"]["state"] == "rejected"


def test_cached_detail_retains_s02_diagnostic_and_phase_identity(monkeypatch):
    from types import SimpleNamespace

    from app.api import screener
    value = {
        "as_of": "2026-11-24", "rows": [],
        "repair_provenance": {"kind": "s02_manual_signal_review_v1", "diagnostic_only": True},
        "original_realtime": {"record_id": "original", "buy_status": "failed"},
        "diagnostic_review": {"record_id": "review", "diagnostic_only": True},
        "run_evidence": {"phases": {"stocks": {"input_sha256": "same"}}},
        "run_commit": {"phases": {"stocks": {"input_sha256": "same"}}},
        "operational_state": {"issues": [{"kind": "delayed_input"}]},
    }
    monkeypatch.setattr(screener, "_cached_with_realtime", lambda _: {
        "as_of": value["as_of"], "results": {"custom_mvxxcp88": value}})
    monkeypatch.setattr(screener, "_load_ext_value_maps", lambda *_: {})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))
    result = screener.get_cached_result("custom_mvxxcp88", request, ext_columns=None)["result"]
    for key in ("repair_provenance", "original_realtime", "diagnostic_review",
                "run_evidence", "run_commit", "operational_state"):
        assert result[key] == value[key]
