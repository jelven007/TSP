from __future__ import annotations

import threading

import pytest

from app.services.research_snapshot import (
    ResearchSnapshotError,
    capture_research_snapshot,
    verify_research_snapshot,
)


def inputs(root):
    for name, content in {
        "kline_daily_enriched/date=2024-01-01/part.parquet": b"old-day",
        "kline_daily_enriched/date=2026-10-16/part.parquet": b"new-day",
        "strategies/custom/example.py": b"strategy",
        "user_data/custom_signals/signal.json": b"{}",
        "preferences.json": b"secret",
        "ext_data/concepts/config.json": b'{"id":"concepts","pull":{"token":"secret"}}',
    }.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def test_snapshot_retains_all_dates_and_private_definitions_without_credentials(tmp_path):
    inputs(tmp_path)
    root, manifest = capture_research_snapshot(tmp_path)
    assert (root / "kline_daily_enriched/date=2024-01-01/part.parquet").read_bytes() == b"old-day"
    assert (root / "strategies/custom/example.py").read_bytes() == b"strategy"
    assert not (root / "preferences.json").exists()
    assert "secret" not in (root / "ext_data/concepts/config.json").read_text()
    assert len(manifest["id"]) == 64
    assert manifest["files"]["kline_daily_enriched/date=2024-01-01/part.parquet"]["sha256"]
    assert verify_research_snapshot(root)["id"] == manifest["id"]
    quotes = tmp_path / "quote_second/today.parquet"
    quotes.parent.mkdir()
    quotes.write_bytes(b"unrelated live quote")
    same, repeated = capture_research_snapshot(tmp_path)
    assert same == root
    assert repeated["id"] == manifest["id"]
    original = tmp_path / "kline_daily_enriched/date=2024-01-01/part.parquet"
    original.write_bytes(b"correction")
    next_root, next_manifest = capture_research_snapshot(tmp_path)
    assert next_manifest["id"] != manifest["id"]
    assert next_root != root
    assert (root / original.relative_to(tmp_path)).read_bytes() == b"old-day"


def test_corrupted_snapshot_cannot_be_reused(tmp_path):
    inputs(tmp_path)
    root, _ = capture_research_snapshot(tmp_path)
    (root / "strategies/custom/example.py").write_bytes(b"changed")
    with pytest.raises(ResearchSnapshotError, match="checksum"):
        verify_research_snapshot(root)
    with pytest.raises(ResearchSnapshotError, match="checksum"):
        capture_research_snapshot(tmp_path)


@pytest.mark.parametrize("limit", ["max_files", "max_bytes"])
def test_snapshot_limits_do_not_publish_partial_data(tmp_path, limit):
    inputs(tmp_path)
    with pytest.raises(ResearchSnapshotError, match="limit"):
        capture_research_snapshot(tmp_path, **{limit: 1})
    assert not list((tmp_path / "research_inputs").glob("*/.research_snapshot.json"))


def test_cancellation_and_links_fail_closed(tmp_path):
    inputs(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ResearchSnapshotError, match="cancel"):
        capture_research_snapshot(tmp_path, cancel=cancel)
    (tmp_path / "instruments").symlink_to(tmp_path / "user_data", target_is_directory=True)
    with pytest.raises(ResearchSnapshotError, match="symbolic"):
        capture_research_snapshot(tmp_path)


def test_new_file_during_copy_rejects_mixed_snapshot(tmp_path, monkeypatch):
    from app.services import research_snapshot

    inputs(tmp_path)
    original = research_snapshot._copy_file
    changed = False

    def copy(source, target, cancel):
        nonlocal changed
        result = original(source, target, cancel)
        if not changed:
            changed = True
            (tmp_path / "strategies/custom/new.py").write_text("new")
        return result

    monkeypatch.setattr(research_snapshot, "_copy_file", copy)
    with pytest.raises(ResearchSnapshotError, match="changed"):
        capture_research_snapshot(tmp_path)
    assert not list((tmp_path / "research_inputs").glob("*/.research_snapshot.json"))


def test_version_limit_keeps_existing_snapshot(tmp_path):
    inputs(tmp_path)
    root, _ = capture_research_snapshot(tmp_path)
    (tmp_path / "strategies/custom/example.py").write_bytes(b"new strategy")
    with pytest.raises(ResearchSnapshotError, match="retained"):
        capture_research_snapshot(tmp_path, max_versions=1)
    assert verify_research_snapshot(root)


def test_runtime_change_and_total_retention_limit_reject_reuse(tmp_path, monkeypatch):
    from app.services import research_snapshot

    inputs(tmp_path)
    root, _ = capture_research_snapshot(tmp_path)
    original = research_snapshot._runtime_identity
    monkeypatch.setattr(research_snapshot, "_runtime_identity", lambda cancel=None: {"source": "changed"})
    with pytest.raises(ResearchSnapshotError, match="runtime changed"):
        verify_research_snapshot(root)
    monkeypatch.setattr(research_snapshot, "_runtime_identity", original)
    monkeypatch.setattr(research_snapshot, "MAX_RETAINED_BYTES", 1)
    (tmp_path / "strategies/custom/example.py").write_bytes(b"new strategy")
    with pytest.raises(ResearchSnapshotError, match="retained research byte limit"):
        capture_research_snapshot(tmp_path)
    assert verify_research_snapshot(root)
