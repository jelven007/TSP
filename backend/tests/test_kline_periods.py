from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import kline_periods as periods
from app.tickflow.repository import DataStore, KlineRepository


def test_defaults_and_calendar_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.preferences._path", lambda: tmp_path / "preferences.json")
    configs = periods.get_configs(tmp_path)
    assert [configs[p]["interval_seconds"] for p in ("1m", "5m", "10m", "15m")] == [60, 300, 600, 900]
    assert configs["1w"]["time"] == configs["1d"]["time"] == "16:00"
    cfg = configs["5m"]
    assert cfg["windows"] == [["09:15", "11:45"], ["13:00", "16:00"]]
    for hour, minute, expected in [(9, 15, True), (11, 44, True), (11, 45, False), (12, 0, False), (13, 0, True), (15, 59, True), (16, 0, False)]:
        assert periods.in_sync_window(cfg, datetime(2026, 10, 26, hour, minute), trading_day=True) is expected
    assert not periods.in_sync_window(cfg, datetime(2026, 10, 26, 10), trading_day=False)
    periods.save_config("5m", {
        **cfg,
        "enabled": False,
        "interval_seconds": 420,
        "windows": [["09:15", "11:35"], ["13:00", "15:20"]],
    }, tmp_path)
    saved = periods.get_configs(tmp_path)["5m"]
    assert saved["interval_seconds"] == 420
    assert saved["windows"] == [["09:15", "11:45"], ["13:00", "16:00"]]
    with pytest.raises(ValueError):
        periods.save_config("5m", {**cfg, "windows": [["11:35", "09:15"]]}, tmp_path)


def minute_sample():
    return pl.DataFrame({
        "symbol": ["600000.SH"] * 7,
        "datetime": [datetime(2026, 10, 26, h, m) for h, m in [(9, 31), (9, 40), (9, 41), (11, 30), (13, 1), (13, 10), (13, 11)]],
        "open": [1., 2., 3., 4., 5., 6., 7.],
        "high": [2., 3., 4., 5., 6., 7., 8.],
        "low": [0., 1., 2., 3., 4., 5., 6.],
        "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5],
        "volume": [10.] * 7, "amount": [100.] * 7,
    })


def test_ten_minute_ohlcv_lunch_and_nulls():
    result = periods.aggregate_ten_minute(minute_sample())
    assert result["datetime"].to_list() == [datetime(2026, 10, 26, h, m) for h, m in [(9, 40), (9, 50), (11, 30), (13, 10), (13, 20)]]
    first = result.row(0, named=True)
    assert [first[k] for k in ("open", "high", "low", "close", "volume", "amount")] == [1., 3., 0., 2.5, 20., 200.]
    unknown = minute_sample().with_columns(pl.lit(None, dtype=pl.Float64).alias("volume"))
    assert periods.aggregate_ten_minute(unknown)["volume"].null_count() == 5
    invalid = minute_sample().with_columns(pl.lit(datetime(2026, 10, 26, 12)).alias("datetime"))
    with pytest.raises(ValueError):
        periods.aggregate_ten_minute(invalid)


def test_months_use_calendar_not_thirty_days():
    assert periods.history_start(date(2024, 3, 31), 1, "month") == date(2024, 2, 29)
    assert periods.history_start(date(2024, 2, 29), 1, "year") == date(2023, 2, 28)


def test_persist_isolated_tables_and_idempotent_merge(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    frame = minute_sample()
    for period in ("5m", "10m", "15m"):
        assert periods.persist_period(repo, period, frame) == 7
        periods.persist_period(repo, period, frame)
        assert repo.execute_one(f"SELECT count(*) FROM {periods.TABLES[period]}")[0] == 7
    assert not any((tmp_path / "kline_minute").rglob("*.parquet"))
    weekly = frame.with_columns(pl.col("datetime").dt.date().alias("date")).drop("datetime")
    periods.persist_period(repo, "1w", weekly)
    assert repo.execute_one("SELECT count(*) FROM kline_weekly")[0] == 1

def test_period_write_does_not_invert_the_legacy_repository_partition_locks(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from app.services import kline_sync
    repo = KlineRepository(DataStore(tmp_path))
    original = kline_sync._minute_partition_lock
    inside_partition = [False]

    @contextmanager
    def partition(path):
        with original(path):
            inside_partition[0] = True
            try:
                yield
            finally:
                inside_partition[0] = False

    class RepositoryLock:
        def __enter__(self):
            assert not inside_partition[0], "legacy writers acquire repository before partition"
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(kline_sync, "_minute_partition_lock", partition)
    repo._write_lock = RepositoryLock()
    periods.persist_period(repo, "1m", minute_sample())
    periods.persist_period(repo, "5m", minute_sample())
    weekly = minute_sample().with_columns(pl.col("datetime").dt.date().alias("date")).drop("datetime")
    periods.persist_period(repo, "1w", weekly)
    assert repo.execute_one("SELECT count(*) FROM kline_minute")[0] == 7


@pytest.mark.parametrize(("period", "count"), [("5m", 7), ("10m", 5), ("15m", 3)])
def test_derived_minute_sync_uses_local_one_minute_without_network(tmp_path, monkeypatch, period, count):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(periods, "_period_provider", lambda *a: pytest.fail("derived minutes must stay local"))
    periods.persist_period(repo, "1m", minute_sample())
    result = periods.sync_period(repo, period, datetime(2026, 10, 26), datetime(2026, 10, 26, 15, 30), symbols=["600000.SH"])
    assert result["rows"] == count
    assert result["source"] == "1m"
    assert result["price_basis"] == "raw"
    empty = periods.sync_period(repo, period, datetime(2026, 10, 25), datetime(2026, 10, 25, 15, 30), symbols=["600000.SH"])
    assert empty["rows"] == 0
    assert "1分K" in empty["error"]


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_aggregation_preserves_sessions_days_symbols_and_unknowns(period):
    from datetime import timedelta

    step = int(period[:-1])
    stamps = [datetime(2026, 10, day) + timedelta(minutes=base + i)
              for day in (23, 26) for base in (570, 780) for i in range(1, 121)]
    frame = pl.DataFrame({
        "symbol": ["600000.SH"] * len(stamps), "datetime": stamps,
        "open": [float(i) for i in range(len(stamps))],
        "high": [float(i + 2) for i in range(len(stamps))],
        "low": [float(i - 1) for i in range(len(stamps))],
        "close": [float(i + 1) for i in range(len(stamps))],
        "volume": [100.] * len(stamps), "amount": [1000.] * len(stamps),
    })
    other = frame.with_columns(pl.lit("000001.SZ").alias("symbol"),
                               pl.lit(None, dtype=pl.Float64).alias("volume"))
    result = periods.aggregate_minute_period(pl.concat([frame.reverse(), other]), period)
    own = result.filter(pl.col("symbol") == "600000.SH")
    assert own.height == 480 // step
    assert own["datetime"].to_list() == [
        datetime(2026, 10, day) + timedelta(minutes=base + i)
        for day in (23, 26) for base in (570, 780) for i in range(step, 121, step)
    ]
    first = own.row(0, named=True)
    assert [first[c] for c in ("open", "high", "low", "close", "volume", "amount")] == [
        0., step + 1., -1., float(step), step * 100., step * 1000.,
    ]
    assert result.filter(pl.col("symbol") == "000001.SZ")["volume"].null_count() == 480 // step


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_query_prefers_current_local_base_even_with_live_and_stale_table(tmp_path, monkeypatch, period):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(periods, "_period_provider", lambda *a: pytest.fail("query must stay local"))
    periods.persist_period(repo, "1m", minute_sample())
    periods.persist_period(repo, period, minute_sample().with_columns(pl.lit(99.).alias("close")))
    for live in (False, True):
        frame, source = periods.query_period(repo, period, "600000.SH", datetime(2026, 10, 26, 9, 34),
                                             datetime(2026, 10, 26, 15, 30), live=live)
        assert source == "local"
        assert frame["close"][0] == {"5m": 1.5, "10m": 2.5, "15m": 3.5}[period]
    assert not any((tmp_path / "kline_5minute").glob("date=2026-10-25/*"))


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_fetch_rejects_remote_even_when_provider_supports_period(period):
    provider = SimpleNamespace(get_klines=lambda *a, **kw: pytest.fail("must not invoke remote"))
    with pytest.raises(ValueError, match="远程"):
        periods.fetch_period(provider, period, ["600000.SH"], datetime(2026, 10, 26), datetime(2026, 10, 26, 15))


def test_provider_unsupported_period_fails_closed():
    provider = SimpleNamespace(get_minute=lambda *a, **kw: minute_sample())
    with pytest.raises(ValueError, match="周期"):
        periods.fetch_period(provider, "5m", ["600000.SH"], datetime(2026, 10, 26), datetime(2026, 10, 26, 15))

@pytest.mark.parametrize("period", ["1m", "5m", "15m", "1d", "1w"])
def test_tickflow_unified_period_parameter_timezone_and_pacing(monkeypatch, period):
    from app.data_providers.tickflow_provider import TickFlowProvider
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
    calls, pace = [], []
    from zoneinfo import ZoneInfo
    timestamp = int(datetime(2026, 10, 26, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    def batch(symbols, **kwargs):
        calls.append(kwargs)
        return {"600000.SH": {"timestamp": [timestamp], **{c: [10.] for c in ("open", "high", "low", "close", "volume", "amount")}}}
    monkeypatch.setattr("app.data_providers.tickflow_provider.get_client", lambda: SimpleNamespace(klines=SimpleNamespace(batch=batch)))
    monkeypatch.setattr("app.tickflow.policy.detect_capabilities", lambda: CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(), Cap.KLINE_MINUTE_BATCH: CapabilityLimits()}))
    monkeypatch.setattr("app.tickflow.rate_limits.sleep_between_batches", lambda index, rpm: pace.append((index, rpm)))
    frame = TickFlowProvider().get_klines(["600000.SH"], datetime(2026, 10, 26), datetime(2026, 10, 26, 16), freq=period)
    assert calls[0]["period"] == period and calls[0]["adjust"] == "none"
    assert pace == [(1, 48)]
    if period in ("1d", "1w"):
        assert frame["date"].to_list() == [date(2026, 10, 26)]
    else:
        assert frame["datetime"].to_list() == [datetime(2026, 10, 26, 9, 30)]


def test_remote_sync_passes_period_and_persists_only_requested_table(tmp_path, monkeypatch):
    from app.data_providers.base import ProviderCapabilities
    calls = []
    def fetch(symbols, start, end, *, asset_type, freq):
        calls.append((symbols, asset_type, freq))
        return minute_sample()
    provider = SimpleNamespace(capabilities=ProviderCapabilities(minute=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)
    repo = KlineRepository(DataStore(tmp_path))
    result = periods.sync_period(repo, "1m", datetime(2026, 10, 26), datetime(2026, 10, 26, 15), symbols=["600000.SH"])
    assert result["rows"] == 7
    assert calls == [(["600000.SH"], "stock", "1m")]
    assert not any((tmp_path / "kline_5minute").rglob("*.parquet"))


def test_weekly_current_bar_replaces_within_same_week(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    for day, close in [(date(2026, 10, 26), 10.), (date(2026, 10, 27), 12.)]:
        frame = pl.DataFrame({"symbol": ["600000.SH"], "date": [day], "open": [9.], "high": [13.], "low": [8.], "close": [close], "volume": [100.], "amount": [1000.]})
        periods.persist_period(repo, "1w", frame)
    assert repo.execute_one("SELECT count(*), max(close) FROM kline_weekly") == (1, 12.)


def test_weekly_sync_aggregates_local_daily_without_provider(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame({
        "symbol": ["600000.SH"] * 5,
        "date": [date(2026, 10, day) for day in range(26, 31)],
        "open": [10., 11., 13., 12., 15.],
        "high": [12., 14., 15., 16., 17.],
        "low": [9., 10., 11., 8., 14.],
        "close": [11., 13., 12., 15., 16.],
        "volume": [100., 200., 300., 400., 500.],
        "amount": [1000., 2500., 3600., 5000., 7500.],
    }))
    monkeypatch.setattr(periods, "_period_provider", lambda *a: pytest.fail("weekly must stay local"))

    result = periods.sync_period(
        repo, "1w", datetime(2026, 10, 26), datetime(2026, 10, 31, 23, 59),
        symbols=["600000.SH"],
    )

    assert result["rows"] == 1
    assert result["source"] == "1d"
    assert repo.execute_one(
        "SELECT symbol, date, open, high, low, close, volume, amount FROM kline_weekly",
    ) == ("600000.SH", date(2026, 10, 26), 10., 17., 8., 16., 1500., 19600.)


@pytest.mark.parametrize(("period", "count"), [("5m", 7), ("10m", 5), ("15m", 3)])
def test_legacy_one_minute_keeps_fetch_basis_and_derived_minute_inherits_it(tmp_path, monkeypatch, period, count):
    repo = KlineRepository(DataStore(tmp_path))
    path = tmp_path / "kline_minute/date=2026-10-26/part.parquet"
    path.parent.mkdir(parents=True)
    minute_sample().write_parquet(path)
    from app.data_providers.base import ProviderCapabilities
    adjustments = []
    def fetch(*args, **kwargs):
        adjustments.append(kwargs["adjust"])
        return minute_sample()
    provider = SimpleNamespace(name="tickflow", capabilities=ProviderCapabilities(minute=True), get_klines=fetch)
    monkeypatch.setattr(periods.preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)
    one = periods.sync_period(repo, "1m", datetime(2026, 10, 26), datetime(2026, 10, 26, 15), symbols=["600000.SH"])
    ten = periods.sync_period(repo, period, datetime(2026, 10, 26), datetime(2026, 10, 26, 15), symbols=["600000.SH"])
    assert adjustments == ["forward"]
    assert one["price_basis"] == ten["price_basis"] == "legacy_1m"
    assert not (tmp_path / "kline_minute/.raw_basis").exists()
    assert ten["rows"] == count


def test_scheduled_periods_holidays_intervals_and_weekly_retry(monkeypatch):
    clock = [datetime(2026, 10, 26, 10)]
    verdict = [True]
    calls = []
    monkeypatch.setattr(periods, "cn_now", lambda: clock[0])
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda now: verdict[0])
    monkeypatch.setattr(preferences := periods.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(periods, "get_configs", lambda: {
        p: {"enabled": True, "interval_seconds": interval, "windows": periods.DEFAULT_WINDOWS, "time": "16:00"}
        for p, interval in [("5m", 300), ("10m", 600), ("15m", 900), ("1w", 60)]
    })
    monkeypatch.setattr("app.jobs.daily_pipeline._run_tracked", lambda fn, label: calls.append(label) or True)
    monkeypatch.setattr(periods, "_last_success", {})
    periods.scheduled_periods(None)
    assert calls == ["kline_5m", "kline_10m", "kline_15m"]
    calls.clear()
    clock[0] = datetime(2026, 10, 26, 10, 5)
    periods.scheduled_periods(None)
    assert calls == ["kline_5m"]
    calls.clear()
    clock[0] = datetime(2026, 10, 26, 16)
    verdict[0] = False
    periods.scheduled_periods(None)
    assert not calls
    verdict[0] = True
    periods.scheduled_periods(None)
    assert not calls
    periods.scheduled_periods(None)
    assert not calls
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: False)
    clock[0] = datetime(2026, 10, 27, 16)
    periods.scheduled_periods(None)
    assert not calls


def test_scheduled_weekly_runs_once_on_saturday_without_trading_day(monkeypatch):
    clock = [datetime(2026, 10, 26, 16)]
    calls = []
    monkeypatch.setattr(periods, "cn_now", lambda: clock[0])
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda now: False)
    monkeypatch.setattr(periods.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(periods, "get_configs", lambda: {
        p: {"enabled": p == "1w", "interval_seconds": 60, "windows": periods.DEFAULT_WINDOWS, "time": "16:00"}
        for p in ("5m", "10m", "15m", "1w")
    })
    monkeypatch.setattr("app.jobs.daily_pipeline._run_tracked", lambda fn, label: calls.append(label) or True)
    monkeypatch.setattr(periods, "_last_success", {})

    periods.scheduled_periods(None)
    assert calls == []
    clock[0] = datetime(2026, 10, 31, 16)
    periods.scheduled_periods(None)
    periods.scheduled_periods(None)

    assert calls == ["kline_1w"]


@pytest.mark.parametrize("period", ["5m", "10m", "15m", "1w"])
def test_local_generation_uses_base_symbols_without_instrument_catalog(tmp_path, monkeypatch, period):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(periods, "_period_provider", lambda *a: pytest.fail("generation must stay local"))
    if period == "1w":
        repo.append_daily(minute_sample().with_columns(pl.col("datetime").dt.date().alias("date")).drop("datetime").head(1))
    else:
        periods.persist_period(repo, "1m", minute_sample().head(1))
    assert repo.get_instruments().is_empty()
    result = periods.generate_period(repo, period)
    assert "error" not in result
    assert result["rows"] == 1
    assert repo.execute_one(f"SELECT symbol FROM {periods.TABLES[period]}")[0] == "600000.SH"
