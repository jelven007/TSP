from datetime import date, datetime

import polars as pl
import pytest

from app.instrument_suspension import RELATIVE_PATH, SCHEMA
from app.services.selection_suspension_repair import merge_events, repair_suspensions
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 12)


def event(symbol="600185.SH", suspended=True):
    return {"symbol": symbol, "effective_date": DAY, "announced_on": date(2026, 11, 11),
            "announced_at": datetime(2026, 11, 11, 18, 41, 26), "is_suspended": suspended,
            "source_url": "https://www.sse.com.cn/disclosure/test.pdf"}


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "instruments/instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["600001.SH", "600185.SH"], "name": ["one", "two"]}).write_parquet(path)
    path = tmp_path / "kline_daily/date=2026-11-12/part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600001.SH"], "date": [DAY], "close": [10.]}).write_parquet(path)
    repository = KlineRepository(DataStore(tmp_path))
    yield repository
    repository.db.close()


def test_missing_daily_halt_is_independently_verified_and_cache_changes(repo):
    assert repo.get_suspension_history().is_empty()
    calls = []
    def lookup(symbol, day, **kwargs):
        calls.append(symbol)
        return {"events": [event()], "diagnostics": []}
    result = repair_suspensions(repo, DAY, lookup=lookup, check_cancelled=lambda: None)
    assert calls == ["600185.SH"]
    assert result["added"] == 1 and result["remaining_symbols"] == []
    assert repo.get_suspension_history()["symbol"].to_list() == ["600185.SH"]
    assert repair_suspensions(repo, DAY, lookup=lookup, check_cancelled=lambda: None)["added"] == 0


def test_unknown_and_source_failure_leave_missing_daily_unresolved(repo):
    result = repair_suspensions(repo, DAY, lookup=lambda *a, **kw:
                               {"events": [], "diagnostics": ["unknown"]}, check_cancelled=lambda: None)
    assert result["remaining_symbols"] == ["600185.SH"]
    assert not (repo.store.data_dir / RELATIVE_PATH).exists()
    def unavailable(*a, **kw):
        raise RuntimeError("source unavailable")
    result = repair_suspensions(repo, DAY, lookup=unavailable, check_cancelled=lambda: None)
    assert "source unavailable" in str(result["diagnostics"])
    assert result["remaining_symbols"] == ["600185.SH"]


def test_event_merge_preserves_legacy_history_and_is_idempotent(repo):
    old = {**event("600002.SH"), "effective_date": date(2026, 11, 10)}
    path = repo.store.data_dir / RELATIVE_PATH
    path.parent.mkdir()
    pl.DataFrame([{k: v for k, v in old.items() if k in SCHEMA}], schema=SCHEMA).write_parquet(path)
    assert merge_events(repo, [event()]) == 1
    first = path.read_bytes()
    assert merge_events(repo, [event()]) == 0
    assert first == path.read_bytes()
    assert repo.get_suspension_history().height == 2


def test_conflict_never_overwrites_saved_evidence(repo):
    merge_events(repo, [event()])
    path = repo.store.data_dir / RELATIVE_PATH
    original = path.read_bytes()
    with pytest.raises(ValueError, match="冲突"):
        merge_events(repo, [event(suspended=False)])
    assert path.read_bytes() == original


def test_resume_event_is_verified_then_missing_price_remains_for_real_backfill(repo):
    old = {**event(), "effective_date": date(2026, 11, 10)}
    merge_events(repo, [old])
    result = repair_suspensions(repo, DAY, lookup=lambda *a, **kw:
                               {"events": [event(suspended=False)], "diagnostics": []},
                               check_cancelled=lambda: None)
    assert result["added"] == 1 and result["remaining_symbols"] == ["600185.SH"]
    assert repo.get_suspension_history().height == 2


def test_late_event_and_lookup_budget_cannot_manufacture_completeness(repo):
    late = {**event(), "announced_on": DAY, "announced_at": datetime(2026, 11, 12, 22)}
    result = repair_suspensions(repo, DAY, lookup=lambda *a, **kw:
                               {"events": [late], "diagnostics": []}, check_cancelled=lambda: None)
    assert result["added"] == 0 and result["remaining_symbols"] == ["600185.SH"]
    result = repair_suspensions(repo, DAY, lookup=lambda *a, **kw: pytest.fail("budget exceeded"),
                               max_symbols=0, check_cancelled=lambda: None)
    assert result["deferred_symbols"] == ["600185.SH"]


def test_cancelled_probe_writes_no_evidence(repo):
    class CancelledError(Exception):
        pass
    def cancelled():
        raise CancelledError
    with pytest.raises(CancelledError):
        repair_suspensions(repo, DAY, lookup=lambda *a, **kw: {"events": [event()]},
                           check_cancelled=cancelled)
    assert not (repo.store.data_dir / RELATIVE_PATH).exists()


def test_missing_daily_planner_excludes_only_independently_known_halts(repo, monkeypatch):
    from app.market_time import CN_TZ
    from app.services import kline_gaps
    symbols = ["600001.SH", "600185.SH", "000001.SZ", "300001.SZ", "688001.SH"]
    path = repo.store.data_dir / "instruments/instruments.parquet"
    pl.DataFrame({"symbol": symbols, "name": symbols}).write_parquet(path)
    daily = repo.store.data_dir / "kline_daily/date=2026-11-12/part.parquet"
    pl.DataFrame({"symbol": [s for s in symbols if s != "600185.SH"],
                  "date": [DAY] * 4, "close": [10.] * 4}).write_parquet(daily)
    monkeypatch.setattr(kline_gaps.market_time, "cn_now",
                        lambda: datetime(2026, 11, 12, 20, tzinfo=CN_TZ))
    monkeypatch.setattr("app.services.kline_periods.require_period_provider",
                        lambda *a: pytest.fail("verified halt has no daily to backfill"))
    merge_events(repo, [event()])
    result = kline_gaps.sync_missing(repo, "1d", DAY, DAY, capset=object())
    assert result["remaining"] == 0 and result["requests"] == 0
    assert result["suspended_skipped"] == 1


def test_parallel_event_merges_preserve_all_writers(repo):
    from concurrent.futures import ThreadPoolExecutor
    symbols = [f"600{number:03}.SH" for number in range(20)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda symbol: merge_events(repo, [event(symbol)]), symbols)) == 20
    assert set(repo.get_suspension_history()["symbol"]) == set(symbols)


def test_next_attempt_rotates_probes_instead_of_starving_deferred_symbols(repo):
    checked = []
    symbols = [f"600{number:03}.SH" for number in range(40)]
    pl.DataFrame({"symbol": symbols, "name": symbols}).write_parquet(
        repo.store.data_dir / "instruments/instruments.parquet")
    def lookup(symbol, day, **kwargs):
        checked.append(symbol)
        return {"events": [], "diagnostics": ["unresolved"]}
    first = repair_suspensions(repo, DAY, lookup=lookup, check_cancelled=lambda: None)
    second = repair_suspensions(repo, DAY, lookup=lookup, offset=32, check_cancelled=lambda: None)
    assert first["deferred_symbols"] and second["deferred_symbols"]
    assert set(checked) == set(symbols) - {"600001.SH"}
