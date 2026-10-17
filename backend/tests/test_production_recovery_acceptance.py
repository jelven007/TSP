from __future__ import annotations

from pathlib import Path

import pytest

from scripts.accept_production_recovery import (
    _sha256,
    _tree_inventory,
    _verify_backup_integrity,
)


def _backup(root: Path) -> dict:
    public = root / "public"
    public.mkdir(parents=True)
    (public / "marker.txt").write_text("release-001\n", encoding="utf-8")
    control = b"PGDMP-control"
    data = b"PGDMP-data"
    return {
        "control_dump": control,
        "data_dump": data,
        "evidence": {
            "control_dump": {
                "bytes": len(control),
                "sha256": _sha256(control),
            },
            "data_dump": {
                "bytes": len(data),
                "sha256": _sha256(data),
            },
            "directories": {
                "public": _tree_inventory(public),
            },
        },
    }


def test_backup_integrity_accepts_original_dump_and_directory(tmp_path: Path) -> None:
    backup = _backup(tmp_path)

    result = _verify_backup_integrity(backup, tmp_path)

    assert result["verified"] is True
    assert result["dumps"]["control"] == backup["evidence"]["control_dump"]
    assert result["directories"]["public"] == backup["evidence"]["directories"]["public"]


def test_backup_integrity_rejects_truncated_dump(tmp_path: Path) -> None:
    backup = _backup(tmp_path)
    backup["control_dump"] = backup["control_dump"][:-1]

    with pytest.raises(AssertionError, match="control database dump"):
        _verify_backup_integrity(backup, tmp_path)


def test_backup_integrity_rejects_tampered_directory(tmp_path: Path) -> None:
    backup = _backup(tmp_path)
    (tmp_path / "public" / "marker.txt").write_text(
        "release-002\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="public backup directory"):
        _verify_backup_integrity(backup, tmp_path)
