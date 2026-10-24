from __future__ import annotations

import json
import subprocess

import pytest

import advance_version
import check_version
import test_advance_version as fixtures
from versioning import next_version

git = fixtures.git
repository = fixtures.repository
main_event = fixtures.main_event


def test_branch_keeps_its_fork_version_after_main_advances(main_event):
    root, base = main_event
    git(root, "checkout", "-b", "feature")
    git(root, "commit", "--allow-empty", "-m", "Feature")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "main")
    revision = advance_version.advance_main(root, base)
    check_version.check_branch_version(root, revision, head)


def test_branch_rejects_manual_bump_even_when_all_sources_match(repository):
    base = git(repository, "rev-parse", "HEAD")
    advance_version.update_versions(repository)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Manual bump")
    with pytest.raises(ValueError, match="branch.*version"):
        check_version.check_branch_version(
            repository, base, git(repository, "rev-parse", "HEAD")
        )


def test_branch_can_update_from_main_without_counting_bot_bump(main_event):
    root, source = main_event
    git(root, "checkout", "-b", "feature")
    git(root, "commit", "--allow-empty", "-m", "Feature")
    git(root, "checkout", "main")
    revision = advance_version.advance_main(root, source)
    git(root, "checkout", "feature")
    git(root, "merge", "--no-ff", revision, "-m", "Update feature from main")
    check_version.check_branch_version(root, revision, git(root, "rev-parse", "HEAD"))


def test_cli_branch_check_is_enforced_on_actual_pr_head(repository):
    base = git(repository, "rev-parse", "HEAD")
    advance_version.update_versions(repository)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Manual bump")
    head = git(repository, "rev-parse", "HEAD")
    result = subprocess.run(
        [
            fixtures.sys.executable, str(check_version.__file__),
            "--current", str(repository / "VERSION"),
            "--branch-base", base, "--branch-head", head,
        ],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "branch" in result.stderr


def test_invalid_version_trailer_cannot_skip_allocation(main_event):
    root, source = main_event
    git(root, "commit", "--allow-empty", "-m", f"TSP-Version-Source: {source}")
    git(root, "push", "origin", "main")
    with pytest.raises(ValueError, match="invalid automatic version commit"):
        advance_version.advance_main(root, source)


def test_rerunning_bot_push_never_allocates_another_version(main_event):
    root, source = main_event
    revision = advance_version.advance_main(root, source)
    assert advance_version.advance_main(root, revision) == revision
    assert git(root, "ls-remote", "origin", "main").split()[0] == revision


def test_main_version_rollback_is_rejected_before_writing(main_event):
    root, source = main_event
    before = fixtures.snapshot(root)
    revision = advance_version.advance_main(root, source)
    git(root, "checkout", "-B", "main", revision)
    for name, content in before.items():
        (root / name).write_bytes(content)
    git(root, "add", ".")
    git(root, "commit", "-m", "Accidental rollback from old branch")
    rollback = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    with pytest.raises(ValueError, match="main.*version"):
        advance_version.advance_main(root, rollback)
    assert fixtures.snapshot(root) == before
    assert git(root, "ls-remote", "origin", "main").split()[0] == rollback


def test_release_resolves_old_tag_even_after_main_advances(main_event):
    import prepare_release

    root, source = main_event
    initial = fixtures.current_version(root)
    git(root, "tag", "-a", f"v{initial}", "-m", "Tested source")
    git(root, "push", "origin", f"refs/tags/v{initial}")
    advance_version.advance_main(root, source)
    result = prepare_release.prepare(root, f"v{initial}", "windows,linux")
    assert result["revision"] == source
    assert result["ver"] == initial
    assert [x["platform"] for x in json.loads(result["matrix"])["include"]] == [
        "windows", "linux",
    ]
    assert all(initial in x["artifact"] for x in json.loads(result["matrix"])["include"])


def test_release_missing_tag_never_creates_one(main_event):
    import prepare_release

    root, _ = main_event
    tag = "v" + fixtures.current_version(root)
    with pytest.raises(ValueError, match="tag"):
        prepare_release.prepare(root, tag, "windows")
    assert git(root, "ls-remote", "origin", f"refs/tags/{tag}") == ""


def test_release_tag_must_be_integrated_and_match_version(main_event):
    import prepare_release

    root, _ = main_event
    version = fixtures.current_version(root)
    git(root, "tag", f"v{next_version(version)}")
    git(root, "push", "origin", f"refs/tags/v{next_version(version)}")
    with pytest.raises(ValueError, match="version"):
        prepare_release.prepare(root, f"v{next_version(version)}", "windows")
    git(root, "checkout", "-b", "unmerged")
    git(root, "commit", "--allow-empty", "-m", "Unmerged")
    git(root, "tag", f"v{version}")
    git(root, "push", "origin", f"refs/tags/v{version}")
    with pytest.raises(ValueError, match="main"):
        prepare_release.prepare(root, f"v{version}", "windows")


@pytest.mark.parametrize("platforms", ["", "windwos", "windows,unknown", "windows,windows"])
def test_release_invalid_platform_selection_is_rejected(main_event, platforms):
    import prepare_release

    root, _ = main_event
    with pytest.raises(ValueError, match="platform"):
        prepare_release.prepare(root, "v" + fixtures.current_version(root), platforms)


def test_latest_promotion_skips_an_older_completed_build(main_event):
    import prepare_release

    root, source = main_event
    assert prepare_release.is_current_main(root, source)
    revision = advance_version.advance_main(root, source)
    assert not prepare_release.is_current_main(root, source)
    assert prepare_release.is_current_main(root, revision)
