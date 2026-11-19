"""Upgrade workspace without discarding staged or unstaged development."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/release/sync_workspace.py"
spec = importlib.util.spec_from_file_location("sync_workspace", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "source.txt").write_text("base\nmiddle\nlast\n")
    (root / "removed.txt").write_text("keep deletion\n")
    git(root, "add", "source.txt", "removed.txt")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "baseline")
    base = git(root, "rev-parse", "HEAD")
    (root / "source.txt").write_text("upstream\nmiddle\nlast\n")
    (root / "new.txt").write_text("latest\n")
    git(root, "add", "source.txt", "new.txt")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "upstream")
    target = git(root, "rev-parse", "HEAD")
    git(root, "switch", "--detach", base)
    return root, target, tmp_path / "protected"


def test_preserves_both_development_layers_and_deletions(repository):
    root, target, protected = repository
    (root / "source.txt").write_text("base\nmiddle\nstaged\n")
    git(root, "add", "source.txt")
    (root / "source.txt").write_text("base\nmiddle\nunstaged\n")
    (root / "removed.txt").unlink()
    (root / "notes.txt").write_text("private unfinished work\n")

    report = module.sync_workspace(root, target, protected, apply=True)

    assert report["status"] == "passed"
    assert git(root, "rev-parse", "HEAD") == target
    assert git(root, "show", ":source.txt") == "upstream\nmiddle\nstaged"
    assert (root / "source.txt").read_text() == "upstream\nmiddle\nunstaged\n"
    assert not (root / "removed.txt").exists()
    assert (root / "new.txt").read_text() == "latest\n"
    assert (root / "notes.txt").read_text() == "private unfinished work\n"
    assert (protected / "work.tar").exists()
    assert (protected / "index").exists()


def test_conflict_never_changes_head_index_or_work(repository):
    root, target, protected = repository
    (root / "source.txt").write_text("local conflict\nmiddle\nlast\n")
    before = git(root, "rev-parse", "HEAD"), git(root, "write-tree")
    report = module.sync_workspace(root, target, protected, apply=True)
    assert report["status"] == "conflict"
    assert "source.txt" in report["conflicts"]
    assert (git(root, "rev-parse", "HEAD"), git(root, "write-tree")) == before
    assert (root / "source.txt").read_text() == "local conflict\nmiddle\nlast\n"


def test_untracked_collision_is_preserved_and_blocks_upgrade(repository):
    root, target, protected = repository
    (root / "new.txt").write_text("untracked user content\n")
    report = module.sync_workspace(root, target, protected, apply=True)
    assert report["status"] == "conflict"
    assert "new.txt" in report["conflicts"]
    assert (root / "new.txt").read_text() == "untracked user content\n"
    assert git(root, "rev-parse", "HEAD") != target


def test_preview_does_not_create_protection_or_change_workspace(repository):
    root, target, protected = repository
    before = git(root, "rev-parse", "HEAD"), git(root, "write-tree")
    report = module.sync_workspace(root, target, protected)
    assert report["status"] == "ready"
    assert not protected.exists()
    assert (git(root, "rev-parse", "HEAD"), git(root, "write-tree")) == before


def test_explicit_preservation_updates_base_without_overwriting_conflict(repository):
    root, target, protected = repository
    (root / "source.txt").write_text("unfinished conflict\nmiddle\nlast\n")
    report = module.sync_workspace(root, target, protected, apply=True, preserve_conflicts=True)
    assert report["status"] == "preserved"
    assert git(root, "rev-parse", "HEAD") == target
    assert (root / "source.txt").read_text() == "unfinished conflict\nmiddle\nlast\n"
    assert (root / "new.txt").read_text() == "latest\n"


@pytest.mark.parametrize("phase", ["file", "head"])
def test_failed_application_restores_head_index_and_work(repository, monkeypatch, phase):
    root, target, protected = repository
    (root / "source.txt").write_text("base\nmiddle\nunfinished\n")
    git(root, "add", "source.txt")
    (root / "source.txt").write_text("base\nmiddle\nnewer unfinished\n")
    before = git(root, "rev-parse", "HEAD"), git(root, "write-tree")
    original = {path: module.read_work(root / path)
                for path in ("source.txt", "new.txt", "removed.txt")}
    if phase == "file":
        replace = Path.replace
        def fail_replace(self, destination):
            if Path(destination) == root / "source.txt" and self.name.startswith("tmp"):
                raise OSError("injected file failure")
            return replace(self, destination)
        monkeypatch.setattr(Path, "replace", fail_replace)
    else:
        original_git = module.git
        def fail_git(repo, *args, **kwargs):
            if args[0] == "update-ref" and target in args:
                raise OSError("injected head failure")
            return original_git(repo, *args, **kwargs)
        monkeypatch.setattr(module, "git", fail_git)
    with pytest.raises(OSError, match="injected"):
        module.sync_workspace(root, target, protected, apply=True)
    assert (git(root, "rev-parse", "HEAD"), git(root, "write-tree")) == before
    assert {path: module.read_work(root / path) for path in original} == original
