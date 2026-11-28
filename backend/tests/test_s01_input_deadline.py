"""S01's two phases use a common identity and prepare before confirmation."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
from test_strategy_input_manifest import SYMBOLS, request

from app.market_time import CN_TZ
from app.services.strategy_input_manifest import S01_ID, common_key, normalize_request

AT = datetime(2026, 11, 27, 14, 45, tzinfo=CN_TZ)
SIGNAL = "late_session_strength_s01"
OPTIONAL = ("gap_max_pct", "position_min_pct", "return_5m_min_pct", "return_15m_min_pct")


def s01_request():
    req = request()
    return {**req, "strategy_id": S01_ID, "as_of": "2026-11-27", "reference_date": "2026-11-26",
            "bar_at": AT.replace(tzinfo=None).isoformat(), "retry_until": "2026-11-27T14:55:00+08:00",
            "signal": {"id": SIGNAL, "params": {"time": "14:45:00", "pool_up_min_pct": 65,
                        "evaluation_delay_minutes": 1, "ma5_1m_count": None, "ma5_5m_count": None}},
            "selection": {**req["selection"], "as_of": "2026-11-27", "source_date": "2026-11-26"}}


def test_s01_input_request_collapses_only_equivalent_null_options():
    clean = s01_request()
    filled = s01_request()
    filled["signal"]["params"].update(dict.fromkeys(OPTIONAL))
    assert common_key(normalize_request(filled)) == common_key(normalize_request(clean))
    assert normalize_request(filled)["signal"] == normalize_request(clean)["signal"]
    # Previous input files with an explicit-null key remain readable by raw identity.
    assert common_key(filled) != common_key(clean)
    filled["signal"]["params"]["gap_max_pct"] = 1
    assert common_key(normalize_request(filled)) != common_key(normalize_request(clean))
    filled["signal"]["params"].pop("gap_max_pct")
    filled["signal"]["params"].pop("pool_up_min_pct")
    clean["signal"]["params"]["pool_up_min_pct"] = None
    assert common_key(normalize_request(filled)) != common_key(normalize_request(clean))


def test_breadth_admission_stages_both_s01_inputs_without_working_for_the_worker(tmp_path, monkeypatch):
    from app.services.strategy_input_authority import S02InputService

    at = AT + timedelta(seconds=15)
    monkeypatch.setattr("app.market_time.cn_now", lambda: at)
    monkeypatch.setattr("app.services.strategy_input_binding.algorithm_sha256", lambda: "b" * 64)
    service = S02InputService(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))
    jobs = []
    monkeypatch.setattr(service.pool, "submit", lambda fn, req, phase: jobs.append((common_key(req), phase)))
    try:
        req = s01_request()
        assert service.request(req, "breadth")["state"] == "waiting_input"
        assert service.request(req, "breadth")["state"] == "waiting_input"
        assert jobs == [(common_key(req), "stocks"), (common_key(req), "breadth")]
    finally:
        service.close()


def test_priority_s01_minute_only_repair_bypasses_busy_pipeline_and_publishes_once(tmp_path, monkeypatch):
    from app.services import kline_periods, kline_sync, monitor_input_repair, pipeline_jobs

    calls = []
    symbols = [f"{n:06d}.SH" for n in range(600001, 600205)]
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    publisher = SimpleNamespace(publish_priority=lambda at: calls.append(("publish", at)) or {"id": "new"})
    service = monitor_input_repair.MonitorInputRepair(
        SimpleNamespace(repo=repo, public_publisher=publisher), now=lambda: AT + timedelta(seconds=17))
    req = {"owner": "s02-" + "0" * 64, "strategy_id": S01_ID, "day": "2026-11-27", "phase": "breadth",
           "generation": 1, "minute_start": "14:45:00", "minute_end": "14:45:00", "daily_dates": [],
           "retry_until": "2026-11-27T14:55:00+08:00", "certify_coverage": True, "symbols": symbols}
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda _: False)
    monkeypatch.setattr(kline_periods, "require_period_provider", lambda *_: (object(), "test"))
    monkeypatch.setattr(kline_periods, "fetch_period", lambda p, kind, wanted, start, end, **kw:
        calls.append(("fetch", len(wanted), start, end)) or pl.DataFrame({
            "datetime": [AT.replace(tzinfo=None)] * len(wanted), "symbol": wanted, "close": [10.] * len(wanted)}))
    monkeypatch.setattr(kline_sync, "_write_minute_partition", lambda frame, *_args, **kw:
        calls.append(("write", frame.height, kw.get("request_seq"))) or frame.height)
    try:
        target = service._priority_target(req)
        assert target == AT + timedelta(minutes=1)
        service._run("test-repair", {"id": "test-repair", "request": req, "successes": 0, "result": {}}, target)
        receipt = service._read("test-repair")
        assert receipt["state"] == "succeeded"
        assert [c[0] for c in calls] == ["fetch", "write", "publish"]
        assert calls[0][1:] == (len(symbols), AT.replace(tzinfo=None), AT.replace(tzinfo=None))
        assert len(receipt["result"]["proof"]) == len(symbols)
    finally:
        service.close()


def test_s01_publisher_seals_both_phases_before_confirm_without_another_worker_request(tmp_path, monkeypatch):
    from app.services.strategy_input_authority import S02InputService
    from app.services.strategy_input_manifest import digest
    from app.services.strategy_input_source import verify_source

    clock = [AT + timedelta(seconds=16)]
    monkeypatch.setattr("app.market_time.cn_now", lambda: clock[0])
    symbols = SYMBOLS
    req = normalize_request(s01_request())
    at = AT.replace(tzinfo=None)
    full = pl.DataFrame({"symbol": symbols, "datetime": [at] * len(symbols),
                         "open": [10.] * len(symbols), "high": [11.] * len(symbols),
                         "low": [9.] * len(symbols), "close": [10.] * len(symbols),
                         "amount": [1e6] * len(symbols)})
    references = dict.fromkeys(symbols, 10.)
    raw = full.select("symbol", "open", "high", "low", "close").with_columns(
        pl.lit(datetime.fromisoformat("2026-11-26").date()).alias("date"))
    factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64})
    evidence, errors = verify_source(req, raw, factors)
    evidence["reference_daily"] = raw.to_dicts()
    evidence["reference_daily_sha256"] = digest(evidence["reference_daily"])
    evidence["reference_errors"] = errors
    evidence["daily_reference_errors"] = {}
    service = S02InputService(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))

    def read(_req, _phase):
        return {"minutes": full if clock[0].second > 16 else full.clear(), "references": references,
                "reference_errors": errors, "source_evidence": evidence, "coverage": {}}

    def publish(_delay):
        clock[0] += timedelta(seconds=14)
        return False

    monkeypatch.setattr(service, "_inputs", read)
    monkeypatch.setattr(service, "_repair", lambda *_: [])
    monkeypatch.setattr(service.stop_wait, "wait", publish)
    try:
        key = common_key(req)
        for phase in ("stocks", "breadth"):
            service._advance(req, phase)
            assert service.store.read_key(key, phase)["state"] == "ready"
            receipt = service.status[key, phase]
            assert receipt["sealed_before_target"] is True
            assert receipt["sealed_at"] == "2026-11-27T14:45:30+08:00"
    finally:
        service.close()


def test_s01_late_priority_response_never_writes_or_publishes(tmp_path, monkeypatch):
    from app.services import kline_periods, kline_sync, monitor_input_repair

    clock = [AT + timedelta(seconds=17)]
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    service = monitor_input_repair.MonitorInputRepair(SimpleNamespace(repo=repo), now=lambda: clock[0])
    req = {"owner": "s02-" + "0" * 64, "strategy_id": S01_ID, "day": "2026-11-27", "phase": "breadth",
           "generation": 1, "minute_start": "14:45:00", "minute_end": "14:45:00", "daily_dates": [],
           "retry_until": "2026-11-27T14:55:00+08:00", "certify_coverage": True, "symbols": SYMBOLS}

    def fetch(*_args, **_kwargs):
        clock[0] = AT + timedelta(seconds=60)
        return pl.DataFrame()

    monkeypatch.setattr(kline_periods, "require_period_provider", lambda *_: (object(), "test"))
    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    monkeypatch.setattr(kline_sync, "_write_minute_partition", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("late repair wrote market data")))
    try:
        service._run("late-repair", {"id": "late-repair", "request": req, "successes": 0, "result": {}},
                     service._priority_target(req))
        result = service._read("late-repair")
        assert result["state"] == "failed" and "封存目标" in result["error"]
    finally:
        service.close()


def test_s01_before_target_fetches_only_missing_target_minute_in_small_groups(tmp_path, monkeypatch):
    from app.services.strategy_input_authority import S02InputService

    clock = [AT + timedelta(seconds=17)]
    monkeypatch.setattr("app.market_time.cn_now", lambda: clock[0])
    requests = []
    service = S02InputService(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        monitor_input_repair=SimpleNamespace(request=lambda req: requests.append(req))))
    req = normalize_request(s01_request())
    stamp, older = (dt.replace(tzinfo=None).isoformat() for dt in (AT, AT - timedelta(minutes=1)))

    def pending(*minutes, days=()):
        return {"state": "pending", "missing_reference": False,
                "missing_minutes": minutes, "missing_daily_dates": days}

    try:
        breadth = {"coverage": {f"{n:06d}.SH": pending(stamp) for n in range(600000, 600300)}}
        service._repair(req, "breadth", breadth)
        assert requests == []
        stocks = {"coverage": {"600000.SH": pending(stamp, older), "600001.SH": pending(older),
                               "600002.SH": pending(stamp, days=("2026-11-26",))}}
        service._repair(req, "stocks", stocks)
        assert [(r["symbols"], r["minute_start"], r["minute_end"], r["daily_dates"]) for r in requests] == [
            (["600000.SH"], "14:45:00", "14:45:00", []),
        ]
        requests.clear()
        clock[0] = AT + timedelta(minutes=1)
        service._repair(req, "breadth", breadth)
        assert len(requests) == 1 and len(requests[0]["symbols"]) == 300
    finally:
        service.close()
