from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    # BaseLoader preserves GitHub's "on" key instead of treating it as YAML 1.1 true.
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def step_using(job, action):
    return next(step for step in job["steps"] if step.get("uses") == action)


def test_main_events_are_not_cancelled_and_prs_only_check_versions():
    ci = workflow("ci.yml")
    assert ci["on"]["push"]["branches"] == ["main"]
    assert ci["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )
    assert "github.run_id" in ci["concurrency"]["group"]
    version = ci["jobs"]["version"]
    selection = next(step for step in version["steps"] if step.get("id") == "revision")
    assert 'if [ "$GITHUB_EVENT_NAME" = "push" ]' in selection["run"]
    assert '[ "$GITHUB_REF" = "refs/heads/main" ]' in selection["run"]
    assert "python3 scripts/advance_version.py" in selection["run"]
    assert "python3 scripts/check_version.py --current VERSION" in selection["run"]
    assert '--branch-base "$PR_BASE" --branch-head "$PR_HEAD"' in selection["run"]
    assert ci["on"]["merge_group"]["types"] == ["checks_requested"]
    assert "--previous" not in selection["run"]


def test_quality_jobs_and_docker_use_same_recorded_commit():
    ci = workflow("ci.yml")
    version = ci["jobs"]["version"]
    assert version["outputs"]["revision"] == "${{ steps.revision.outputs.revision }}"
    assert ci["permissions"]["contents"] == "read"
    assert version["permissions"]["contents"] == "write"
    artifact = step_using(version, "actions/upload-artifact@v4")["with"]
    assert artifact["name"] == "ci-revision"
    assert artifact["overwrite"] == "true"
    for name in ("backend", "frontend"):
        job = ci["jobs"][name]
        assert job["needs"] == "version"
        checkout = step_using(job, "actions/checkout@v4")["with"]
        assert checkout["ref"] == "${{ needs.version.outputs.revision }}"
        assert checkout["persist-credentials"] == "false"

    docker = workflow("docker.yml")["jobs"]["build-and-push"]
    assert "github.event.workflow_run.event == 'push'" in docker["if"]
    assert "github.event.workflow_run.head_branch == 'main'" in docker["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in docker["if"]
    download = step_using(docker, "actions/download-artifact@v4")
    assert download["if"] == "github.event_name == 'workflow_run'"
    assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"
    assert download["with"]["name"] == artifact["name"]
    checkout = step_using(docker, "actions/checkout@v4")["with"]
    assert checkout["ref"] == "${{ steps.revision.outputs.revision }}"
    metadata = step_using(docker, "docker/metadata-action@v5")["with"]
    assert "${{ steps.revision.outputs.revision }}" in metadata["tags"]
    assert metadata["labels"].strip() == (
        "org.opencontainers.image.revision=${{ steps.revision.outputs.revision }}"
    )
    assert (
        "type=raw,value=${{ steps.application-version.outputs.version }},"
        "enable=${{ github.event_name == 'workflow_run' }}"
    ) in metadata["tags"]
    application_version = next(
        step for step in docker["steps"] if step.get("id") == "application-version"
    )
    assert "python3 scripts/check_version.py --current VERSION" in application_version["run"]
    assert "workflow_run.head_sha" not in str(docker)


@pytest.mark.parametrize(
    ("event", "artifact", "expected"),
    [
        ("workflow_run", "a" * 40 + "\n", "a" * 40),
        ("workflow_run", None, None),
        ("workflow_run", "invalid\n", None),
        ("workflow_run", "a" * 40 + "\nextra\n", None),
        ("workflow_dispatch", None, "b" * 40),
        ("push", None, "b" * 40),
    ],
)
def test_docker_revision_selection_fails_closed(tmp_path, event, artifact, expected):
    docker = workflow("docker.yml")["jobs"]["build-and-push"]
    select = next(step for step in docker["steps"] if step.get("id") == "revision")
    artifact_dir = tmp_path / "ci-revision"
    artifact_dir.mkdir()
    if artifact is not None:
        (artifact_dir / "revision.txt").write_text(artifact)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", select["run"]],
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_SHA": "b" * 40,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
    )
    if expected is None:
        assert result.returncode != 0
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"revision={expected}\n"


def test_desktop_release_requires_explicit_version_without_stale_default():
    version = workflow("release.yml")["on"]["workflow_dispatch"]["inputs"]["version"]
    assert version["required"] == "true"
    assert "default" not in version


def test_version_tag_requires_successful_main_quality_gates():
    ci = workflow("ci.yml")
    tag = ci["jobs"]["tag"]
    assert set(tag["needs"]) == {"version", "backend", "frontend"}
    assert tag["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert tag["permissions"]["contents"] == "write"
    checkout = step_using(tag, "actions/checkout@v4")["with"]
    assert checkout["ref"] == "${{ needs.version.outputs.revision }}"
    assert checkout["fetch-depth"] == "0"
    assert checkout["persist-credentials"] == "true"
    publish = next(step for step in tag["steps"] if step.get("id") == "tag")
    assert publish["env"]["TESTED_REVISION"] == "${{ needs.version.outputs.revision }}"
    assert publish["run"] == 'python3 scripts/tag_version.py "$TESTED_REVISION"'
    version_tests = next(
        step for step in ci["jobs"]["backend"]["steps"]
        if step.get("name") == "版本规则测试"
    )
    assert "../scripts/test_tag_version.py" in version_tests["run"]
    assert "../scripts/test_collaborative_versioning.py" in version_tests["run"]


def test_desktop_release_pins_every_job_to_existing_tag_revision():
    release = workflow("release.yml")
    assert release["permissions"]["contents"] == "read"
    assert release["concurrency"]["cancel-in-progress"] == "false"
    assert "inputs.version" in release["concurrency"]["group"]
    prepare = release["jobs"]["prepare"]
    select = next(step for step in prepare["steps"] if step.get("id") == "filter")
    assert select["run"].strip() == "python3 scripts/prepare_release.py"
    assert select["env"]["RELEASE_TAG"] == "${{ inputs.version }}"
    assert select["env"]["RELEASE_PLATFORMS"] == "${{ inputs.platforms }}"
    assert release["jobs"]["quality"]["needs"] == "prepare"
    for name in ("quality", "build"):
        checkout = step_using(release["jobs"][name], "actions/checkout@v4")["with"]
        assert checkout["ref"] == "${{ needs.prepare.outputs.revision }}"
        assert checkout["persist-credentials"] == "false"
    manifest = release["jobs"]["manifest"]
    assert set(manifest["needs"]) == {"prepare", "build"}
    assert "if" not in manifest
    upload = step_using(release["jobs"]["build"], "softprops/action-gh-release@v2")
    assert upload["with"]["target_commitish"] == "${{ needs.prepare.outputs.revision }}"
    assert upload["with"]["draft"] == "true"


def test_docker_latest_only_promoted_for_current_main_after_build():
    docker = workflow("docker.yml")
    build = docker["jobs"]["build-and-push"]
    tags = step_using(build, "docker/metadata-action@v5")["with"]["tags"]
    assert "value=latest" not in tags
    assert "type=raw,value=${{ steps.revision.outputs.revision }}\n" in tags
    promotion = docker["jobs"]["promote-latest"]
    assert promotion["needs"] == "build-and-push"
    assert promotion["if"] == "github.event_name == 'workflow_run'"
    assert promotion["concurrency"]["cancel-in-progress"] == "false"
    publish = promotion["steps"][-1]
    assert "is_current_main(Path.cwd(), revision)" in publish["run"]
    assert publish["env"]["BUILD_REVISION"] == "${{ needs.build-and-push.outputs.revision }}"


def test_history_and_tags_are_protected_even_when_automation_bypasses_integration():
    policies = {
        name: json.loads((ROOT / ".github/rulesets" / f"{name}.json").read_text())
        for name in ("main-history", "main-integration", "main-quality", "version-tags")
    }
    for name in ("main-history", "version-tags"):
        assert policies[name]["bypass_actors"] == []
    assert {rule["type"] for rule in policies["main-history"]["rules"]} == {
        "deletion", "non_fast_forward",
    }
    assert {rule["type"] for rule in policies["version-tags"]["rules"]} == {
        "update", "deletion",
    }
    assert policies["version-tags"]["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
    quality = policies["main-quality"]
    assert all(actor["actor_type"] == "Integration" for actor in quality["bypass_actors"])
    checks = quality["rules"][0]["parameters"]
    assert checks["strict_required_status_checks_policy"] is True
    assert {check["context"] for check in checks["required_status_checks"]} == {
        "version", "backend", "frontend",
    }


@pytest.mark.parametrize("draft", [True, False])
def test_desktop_preflight_accepts_draft_and_rejects_published_release(tmp_path, draft):
    prepare = workflow("release.yml")["jobs"]["prepare"]
    step = next(step for step in prepare["steps"] if step.get("name") == "拒绝覆盖已发布版本")
    sample = [[{"tag_name": "v0.4.4", "draft": draft}]]
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf '%s' '" + json.dumps(sample) + "'\n")
    fake_gh.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path), "RELEASE_TAG": "v0.4.4",
            "GITHUB_REPOSITORY": "example/repository",
        },
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) is draft
    if not draft:
        assert "已经发布并冻结" in result.stderr
