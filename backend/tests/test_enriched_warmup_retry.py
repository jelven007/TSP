"""Background warmup must rebuild after a generation change, with a bound."""
from datetime import date
from threading import Event
from unittest.mock import Mock

import polars as pl
import pytest

from app.enriched_generation import EnrichedGenerationUnavailableError
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repository = KlineRepository(DataStore(tmp_path))
    latest = date(2026, 10, 16)
    frame = pl.DataFrame({
        "symbol": ["600000.SH"], "date": [latest],
        "open": [10.], "high": [10.], "low": [10.], "close": [10.],
        "volume": [100.], "amount": [1000.],
    })
    part = tmp_path / "kline_daily_enriched" / f"date={latest}"
    part.mkdir()
    frame.write_parquet(part / "part.parquet")
    repository._instruments_cache = pl.DataFrame()
    monkeypatch.setattr(repository, "_latest_enriched_date_duckdb", lambda: latest)
    monkeypatch.setattr(repository, "get_instrument_history", lambda: None)
    monkeypatch.setattr(repository, "_restore_missing_latest_rows", lambda _, df, __: df)
    monkeypatch.setattr(
        repository, "_build_live_agg",
        lambda _: setattr(repository, "_live_agg_cache", frame),
    )
    repository._on_refresh_done = Mock()
    repository._on_warmup_done = Mock()
    yield repository
    repository.db.close()


def warm(repo):
    repo._start_enriched_warmup()
    repo._warmup_thread.join(timeout=10)
    assert not repo._warmup_thread.is_alive()
    assert not repo._enriched_warming
    repo._on_warmup_done.assert_called_once()


def test_warmup_recovers_changed_generation_and_reuses_history(repo, monkeypatch):
    generation = ["old"]
    calls = []

    def compute(df, *_args, **_kwargs):
        calls.append(generation[0])
        generation[0] = "new"
        return df

    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _: generation[0])
    monkeypatch.setattr("app.indicators.pipeline.compute_enriched_history_window", compute)
    warm(repo)

    assert calls == ["old", "new"]
    assert repo.enriched_ready
    assert repo._enriched_history_generation == "new"
    repo._on_refresh_done.assert_called_once()
    history = repo._enriched_history_cache
    repo.get_enriched_latest()
    assert repo._enriched_history_cache is history
    assert calls == ["old", "new"]


def test_warmup_stops_after_continuous_generation_changes(repo, monkeypatch):
    generation = [0]

    def compute(df, *_args, **_kwargs):
        generation[0] += 1
        return df

    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _: str(generation[0]))
    monkeypatch.setattr("app.indicators.pipeline.compute_enriched_history_window", compute)
    warm(repo)

    assert generation[0] == 3
    assert repo._enriched_history_cache is None
    assert not repo.enriched_ready
    repo._on_refresh_done.assert_not_called()


def test_warmup_does_not_retry_disk_error(repo, monkeypatch):
    reads = Mock(side_effect=OSError("unreadable partition"))
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _: "same")
    monkeypatch.setattr("app.tickflow.repository.pl.read_parquet", reads)
    warm(repo)

    reads.assert_called_once()
    assert not repo.enriched_ready
    repo._on_refresh_done.assert_not_called()


def test_foreground_refresh_does_not_retry_generation_error(repo, monkeypatch):
    reads = Mock(side_effect=EnrichedGenerationUnavailableError("publication active"))
    monkeypatch.setattr(repo, "get_matrix_data_generation", reads)
    repo._refresh_enriched()
    reads.assert_called_once()
    assert repo._enriched_history_cache is None


def test_active_publication_waits_before_retry(repo, monkeypatch):
    generation = Mock(side_effect=[
        EnrichedGenerationUnavailableError("publication active"), "new", "new",
    ])
    delay = Mock()
    monkeypatch.setattr(repo, "get_matrix_data_generation", generation)
    monkeypatch.setattr("app.tickflow.repository.enriched_publication_incomplete", lambda *_: True)
    monkeypatch.setattr("app.tickflow.repository.time.sleep", delay)
    monkeypatch.setattr(
        "app.indicators.pipeline.compute_enriched_history_window", lambda df, *_a, **_k: df,
    )
    warm(repo)
    delay.assert_called_once_with(5.0)
    assert repo.enriched_ready


def test_duplicate_warmup_does_not_spawn_or_compute_twice(repo, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def compute(df, *_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=5)
        return df

    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _: "same")
    monkeypatch.setattr("app.indicators.pipeline.compute_enriched_history_window", compute)
    repo._start_enriched_warmup()
    try:
        assert entered.wait(timeout=5)
        first_thread = repo._warmup_thread
        repo._start_enriched_warmup()
        assert repo._warmup_thread is first_thread
        assert repo.get_enriched_range(date(2026, 10, 16), date(2026, 10, 16)) is None
    finally:
        release.set()
        repo._warmup_thread.join(timeout=10)
    assert calls == [1]
    assert repo.enriched_ready
    repo._on_refresh_done.assert_called_once()
