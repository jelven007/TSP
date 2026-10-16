from __future__ import annotations

import os
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
