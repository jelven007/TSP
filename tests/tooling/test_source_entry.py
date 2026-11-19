"""A copied VERSION must never certify a development source identity."""
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "scripts/release/check_source.py"
REVISION = "a" * 40


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    files = {"VERSION": b"0.4.67\n", "backend/app.py": b"print('example')\n"}
    archive = recovery / "TSP-v0.4.67.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            member = tarfile.TarInfo(f"TSP-v0.4.67/{name}")
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    record = {
        "version": "0.4.67", "commit": REVISION, "ref": "v0.4.67",
        "archive": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    (recovery / "index.json").write_text(json.dumps({
        "format": 1, "policy": "latest_source_plus_one_prior_version",
        "latest": "0.4.67", "versions": [record],
    }))
    return root, recovery


def invoke(source, *extra, recovery=True):
    root, bundle = source
    args = [sys.executable, str(ENTRY), "--root", str(root),
            "--version", "0.4.67", "--revision", REVISION]
    if recovery:
        args += ["--recovery-set", str(bundle)]
    return subprocess.run(args + list(extra), capture_output=True, text=True, check=False)


def test_archive_match_is_explicitly_not_git_or_runtime_certification(source):
    result = invoke(source)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["basis"] == "verified_recovery_archive"
    assert report["git_head"] is None
    assert report["runtime_verified"] is False
    assert report["clean"] is True
    assert report["checked_files"] == 2


@pytest.mark.parametrize("change", ["modified", "missing", "added", "symlink"])
def test_reports_drift_and_requires_explicit_dirty_mode(source, change):
    root, _ = source
    target = root / "backend/app.py"
    if change == "modified":
        target.write_text("changed\n")
    elif change == "missing":
        target.unlink()
    elif change == "added":
        target = root / "backend/new.py"
        target.write_text("new\n")
    else:
        target.unlink()
        target.symlink_to(root / "VERSION")
    result = invoke(source)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["clean"] is False
    assert report["changes"] == [{"path": str(target.relative_to(root)), "kind": change}]
    assert invoke(source, "--allow-dirty").returncode == 0


@pytest.mark.parametrize("damage", ["version", "revision", "archive"])
def test_identity_or_archive_mismatch_cannot_be_bypassed(source, damage):
    root, recovery = source
    if damage == "version":
        (root / "VERSION").write_text("0.4.46\n")
    elif damage == "revision":
        index = recovery / "index.json"
        index.write_text(index.read_text().replace(REVISION, "b" * 40))
    else:
        (recovery / "TSP-v0.4.67.tar.gz").write_bytes(b"corrupt")
    result = invoke(source, "--allow-dirty")
    assert result.returncode == 2
    assert "source check failed:" in result.stderr


def test_untracked_runtime_directories_are_not_source_drift(source):
    root, _ = source
    for name in ["node_modules/lib.js", "backend/.venv/lib.py", "data/private.db"]:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not source")
    result = invoke(source)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["excluded_untracked_directories"]


def test_version_only_copy_has_no_git_identity(source):
    result = invoke(source, recovery=False)
    assert result.returncode == 2
    assert "source check failed:" in result.stderr


def test_symlinked_parent_is_not_read_as_archived_source(source):
    root, _ = source
    outside = root.parent / "outside"
    (root / "backend").rename(outside)
    (root / "backend").symlink_to(outside, target_is_directory=True)
    result = invoke(source)
    assert result.returncode == 1
    assert {"path": "backend/app.py", "kind": "symlink"} in json.loads(result.stdout)["changes"]


@pytest.mark.parametrize("change", ["clean", "dirty", "wrong_revision", "nested"])
def test_git_mode_checks_root_head_and_drift_in_disposable_repo(source, change):
    root, _ = source

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Source Entry Test",
             "-c", "user.email=test@example.invalid", *args],
            cwd=root, text=True, stderr=subprocess.PIPE,
        ).strip()

    git("init", "-b", "main")
    git("add", "VERSION", "backend/app.py")
    git("commit", "-m", "disposable source fixture")
    revision = git("rev-parse", "HEAD")
    target = root
    if change == "dirty":
        (root / "backend/app.py").write_text("modified\n")
    elif change == "wrong_revision":
        revision = REVISION
    elif change == "nested":
        target = root / "backend"
        (target / "VERSION").write_text("0.4.67\n")
    args = [sys.executable, str(ENTRY), "--root", str(target),
            "--version", "0.4.67", "--revision", revision]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if change in {"wrong_revision", "nested"}:
        assert result.returncode == 2
        assert "source check failed:" in result.stderr
    else:
        assert result.returncode == (1 if change == "dirty" else 0), result.stderr
        report = json.loads(result.stdout)
        assert report["basis"] == "git_head"
        assert report["git_head"] == revision
        assert report["clean"] is (change == "clean")
    expected = 2 if change in {"wrong_revision", "nested"} else 0
    assert subprocess.run(
        [*args, "--allow-dirty"], capture_output=True, check=False,
    ).returncode == expected
