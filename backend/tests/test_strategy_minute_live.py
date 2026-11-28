from datetime import datetime, timedelta
from threading import Event, Thread
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.minute_write_order import VERSION_COLUMN

AT = datetime(2026, 11, 27, 14, 45)
A, B = "600001.SH", "600002.SH"


def bars(*rows):
    return pl.DataFrame(rows, schema=["symbol", "datetime", "close"], orient="row")


def test_completed_rows_reach_strategy_before_partition_lock_releases(tmp_path, monkeypatch):
    from app.services import kline_sync, minute_refresh
    from app.services.minute_refresh import MinuteRefreshService

    monkeypatch.setenv("TSP_STRATEGY_MINUTE_LIVE", "active")
    monkeypatch.setattr(minute_refresh, "cn_now", lambda: AT + timedelta(seconds=1))
    monkeypatch.setattr("app.services.strategy_minute_live.cn_now", lambda: AT + timedelta(seconds=1))
    service = MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(service, "active_provider", lambda: "tickflow")
    monkeypatch.setattr(service, "_resolve_custom", lambda: (None, "tickflow"))
    monkeypatch.setattr(service, "_select_mode", lambda: "increment")
    monkeypatch.setattr(kline_sync, "fetch_intraday_universe_increment",
                        lambda: (bars((A, AT, 10.), (B, AT + timedelta(minutes=1), 11.)), 1))
    entered, unblock, finished = Event(), Event(), Event()
    real_write = kline_sync._write_minute_partition

    def blocked(*args, **kwargs):
        entered.set()
        assert unblock.wait(5)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(kline_sync, "_write_minute_partition", blocked)

    def run():
        service._run_round()
        finished.set()

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        live = service.live_minutes([A, B], AT, AT)
        assert live.select("symbol", "close").rows() == [(A, 10.)]
        assert not list(tmp_path.rglob("part.parquet"))
        assert not finished.is_set()
    finally:
        unblock.set()
        thread.join(5)
    assert finished.is_set()


def test_live_rows_keep_request_order_day_source_and_capacity(monkeypatch):
    from app.services.strategy_minute_live import LiveMinutes, merge_minutes

    tape = LiveMinutes(max_rows=2)
    monkeypatch.setattr("app.services.strategy_minute_live.cn_now", lambda: AT)
    prev = AT - timedelta(minutes=1)
    tape.offer("source", 2, bars((A, AT, 10.), (A, prev, 8.), (B, AT, 11.)))
    tape.offer("source", 1, bars((A, AT, 2.)))
    tape.offer("source", 2, bars((A, AT, 5.)))
    live = tape.read("source", [A, B], prev, AT)
    assert live.select("symbol", "close").sort("symbol").rows() == [(A, 10.), (B, 11.)]
    archive = bars((A, AT, 12.), (B, AT, 2.)).with_columns(
        pl.Series(VERSION_COLUMN, [3, 1], dtype=pl.UInt64))
    merged = merge_minutes(archive, live)
    assert merged.select("symbol", "close").rows() == [(A, 12.), (B, 11.)]
    assert tape.read("different-source", [A], prev, AT).is_empty()
    assert tape.status()["capacity_evicted_rows"] == 1
    monkeypatch.setattr("app.services.strategy_minute_live.cn_now", lambda: AT + timedelta(days=1))
    assert tape.read("source", [A], prev, AT).is_empty()


def test_shadow_receipt_distinguishes_early_missing_conflict_and_legal_revision():
    from app.services.strategy_minute_live import shadow_report

    before = AT - timedelta(minutes=1)
    published = bars((A, AT, 9.), (A, before, 8.), (B, before, 12.)).with_columns(
        pl.Series(VERSION_COLUMN, [3, 2, 1], dtype=pl.UInt64))
    live = bars((A, before, 7.), (A, AT, 9.), (B, before, 13.), (B, AT, 14.)).with_columns(
        pl.Series(VERSION_COLUMN, [2, 3, 2, 2], dtype=pl.UInt64))
    result = shadow_report(published, live, [A, B], [before, AT])
    assert result["same_sequence_conflicts"] == 1
    assert result["matching_keys"] == 1 and result["newer_keys"] == 1 and result["live_only_keys"] == 1
    assert result["published_missing_required"] == 1 and result["candidate_missing_required"] == 0
    assert result["published_sha256"] != result["candidate_sha256"]
    assert result == shadow_report(published.reverse(), live.reverse(), [B, A], [AT, before])


def test_shadow_probe_is_bounded_and_never_waits_for_transport(monkeypatch):
    from app.services.strategy_minute_live import ShadowMinuteProbe

    monkeypatch.setenv("TSP_STRATEGY_MINUTE_LIVE", "shadow")
    sid = "custom_mvxxcs03"
    payload = {
        "public_release_id": "a" * 32,
        "strategy_task_plans": {sid: {"actions": [{"kind": "buy", "phase": "stocks", "signal_id": "entry"}]}},
        "strategy_task_windows": {sid: {"buy": [{"start": "09:32", "signal": {"id": "entry"}}]}},
        "selection_snapshots": {sid: {"rows": [{"symbol": A}]}},
    }
    entered, leave = Event(), Event()

    def read(*_a):
        entered.set()
        assert leave.wait(4)
        return {"state": "shadow"}

    probe = ShadowMinuteProbe()
    try:
        market = SimpleNamespace(strategy_minute=read)
        assert probe.offer(payload, market, AT)
        assert entered.wait(4)
        assert not probe.offer(payload, market, AT)
    finally:
        leave.set()
        probe.close()


@pytest.mark.parametrize("sid", ["custom_mvxxcs03", "custom_mvxxcs05"])
def test_buy_scope_is_frozen_and_partial_packet_keeps_published_behavior(sid):
    from app.services.strategy_minute_live import scoped_windows, use_scoped_minutes

    payload = {
        "strategy_task_plans": {sid: {"actions": [{"kind": "buy", "phase": "stocks", "signal_id": "entry"}]}},
        "strategy_task_windows": {sid: {"buy": [{"start": "09:32", "signal": {"id": "entry"}}]}},
        "selection_snapshots": {sid: {"rows": [{"symbol": A}, {"symbol": B}]}},
    }
    start = AT.replace(hour=9, minute=31)
    end = start + timedelta(minutes=1)
    scope = scoped_windows(payload, sid, end)["buy"]
    assert scope == {"symbols": [A, B], "start": start.isoformat(), "end": end.isoformat()}
    published = bars((A, start, 7.), (B, start, 9.)).with_columns(
        pl.lit(1, dtype=pl.UInt64).alias(VERSION_COLUMN))
    partial = bars((A, end, 8.)).with_columns(pl.lit(2, dtype=pl.UInt64).alias(VERSION_COLUMN))
    assert use_scoped_minutes(published, partial, scope) is None
    complete = pl.concat([partial, bars((B, end, 10.)).with_columns(
        pl.lit(2, dtype=pl.UInt64).alias(VERSION_COLUMN))])
    assert use_scoped_minutes(published, complete, scope).height == 4
    payload["strategy_task_windows"][sid]["buy"] = []  # 无当日买分支
    assert "buy" not in scoped_windows(payload, sid, end)


def test_sell_tape_uses_original_due_branch_members_only():
    from app.services.strategy_minute_live import scoped_windows

    sid = "custom_mvxxcs05"
    payload = {
        "strategy_task_plans": {sid: {"actions": [{"kind": "sell", "phase": "observe", "signal_id": "exit-a"}]}},
        "strategy_task_windows": {sid: {"sell": [
            {"signal": {"id": "exit-a"}, "symbols": [A]},
            {"signal": {"id": "exit-b"}, "symbols": [B]},
        ]}},
        "sell_rosters": {sid: {"rows": [{"symbol": A}, {"symbol": B}]}},
    }
    assert scoped_windows(payload, sid, AT)["sell"]["symbols"] == [A]
    payload["sell_rosters"][sid] = None
    assert scoped_windows(payload, sid, AT) == {}


def test_authenticated_pinned_packet_and_compute_exact_cohort(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.cell.platform_market import router
    from app.services.kline_sync import _write_minute_partition
    from app.services.strategy_minute_live import ScopedMinuteRepository
    from app.tickflow.repository import KlineRepository

    opening = AT.replace(hour=9, minute=31)
    next_bar = opening + timedelta(minutes=1)
    release = "e" * 32
    directory = tmp_path / "releases" / release
    # The on-disk base has the opening minute; a second bar for A and a newer
    # sequence on A's opening come from the same still unarchived response.
    _write_minute_partition(bars((A, opening, 10.), (B, opening, 12.)),
                            directory / "kline_minute", request_seq=2)
    repo = KlineRepository.__new__(KlineRepository)
    closes = []
    repo.store = SimpleNamespace(data_dir=directory, _public_release_finalizer=lambda: closes.append(True))
    monkeypatch.setattr(repo, "get_minute_by_dates", lambda *_a, **_kw: bars((B, opening, 12.)))
    monkeypatch.setattr(repo, "_maybe_adjust_minute", lambda frame, _: frame)

    def open_pinned(path, *, bounded):
        assert bounded and path == directory
        return repo

    monkeypatch.setattr("app.multitenancy.public_reader.open_public_repository", open_pinned)
    monkeypatch.setattr("app.market_time.cn_now", lambda: next_bar)
    monkeypatch.setattr("app.services.strategy_minute_live.cn_now", lambda: next_bar)
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "fixture")
    monkeypatch.setenv("TSP_STRATEGY_MINUTE_LIVE", "active")
    app = FastAPI()
    app.include_router(router)
    app.state.public_publisher = SimpleNamespace(root=tmp_path)
    app.state.minute_refresh = SimpleNamespace(live_minutes=lambda *_a: bars(
        (A, opening, 9.), (A, next_bar, 11.)).with_columns(
        pl.Series(VERSION_COLUMN, [1, 3], dtype=pl.UInt64)))
    client = TestClient(app)
    scope = {"symbols": [A], "start": opening.isoformat(), "end": next_bar.isoformat()}
    request = {"release_id": release, "scope": scope}
    path = "/internal/cell-market/strategy-minute"
    assert client.post(path, json=request).status_code == 401
    headers = {"Authorization": "Bearer fixture"}
    assert client.post(path, json={**request, "scope": {**scope, "end": AT.isoformat()}},
                       headers=headers).status_code == 422
    result = client.post(path, json=request, headers=headers)
    assert result.status_code == 200 and closes == [True]
    packet = result.json()
    assert packet["state"] == "ready"
    adapter = ScopedMinuteRepository(repo, {"buy": packet}, release, {"buy": scope})
    assert adapter.get_minute_by_dates([A], [opening.date()])["close"].to_list() == [10., 11.]
    assert adapter.get_minute_by_dates([B], [opening.date()])["close"].to_list() == [12.]
    wrong_release = ScopedMinuteRepository(repo, {"buy": packet}, "a" * 32, {"buy": scope})
    assert wrong_release.get_minute_by_dates([A], [opening.date()])["close"].to_list() == [12.]
    monkeypatch.setenv("TSP_STRATEGY_MINUTE_LIVE", "shadow")
    shadow = client.post(path, json=request, headers=headers).json()
    assert shadow["state"] == "shadow" and "frame" not in shadow
    assert shadow["comparison"]["candidate_missing_required"] == 0
