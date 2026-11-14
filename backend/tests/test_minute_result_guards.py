"""Conflicting source outputs must not disappear through last-row deduplication."""
import json
from datetime import datetime

import pytest

from app.plugins.rustdx.client import RustdxError
from app.plugins.rustdx.provider import RustdxProvider
from app.plugins.rustdx.routing import TaskRuntime


def _rows():
    return [{"datetime": datetime(2026, 11, 13, 14, 30),
             "open": 10., "high": 11., "low": 9., "close": 10.,
             "vol": 100., "amount": 1000.}]


def _check(tmp_path, monkeypatch, candidate, reference):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    runtime = TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709")
    result = runtime.validate_bar_window(
        candidate, scope=("000001.SZ", 8, datetime(2026, 11, 13), datetime(2026, 11, 13, 15)),
        source={"endpoint": "127.0.0.3:7709", "generation": 1},
        reference_read=lambda: (reference, 1),
    )
    return result, json.loads(runtime.stats_json())["window_validation"]


def test_incremental_conflicting_duplicate_is_rejected():
    raw = _rows()
    with pytest.raises(RustdxError, match="冲突"):
        RustdxProvider._minute_frame([*raw, {**raw[0], "amount": 2000.}], "000001.SZ", None, None)
    assert RustdxProvider._minute_frame(raw + raw, "000001.SZ", None, None).height == 1


def test_missing_reference_key_cannot_replace_existing_completed_window(tmp_path, monkeypatch):
    with pytest.raises(RustdxError, match="缺"):
        _check(tmp_path, monkeypatch, _rows(), [])


def test_cent_difference_keeps_exact_reference_without_material_conflict(tmp_path, monkeypatch):
    raw = _rows()
    reference = [{**raw[0], "amount": 1000.01}]
    result, stats = _check(tmp_path, monkeypatch, raw, reference)
    assert result == reference and raw[0]["amount"] == 1000.
    assert stats["conflicts"] == 0 and stats["canonical_replacements"] == 1
    assert stats["within_cent"] == 1


def test_volume_has_no_cent_tolerance_and_conflict_evidence_survives(tmp_path, monkeypatch):
    raw = _rows()
    reference = [{**raw[0], "vol": 100.001}]
    result, stats = _check(tmp_path, monkeypatch, raw, reference)
    assert result == reference and stats["conflicts"] == 1
    receipts = list(tmp_path.glob("cache/rustdx/minute_conflicts/*/*.json"))
    assert len(receipts) == 1
    proof = json.loads(receipts[0].read_text())
    assert proof["candidate"][0]["vol"] == 100.
    assert proof["reference"][0]["vol"] == 100.001
    assert proof["scope"][0] == "000001.SZ" and proof["observed_at"]


def test_noon_copy_matches_canonical_morning_close(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    candidate = [{**_rows()[0], "datetime": datetime(2026, 11, 13, 13)}]
    reference = [{**candidate[0], "datetime": datetime(2026, 11, 13, 11, 30)}]
    runtime = TaskRuntime(None, connections=8, timeout=2, reference="127.0.0.2:7709")
    result = runtime.validate_bar_window(
        candidate, scope=("000001.SZ", 8, datetime(2026, 11, 13), datetime(2026, 11, 13, 13, 1)),
        source={"endpoint": "127.0.0.3:7709"}, reference_read=lambda: reference,
    )
    assert RustdxProvider._minute_frame(result, "000001.SZ", None, None)["datetime"].to_list() == [
        datetime(2026, 11, 13, 11, 30)]
