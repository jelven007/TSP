"""Recovery bundles must identify tagged ancestors and fail closed on corruption."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "source_recovery", Path(__file__).resolve().parents[2] / "scripts/release/source_recovery.py",
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Recovery Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root, text=True, stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture(params=["v", ""])
def repo(tmp_path, request):
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    for index in range(1, 8):
        (root / "VERSION").write_text(f"{request.param}0.4.{index}\n")
        git(root, "add", "VERSION")
        git(root, "commit", "-m", f"version {index}")
        git(root, "tag", f"v0.4.{index}")
    return root


def test_archives_are_six_tagged_versions_and_refs_are_unchanged(repo):
    before = git(repo, "show-ref")
    output = repo.parent / "recovery"
    manifest = recovery.create(repo, "main", output)
    assert recovery.verify(output) == manifest
    assert [r["version"] for r in manifest["versions"]] == [f"0.4.{i}" for i in range(7, 1, -1)]
    assert git(repo, "show-ref") == before
    assert not list(repo.parent.glob(".source-recovery-*"))


def test_rejects_untagged_head_without_creating_output(repo):
    git(repo, "commit", "--allow-empty", "-m", "not checked")
    with pytest.raises(ValueError, match="immutable"):
        recovery.create(repo, "main", repo.parent / "recovery")
    assert not (repo.parent / "recovery").exists()


def test_branch_tags_do_not_count_as_main_history(repo):
    git(repo, "checkout", "-b", "unmerged")
    (repo / "VERSION").write_text("0.4.8\n")
    git(repo, "commit", "-am", "unmerged version")
    git(repo, "tag", "v0.4.8")
    assert recovery.select_versions(repo, "main")[0]["version"] == "0.4.7"


def test_existing_output_and_symlink_are_not_overwritten(repo):
    output = repo.parent / "recovery"
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_text("existing backup")
    with pytest.raises(ValueError, match="new directory"):
        recovery.create(repo, "main", output)
    link = repo.parent / "linked-backup"
    try:
        link.symlink_to(output, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="new directory"):
        recovery.create(repo, "main", link)
    assert sentinel.read_text() == "existing backup"


@pytest.mark.parametrize("damage", ["archive", "path", "duplicate", "extra"])
def test_verification_rejects_corruption_and_unsafe_paths(repo, damage):
    output = repo.parent / "recovery"
    manifest = recovery.create(repo, "main", output)
    if damage == "archive":
        (output / manifest["versions"][0]["archive"]).write_bytes(b"corrupt")
    elif damage == "path":
        manifest["versions"][0]["archive"] = "../outside.tar.gz"
    elif damage == "duplicate":
        manifest["versions"][1] = manifest["versions"][0]
    else:
        (output / "TSP-v0.4.1.tar.gz").write_bytes(b"old")
    (output / "index.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        recovery.verify(output)


def test_missing_historical_version_does_not_publish_partial_set(repo):
    git(repo, "tag", "-d", "v0.4.1", "v0.4.2")
    with pytest.raises(ValueError, match="five prior"):
        recovery.create(repo, "main", repo.parent / "recovery")
    assert not (repo.parent / "recovery").exists()
