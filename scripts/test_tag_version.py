from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import test_advance_version as version_tests
from versioning import next_version

git = version_tests.git
main_event = version_tests.main_event
repository = version_tests.repository
snapshot = version_tests.snapshot


def publisher():
    # Import after fixtures are collected so a missing implementation fails a test.
    import tag_version

    return tag_version


def version_tag(root):
    return "v" + (root / "VERSION").read_text().strip().removeprefix("v")


def remote_target(root, tag):
    return git(root, "ls-remote", "origin", f"refs/tags/{tag}")


def test_creates_remote_tag_on_exact_tested_commit_without_changing_version(main_event):
    root, revision = main_event
    before = snapshot(root)
    tag = publisher().tag_main(root, revision)
    assert tag == version_tag(root)
    assert remote_target(root, tag).split()[0] == revision
    assert git(root, "ls-remote", "origin", "main").split()[0] == revision
    assert snapshot(root) == before
    assert git(root, "status", "--porcelain") == ""


def test_rerun_preserves_existing_annotated_tag(main_event):
    root, revision = main_event
    tag = version_tag(root)
    git(root, "tag", "-a", tag, "-m", "Existing version")
    git(root, "push", "origin", f"refs/tags/{tag}")
    before = remote_target(root, tag)
    assert publisher().tag_main(root, revision) == tag
    assert remote_target(root, tag) == before


def test_existing_tag_on_another_commit_is_never_moved(main_event):
    root, revision = main_event
    tag = version_tag(root)
    git(root, "commit", "--allow-empty", "-m", "Another commit")
    other = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    git(root, "tag", tag)
    git(root, "push", "origin", f"refs/tags/{tag}")
    git(root, "checkout", "--detach", revision)
    with pytest.raises(ValueError, match="already points"):
        publisher().tag_main(root, revision)
    assert remote_target(root, tag).split()[0] == other


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF", "refs/tags/v0.4.3"),
    ],
)
def test_only_main_push_job_can_create_tags(main_event, monkeypatch, key, value):
    root, revision = main_event
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="GitHub Actions push to main"):
        publisher().tag_main(root, revision)
    assert remote_target(root, version_tag(root)) == ""


@pytest.mark.parametrize("revision", ["HEAD", "a" * 39, "--all"])
def test_rejects_non_sha_revision(main_event, revision):
    root, _ = main_event
    with pytest.raises(ValueError, match="full tested commit SHA"):
        publisher().tag_main(root, revision)
    assert remote_target(root, version_tag(root)) == ""


def test_unpushed_commit_is_not_tagged(main_event):
    root, _ = main_event
    git(root, "commit", "--allow-empty", "-m", "Unpushed")
    revision = git(root, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="not integrated into origin/main"):
        publisher().tag_main(root, revision)
    assert remote_target(root, version_tag(root)) == ""


def test_dirty_checkout_is_preserved(main_event):
    root, revision = main_event
    (root / "README.md").write_text("user work\n")
    before = snapshot(root)
    with pytest.raises(ValueError, match="clean checkout"):
        publisher().tag_main(root, revision)
    assert snapshot(root) == before
    assert remote_target(root, version_tag(root)) == ""


def test_checkout_must_match_tested_revision(main_event):
    root, revision = main_event
    git(root, "commit", "--allow-empty", "-m", "Different checkout")
    with pytest.raises(ValueError, match="checkout does not match"):
        publisher().tag_main(root, revision)
    assert remote_target(root, version_tag(root)) == ""


def test_older_successful_run_does_not_tag_newer_main_commit(main_event):
    root, revision = main_event
    git(root, "commit", "--allow-empty", "-m", "Newer main")
    newest = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    git(root, "checkout", "--detach", revision)
    tag = publisher().tag_main(root, revision)
    assert remote_target(root, tag).split()[0] == revision
    assert git(root, "ls-remote", "origin", "main").split()[0] == newest


def test_concurrent_same_tag_publication_is_idempotent(main_event, monkeypatch):
    root, revision = main_event
    module = publisher()
    real_git = module._git
    raced = []
    tag = version_tag(root)

    def concurrent_git(root, *args, **kwargs):
        if args[0] == "push" and not raced:
            raced.append(True)
            git(root, "push", "origin", f"{revision}:refs/tags/{tag}")
            return subprocess.CompletedProcess(args, 1, "", "tag created concurrently")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(module, "_git", concurrent_git)
    assert module.tag_main(root, revision) == tag
    assert remote_target(root, tag).split()[0] == revision


def test_push_rejection_is_reported_without_force(main_event, monkeypatch):
    root, revision = main_event
    module = publisher()
    real_git = module._git
    pushes = []

    def denied_git(root, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1, "", "protected tag")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(module, "_git", denied_git)
    with pytest.raises(RuntimeError, match="protected tag"):
        module.tag_main(root, revision)
    assert pushes == [("push", "origin", f"{revision}:refs/tags/{version_tag(root)}")]
    assert remote_target(root, version_tag(root)) == ""


def test_metadata_mismatch_is_not_tagged(main_event):
    root, _ = main_event
    path = root / "frontend/package.json"
    package = json.loads(path.read_text())
    package["version"] = next_version(package["version"])
    path.write_text(json.dumps(package))
    git(root, "add", ".")
    git(root, "commit", "-m", "Mismatched metadata")
    revision = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    with pytest.raises(ValueError, match="version source mismatch"):
        publisher().tag_main(root, revision)
    assert remote_target(root, version_tag(root)) == ""


def test_cli_exports_tag_and_revision(main_event, monkeypatch):
    root, _ = main_event
    script = Path(__file__).with_name("tag_version.py")
    shutil.copyfile(script, root / "scripts/tag_version.py")
    git(root, "add", ".")
    git(root, "commit", "-m", "Tag publisher")
    revision = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    output = root.parent / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    result = subprocess.run(
        [sys.executable, "scripts/tag_version.py", revision], cwd=root,
        capture_output=True, text=True, env=os.environ,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"tag={version_tag(root)}\nrevision={revision}\n"
