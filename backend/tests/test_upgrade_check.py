"""Real temporary Git histories: materialize evidence without changing the checkout."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/upgrade_check.py"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "backend/app").mkdir(parents=True)
    (root / "backend/app/test file.py").write_text("old\n")
    (root / ".env").write_text("never include\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    git(root, "tag", "base")
    git(root, "checkout", "-qb", "candidate")
    (root / "backend/app/test file.py").write_text("upstream\n")
    git(root, "commit", "-qam", "upstream")
    git(root, "checkout", "-qb", "local", "base")
    (root / "backend/app/test file.py").write_text("local\n")
    git(root, "commit", "-qam", "custom")
    return root


def test_git_bundle_keeps_committed_index_worktree_and_conflict_materials(tmp_path):
    root = repo(tmp_path)
    (root / "backend/app/test file.py").write_text("staged\n")
    git(root, "add", ".")
    (root / "backend/app/test file.py").write_text("unstaged\n")
    (root / "backend/app/new.py").write_bytes(b"\x00binary\r\n")
    (root / ".env").write_text("private changed\n")
    before = git(root, "status", "--porcelain=v1", "-z")
    index = (root / ".git/index").read_bytes()
    head = git(root, "rev-parse", "HEAD")
    output = tmp_path / "bundle"
    result = subprocess.run([
        sys.executable, str(SCRIPT), "candidate", "--repo", str(root), "--bundle", str(output),
    ], capture_output=True, text=True)
    assert result.returncode == 2, result.stderr
    summary = json.loads((output / "report.json").read_bytes())
    assert summary["complete"]
    assert summary["conflicts"] == ["backend/app/test file.py"]
    assert summary["excluded_paths"] == [".env"]
    with zipfile.ZipFile(output / "sources.zip") as archive:
        assert archive.read("base/backend/app/test file.py") == b"old\n"
        assert archive.read("current/backend/app/test file.py") == b"local\n"
        assert archive.read("target/backend/app/test file.py") == b"upstream\n"
        assert archive.read("index/backend/app/test file.py") == b"staged\n"
        assert archive.read("working/backend/app/test file.py") == b"unstaged\n"
        assert archive.read("working/backend/app/new.py") == b"\x00binary\r\n"
        assert not any(".env" in path for path in archive.namelist())
    assert b"staged" in (output / "staged.patch").read_bytes()
    assert b"unstaged" in (output / "unstaged.patch").read_bytes()
    assert b"private changed" not in (output / "unstaged.patch").read_bytes()
    assert git(root, "status", "--porcelain=v1", "-z") == before
    assert (root / ".git/index").read_bytes() == index
    assert git(root, "rev-parse", "HEAD") == head


def test_ref_option_and_symlink_backup_rejected(tmp_path):
    root = repo(tmp_path)
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--", "--help",
    ], cwd=root, capture_output=True)
    assert result.returncode == 1
    (root / "backend/app/link.py").symlink_to("../../../.env")
    output = tmp_path / "bundle"
    result = subprocess.run([
        sys.executable, str(SCRIPT), "candidate", "--repo", str(root), "--bundle", str(output),
    ], capture_output=True)
    assert result.returncode == 1
    assert not (output / "report.json").exists()


def test_existing_bundle_is_not_overwritten(tmp_path):
    root = repo(tmp_path)
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "keep").write_text("keep")
    result = subprocess.run([
        sys.executable, str(SCRIPT), "candidate", "--repo", str(root), "--bundle", str(output),
    ], capture_output=True)
    assert result.returncode == 1
    assert (output / "keep").read_text() == "keep"


def test_binary_overlap_does_not_crash_text_preview(tmp_path):
    root = repo(tmp_path)
    git(root, "checkout", "-qb", "binary-target", "base")
    (root / "backend/app/binary").write_bytes(b"\x89PNG\x00upstream")
    git(root, "add", ".")
    git(root, "commit", "-qm", "binary upstream")
    git(root, "checkout", "local")
    (root / "backend/app/binary").write_bytes(b"\x89PNG\x00custom")
    git(root, "add", ".")
    git(root, "commit", "-qm", "binary custom")
    output = tmp_path / "binary-bundle"
    result = subprocess.run([
        sys.executable, str(SCRIPT), "binary-target", "--repo", str(root), "--bundle", str(output),
    ], capture_output=True, text=True)
    assert result.returncode in (0, 2), result.stderr
    report = json.loads((output / "report.json").read_bytes())
    assert "backend/app/binary" in report["overlap"]
    with zipfile.ZipFile(output / "sources.zip") as archive:
        assert archive.read("current/backend/app/binary") == b"\x89PNG\x00custom"
