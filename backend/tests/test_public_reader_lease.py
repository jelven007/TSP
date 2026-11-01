"""The immutable manifest is the host/API cross-process reader lock."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest


@pytest.fixture
def release_tree(tmp_path):
    root = tmp_path / "public"
    releases = root / "releases"
    releases.mkdir(parents=True)
    for index in range(3):
        path = releases / f"{index:032x}"
        path.mkdir()
        (path / ".public_release.json").write_text(json.dumps({"id": path.name}))
        past = time.time() - (10 - index) * 3600
        os.utime(path, (past, past))
    (root / "current").symlink_to("releases/" + f"{2:032x}")
    return root, releases / f"{0:032x}"


def exclusive_available(path):
    result = subprocess.run(
        [sys.executable, "-c", """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(2)
finally:
    os.close(fd)
""", str(path / ".public_release.json")],
        capture_output=True, timeout=5,
    )
    assert result.returncode in (0, 2), result.stderr
    return result.returncode == 0


def test_two_readers_hold_manifest_until_last_release(release_tree):
    from app.multitenancy.public_reader import PublicReleaseLease

    _, path = release_tree
    one, two = PublicReleaseLease.acquire(path), PublicReleaseLease.acquire(path)
    try:
        assert one.held and two.held
        assert not exclusive_available(path)
        one.close()
        one.close()
        assert not exclusive_available(path)
        two.close()
        assert exclusive_available(path)
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize("invalid", ["id", "missing", "symlink", "directory", "outside"])
def test_reader_rejects_invalid_or_unprotected_manifest(release_tree, tmp_path, invalid):
    from app.multitenancy.public_reader import PublicReleaseError, PublicReleaseLease

    _, path = release_tree
    manifest = path / ".public_release.json"
    if invalid == "id":
        manifest.write_text('{"id":"invalid"}')
    elif invalid == "missing":
        manifest.unlink()
    elif invalid == "symlink":
        manifest.unlink()
        target = tmp_path / "marker"
        target.write_text(json.dumps({"id": path.name}))
        manifest.symlink_to(target)
    elif invalid == "directory":
        manifest.unlink()
        manifest.mkdir()
    else:
        outside = tmp_path / path.name
        path.rename(outside)
        path = outside
    with pytest.raises(PublicReleaseError):
        PublicReleaseLease.acquire(path)
    if invalid == "id":
        assert exclusive_available(path)


def test_manifest_replacement_invalidates_reader(release_tree):
    from app.multitenancy.public_reader import PublicReleaseError, PublicReleaseLease

    _, path = release_tree
    lease = PublicReleaseLease.acquire(path)
    try:
        replacement = path / "replacement.json"
        replacement.write_text(json.dumps({"id": path.name}))
        replacement.replace(path / ".public_release.json")
        with pytest.raises(PublicReleaseError):
            lease.validate()
    finally:
        lease.close()


def test_reader_rejects_symlink_release_parent(release_tree, tmp_path):
    from app.multitenancy.public_reader import PublicReleaseError, PublicReleaseLease

    root, path = release_tree
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "releases").symlink_to(root / "releases", target_is_directory=True)
    with pytest.raises(PublicReleaseError):
        PublicReleaseLease.acquire(alias / "releases" / path.name)


def test_late_reader_rejects_directory_deleted_after_lock(release_tree, monkeypatch):
    import shutil

    from app.multitenancy import public_reader

    _, path = release_tree
    flock = public_reader.fcntl.flock

    def deleted(fd, mode):
        flock(fd, mode)
        shutil.rmtree(path)

    monkeypatch.setattr(public_reader.fcntl, "flock", deleted)
    with pytest.raises(public_reader.PublicReleaseError):
        public_reader.PublicReleaseLease.acquire(path)


def test_maintenance_query_failure_still_aborts(release_tree):
    from scripts.operations.maintain_full_host import delete_release_if_unused

    root, path = release_tree

    def unavailable():
        raise RuntimeError("research query failed")

    with pytest.raises(RuntimeError, match="research query failed"):
        delete_release_if_unused(
            path, root=root, protected=set(), read_references=unavailable,
            keep=2, min_age_hours=6,
        )
    assert path.exists() and exclusive_available(path)


def test_maintenance_guard_preserves_readers_and_then_deletes(release_tree):
    from app.multitenancy.public_reader import PublicReleaseLease
    from scripts.operations.maintain_full_host import delete_release_if_unused

    root, path = release_tree
    lease = PublicReleaseLease.acquire(path)
    try:
        assert delete_release_if_unused(
            path, root=root, protected=set(), read_references=set, keep=2, min_age_hours=6,
        ) == "reader_lock_busy"
        assert path.is_dir()
    finally:
        lease.close()
    assert delete_release_if_unused(
        path, root=root, protected=set(), read_references=set, keep=2, min_age_hours=6,
    ) == "deleted"
    assert not path.exists()


def test_maintenance_rechecks_research_inside_guard(release_tree):
    from scripts.operations.maintain_full_host import delete_release_if_unused

    root, path = release_tree
    assert delete_release_if_unused(
        path, root=root, protected=set(), read_references=lambda: {path.name},
        keep=2, min_age_hours=6,
    ) == "retained"
    assert path.exists()


def test_maintenance_refuses_missing_manifest(release_tree):
    from scripts.operations.maintain_full_host import delete_release_if_unused

    root, path = release_tree
    (path / ".public_release.json").unlink()
    assert delete_release_if_unused(
        path, root=root, protected=set(), read_references=set, keep=2, min_age_hours=6,
    ) == "invalid_release"
    assert path.exists()


def test_no_flock_preserves_reads_and_disables_deletion(release_tree, monkeypatch):
    from app.multitenancy import public_reader
    from scripts.operations import maintain_full_host

    root, path = release_tree
    monkeypatch.setattr(public_reader, "fcntl", None)
    monkeypatch.setattr(maintain_full_host, "fcntl", None)
    lease = public_reader.PublicReleaseLease.acquire(path)
    try:
        assert not lease.held
        assert lease.validate()["id"] == path.name
        assert maintain_full_host.delete_release_if_unused(
            path, root=root, protected=set(), read_references=set, keep=2, min_age_hours=6,
        ) == "reader_lock_unavailable"
        assert path.is_dir()
    finally:
        lease.close()
