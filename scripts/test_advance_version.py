from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import advance_version
from check_version import _check_source_versions
from versioning import next_version

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = (
    "VERSION",
    "frontend/package.json",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/app/__init__.py",
    "README.md",
    "docs/README.md",
    "docs/version-baseline.md",
    "docs/requirements.md",
    "操作说明书.md",
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    # Isolate these pushes from all developer Git configuration and credentials.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "work"
    root.mkdir()
    for name in VERSION_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (root / "scripts").mkdir()
    for name in ("advance_version.py", "check_version.py", "versioning.py"):
        shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Version test")
    git(root, "config", "user.email", "version-test@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial application")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    return root


@pytest.fixture
def main_event(repository, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    source = git(repository, "rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_SHA", source)
    return repository, source


def snapshot(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in VERSION_FILES}


def current_version(root: Path) -> str:
    return (root / "VERSION").read_text().strip().removeprefix("v")


def test_updates_all_current_sources_without_rewriting_history(repository):
    previous = current_version(repository)
    expected = next_version(previous)
    lock = repository / "backend/uv.lock"
    third_party = f'\n[[package]]\nname = "unrelated"\nversion = "{previous}"\n'
    lock.write_text(lock.read_text() + third_party)
    history = repository / "reports/history.md"
    history.parent.mkdir()
    history.write_text(f"Verified {previous}\n")
    before = snapshot(repository)

    assert advance_version.update_versions(repository) == expected

    _check_source_versions(repository, expected)
    after = snapshot(repository)
    assert set(name for name in before if before[name] != after[name]) == set(
        VERSION_FILES
    )
    assert third_party in lock.read_text()
    assert history.read_text() == f"Verified {previous}\n"
    assert f"应用语义版本为 `{expected}`" in after["README.md"].decode()
    assert f"适用版本：{expected}" in after["操作说明书.md"].decode()
    assert f"# TSP v{expected} 需求基线" in after["docs/requirements.md"].decode()


@pytest.mark.parametrize(
    "broken_file", ["backend/pyproject.toml", "docs/README.md"]
)
def test_invalid_input_leaves_all_files_untouched(repository, broken_file):
    (repository / broken_file).write_text("missing version\n")
    before = snapshot(repository)
    with pytest.raises(ValueError, match=broken_file):
        advance_version.update_versions(repository)
    assert snapshot(repository) == before


def test_mismatched_metadata_is_rejected_before_writing(repository):
    package_path = repository / "frontend/package.json"
    package = json.loads(package_path.read_text())
    package["version"] = next_version(current_version(repository))
    package_path.write_text(json.dumps(package, indent=2))
    before = snapshot(repository)
    with pytest.raises(ValueError, match="version source mismatch"):
        advance_version.update_versions(repository)
    assert snapshot(repository) == before


@pytest.mark.parametrize(
    ("previous", "expected"),
    [("0.4.99", "0.5.0"), ("0.9.99", "1.0.0"), ("9.9.99", None)],
)
def test_version_rollover_and_ceiling_preserve_consistency(repository, previous, expected):
    current = current_version(repository)
    for name in VERSION_FILES:
        path = repository / name
        path.write_text(path.read_text().replace(current, previous))
    before = snapshot(repository)
    if expected is None:
        with pytest.raises(ValueError, match="has reached 9.9.99"):
            advance_version.update_versions(repository)
        assert snapshot(repository) == before
    else:
        assert advance_version.update_versions(repository) == expected
        _check_source_versions(repository, expected)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF", "refs/tags/v0.4.2"),
    ],
)
def test_only_remote_main_push_can_write(main_event, monkeypatch, key, value):
    root, source = main_event
    monkeypatch.setenv(key, value)
    before = snapshot(root)
    with pytest.raises(ValueError, match="GitHub Actions push to main"):
        advance_version.advance_main(root, source)
    assert snapshot(root) == before
    assert git(root, "ls-remote", "origin", "refs/heads/main").split()[0] == source


def test_local_and_branch_version_checks_accept_unchanged_version(repository):
    before = snapshot(repository)
    for branch in ("main", "feature"):
        git(repository, "checkout", "-B", branch)
        result = subprocess.run(
            [sys.executable, "scripts/check_version.py", "--current", "VERSION"],
            cwd=repository,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    assert snapshot(repository) == before


def test_merge_bumps_once_and_rerun_reuses_tested_commit(main_event):
    root, _ = main_event
    previous = current_version(root)
    git(root, "checkout", "-b", "feature")
    (root / "feature.txt").write_text("new behavior\n")
    git(root, "add", "feature.txt")
    git(root, "commit", "-m", "Feature")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "feature", "-m", "Merge feature")
    source = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")

    revision = advance_version.advance_main(root, source)

    assert git(root, "rev-parse", "HEAD") == revision
    assert git(root, "rev-parse", "HEAD^") == source
    assert git(root, "ls-remote", "origin", "refs/heads/main").split()[0] == revision
    assert current_version(root) == next_version(previous)
    _check_source_versions(root, next_version(previous))
    assert git(root, "status", "--porcelain") == ""
    assert advance_version.advance_main(root, source) == revision
    assert current_version(root) == next_version(previous)


def test_delayed_events_each_advance_once_and_old_rerun_returns_original(main_event):
    root, first_source = main_event
    initial = current_version(root)
    git(root, "commit", "--allow-empty", "-m", "Second integration")
    second_source = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    first_revision = advance_version.advance_main(root, first_source)
    second_revision = advance_version.advance_main(root, second_source)
    assert first_revision != second_revision
    assert current_version(root) == next_version(next_version(initial))
    assert advance_version.advance_main(root, first_source) == first_revision
    assert git(root, "ls-remote", "origin", "main").split()[0] == second_revision


def test_concurrent_push_retries_from_latest_remote_without_losing_changes(
    main_event, monkeypatch, tmp_path
):
    root, source = main_event
    initial = current_version(root)
    other = tmp_path / "other"
    git(tmp_path, "clone", str(tmp_path / "origin.git"), str(other))
    git(other, "config", "user.name", "Concurrent test")
    git(other, "config", "user.email", "concurrent@example.invalid")
    real_git = advance_version._git
    pushed = []

    def concurrent_git(root, *args, **kwargs):
        if args[0] == "push" and not pushed:
            pushed.append(True)
            (other / "concurrent.txt").write_text("preserve concurrent integration\n")
            git(other, "add", ".")
            git(other, "commit", "-m", "Concurrent integration")
            git(other, "push", "origin", "main")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(advance_version, "_git", concurrent_git)
    revision = advance_version.advance_main(root, source)
    assert (root / "concurrent.txt").read_text() == "preserve concurrent integration\n"
    assert current_version(root) == next_version(initial)
    assert git(root, "ls-remote", "origin", "main").split()[0] == revision


def test_two_workers_for_same_event_do_not_double_increment(
    main_event, monkeypatch, tmp_path
):
    root, source = main_event
    initial = current_version(root)
    other = tmp_path / "other"
    git(tmp_path, "clone", str(tmp_path / "origin.git"), str(other))
    real_git = advance_version._git
    other_revisions = []

    def concurrent_git(root, *args, **kwargs):
        if root != other and args[0] == "push" and not other_revisions:
            other_revisions.append(advance_version.advance_main(other, source))
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(advance_version, "_git", concurrent_git)
    assert advance_version.advance_main(root, source) == other_revisions[0]
    assert git(root, "show", f"{other_revisions[0]}:VERSION").removeprefix(
        "v"
    ) == next_version(initial)


def test_push_rejection_fails_without_force_or_repeated_version_commits(
    main_event, monkeypatch
):
    root, source = main_event
    real_git = advance_version._git
    pushes = []

    def denied_git(root, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1, "", "protected branch")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(advance_version, "_git", denied_git)
    with pytest.raises(RuntimeError, match="protected branch"):
        advance_version.advance_main(root, source)
    assert pushes == [("push", "origin", "HEAD:refs/heads/main")]
    assert git(root, "ls-remote", "origin", "main").split()[0] == source


def test_unpushed_commit_cannot_trigger_version_update(main_event):
    root, _ = main_event
    git(root, "commit", "--allow-empty", "-m", "Local only")
    unpushed = git(root, "rev-parse", "HEAD")
    before = snapshot(root)
    with pytest.raises(ValueError, match="not integrated into origin/main"):
        advance_version.advance_main(root, unpushed)
    assert snapshot(root) == before


def test_dirty_checkout_is_preserved(main_event):
    root, source = main_event
    (root / "README.md").write_text("user work\n")
    before = snapshot(root)
    with pytest.raises(ValueError, match="clean checkout"):
        advance_version.advance_main(root, source)
    assert snapshot(root) == before


def test_cli_exports_revision_for_downstream_quality_jobs(main_event, monkeypatch):
    root, source = main_event
    output = root.parent / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    result = subprocess.run(
        [sys.executable, "scripts/advance_version.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    revision = git(root, "ls-remote", "origin", "main").split()[0]
    assert revision != source
    assert output.read_text() == f"revision={revision}\n"
    package = json.loads((root / "frontend/package.json").read_text())
    assert package["version"] in result.stdout
