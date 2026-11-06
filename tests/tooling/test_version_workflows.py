from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    # BaseLoader preserves GitHub's "on" key instead of treating it as YAML 1.1 true.
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def step_using(job, action):
    return next(step for step in job["steps"] if step.get("uses") == action)


@pytest.mark.parametrize("stored", ["v0.4.50", "0.4.50"])
@pytest.mark.parametrize("role", ["runtime", "compute", "identity", "promote"])
def test_release_workflow_uses_canonical_version_with_repository_prefix(tmp_path, stored, role):
    name = "docker-promote.yml" if role == "promote" else "docker.yml"
    steps = workflow(name)["jobs"][role]["steps"]
    if role in ("runtime", "compute"):
        command = next(step["run"] for step in steps if step.get("id") in ("inputs", "version"))
    else:
        command = next(step["run"] for step in steps if "image_manifest.py" in step.get("run", ""))
    (tmp_path / "VERSION").write_text(stored + "\n")
    output = tmp_path / "output"
    assignments = [line.strip() for line in command.splitlines()
                   if line.strip().startswith(("version=", 'echo "version='))]
    if role in ("identity", "promote"):
        argument = re.search(r'--version ("[^"]+")', command)[1]
        assignments.append(f'printf "version=%s\\n" {argument} >> "$GITHUB_OUTPUT"')
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", "\n".join(assignments)],
        cwd=tmp_path, env={**os.environ, "GITHUB_OUTPUT": str(output)},
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "version=0.4.50\n"


def test_promotion_uses_single_prefix_version_tag():
    command = next(step["run"] for step in workflow("docker-promote.yml")["jobs"]["promote"]["steps"]
                   if "image_manifest.py" in step.get("run", ""))
    assignments = [line.strip() for line in command.splitlines()
                   if line.strip().startswith("version=")]
    target = re.search(r'git rev-parse ("[^"]+")', command)[1]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c",
         "\n".join([*assignments, f"printf '%s\\n' {target}"])],
        cwd=ROOT, text=True, capture_output=True,
    )
    expected = "refs/tags/v" + (ROOT / "VERSION").read_text().strip().removeprefix("v") + "^{commit}\n"
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


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

    structure = next(
        step for step in ci["jobs"]["backend"]["steps"]
        if step.get("name") == "检查工程目录与关键入口"
    )
    assert structure["working-directory"] == "."
    assert structure["run"] == "python3 scripts/check_repository.py"

    documentation = next(
        step for step in ci["jobs"]["backend"]["steps"]
        if step.get("name") == "检查文档链接与需求契约"
    )
    assert documentation["working-directory"] == "."
    assert documentation["run"] == "python3 scripts/check_docs.py"

    containers = ci["jobs"]["containers"]
    assert containers["needs"] == "version"
    assert containers["uses"] == "./.github/workflows/docker.yml"
    assert containers["with"]["revision"] == "${{ needs.version.outputs.revision }}"
    docker = workflow("docker.yml")
    assert "workflow_run" not in docker["on"]
    assert "workflow_call" in docker["on"]
    for name in ("runtime", "compute", "backend-quality", "frontend-quality"):
        checkout = step_using(docker["jobs"][name], "actions/checkout@v4")["with"]
        assert checkout["ref"] == "${{ needs.resolve.outputs.revision }}"
    assert "runtime" in docker["jobs"]["compute"]["needs"]
    build = step_using(docker["jobs"]["compute"], "docker/build-push-action@v6")["with"]
    assert "RUNTIME_IMAGE=${{ needs.runtime.outputs.image }}" in build["build-args"]
    assert "@${{ needs.runtime.outputs.digest }}" in build["build-args"]
    assert build["platforms"] == "linux/amd64,linux/arm64"


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("a" * 40, "a" * 40), ("", None), ("invalid", None),
        ("a" * 40 + "\nextra", None), ("b" * 40, "b" * 40),
    ],
)
def test_docker_revision_selection_fails_closed(tmp_path, candidate, expected):
    docker = workflow("docker.yml")["jobs"]["resolve"]
    select = next(step for step in docker["steps"] if step.get("id") == "revision")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", select["run"]],
        env={
            **os.environ,
            "BUILD_REVISION": candidate,
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
    assert set(tag["needs"]) == {"version", "backend", "frontend", "containers"}
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
    assert version_tests["run"] == "uv run --frozen --no-sync pytest ../tests/tooling -q"


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


def test_desktop_standalone_smoke_isolates_runtime_configuration():
    build = workflow("release.yml")["jobs"]["build"]
    smoke = next(
        step for step in build["steps"]
        if step.get("name") == "验证 macOS 和 Linux 本地版无需登录"
    )
    command = smoke["run"]

    assert "TICKFLOW_ENV_FILE=/dev/null" in command
    assert "MULTITENANCY_ENABLED=false" in command
    assert "SERVICE_ROLE=standalone" in command
    assert 'DATA_DIR="$data_dir"' in command
    assert 'data_dir="$RUNNER_TEMP/desktop-smoke"' in command
    assert 'log="$data_dir/desktop.log"' in command


def test_docker_latest_only_promoted_for_current_main_after_build():
    ci = workflow("ci.yml")
    assert set(ci["jobs"]["promote"]["needs"]) == {
        "version", "backend", "frontend", "containers", "tag",
    }
    docker = workflow("docker.yml")
    for name in ("runtime", "compute"):
        build = step_using(docker["jobs"][name], "docker/build-push-action@v6")["with"]
        assert "ci-${{ github.run_id }}-${{ github.run_attempt }}" in build["tags"]
        assert "latest" not in build["tags"]
    promotion = workflow("docker-promote.yml")["jobs"]["promote-latest"]
    assert promotion["needs"] == "promote"
    assert promotion["if"] == "${{ inputs.promote_latest }}"
    assert promotion["concurrency"]["cancel-in-progress"] == "false"
    assert promotion["concurrency"]["group"] == "docker-latest-main"
    publish = promotion["steps"][-1]
    assert "is_current_main(Path.cwd(), revision)" in publish["run"]
    assert publish["env"]["BUILD_REVISION"] == "${{ inputs.revision }}"


def test_private_repository_main_check_receives_ephemeral_authentication(tmp_path):
    promotion = workflow("docker-promote.yml")["jobs"]["promote-latest"]
    publish = promotion["steps"][-1]
    assert publish["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import base64,os,sys\n"
        "assert os.environ['GIT_CONFIG_COUNT']=='1'\n"
        "assert os.environ['GIT_CONFIG_KEY_0']=='http.https://github.com/.extraheader'\n"
        "assert os.environ['GIT_CONFIG_VALUE_0']=='AUTHORIZATION: basic '+"
        "base64.b64encode(b'x-access-token:test-only-token').decode()\n"
        "print('a'*40+'\\trefs/heads/main')\n"
    )
    fake_git.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", publish["run"]],
        cwd=ROOT,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GH_TOKEN": "test-only-token", "BUILD_REVISION": "b" * 40},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "main has advanced" in result.stdout
    assert "test-only-token" not in result.stdout + result.stderr


def test_manual_builds_cannot_promote_without_full_quality_and_image_matrix():
    docker = workflow("docker.yml")
    promote = docker["jobs"]["promote"]
    assert set(promote["needs"]) == {
        "resolve", "backend-quality", "frontend-quality", "runtime", "compute", "identity",
    }
    assert "inputs.revision == ''" in promote["if"]
    for name in ("backend-quality", "frontend-quality"):
        assert "inputs.revision == ''" in docker["jobs"][name]["if"]
    assert "pytest ../tests/tooling -q" in str(docker["jobs"]["backend-quality"])
    identity_job = docker["jobs"]["identity"]
    assert set(identity_job["needs"]) == {"resolve", "runtime", "compute"}
    assert "scripts/release/image_manifest.py" in str(identity_job)
    assert "runtime_digest" in docker["on"]["workflow_call"]["outputs"]
    assert "compute_digest" in docker["on"]["workflow_call"]["outputs"]


def test_dependency_layers_do_not_depend_on_readme_or_project_version():
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split(" AS runtime\n", 1)[1]
    assert runtime.index("runtime-locked.txt") < runtime.index("COPY README.md")
    assert runtime.index("uv pip sync") < runtime.index("COPY backend/pyproject.toml")
    assert "--require-hashes" in runtime
    assert "AS dependency-exporter" in dockerfile
    for arg in ("PYTHON_IMAGE", "RUST_IMAGE", "NODE_ALPINE_IMAGE", "NODE_IMAGE"):
        assert f"FROM ${{{arg}}}" in dockerfile
        assert arg in str(workflow("docker.yml")["jobs"]["runtime"])
    assert "USE_CN_MIRROR" in (ROOT / "deploy/full/Compute.Dockerfile").read_text()


def test_native_cache_survives_application_version_allocation_but_not_cargo_change(tmp_path):
    build = step_using(workflow("docker.yml")["jobs"]["runtime"], "docker/build-push-action@v6")
    expression = re.search(r"NATIVE_CACHE_KEY=.*?hashFiles\(([^)]+)\)", build["with"]["build-args"])
    assert expression is not None
    patterns = re.findall(r"'([^']+)'", expression[1])
    native = tmp_path / "backend/native/rustdx_native"
    native.mkdir(parents=True)
    cargo = native / "Cargo.lock"
    cargo.write_text("native dependency 1\n")
    application = tmp_path / "backend/uv.lock"
    application.write_text("application version 0.4.50\n")

    def fingerprint():
        paths = {Path(path) for pattern in patterns
                 for path in glob.glob(str(tmp_path / pattern), recursive=True) if Path(path).is_file()}
        assert cargo in paths
        return hashlib.sha256(b"".join(hashlib.sha256(path.read_bytes()).digest()
                                      for path in sorted(paths))).hexdigest()

    before = fingerprint()
    application.write_text("application version 0.4.51\n")
    assert fingerprint() == before
    cargo.write_text("native dependency 2\n")
    assert fingerprint() != before


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
