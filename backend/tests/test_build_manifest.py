"""Release identity, bounded file scopes and offline upgrade evidence."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_manifest", ROOT / "scripts/build_manifest.py")
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


def tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("print('old')\n")
    (tmp_path / "static").mkdir()
    (tmp_path / "static/index.html").write_text("<html>old</html>")
    return tmp_path


def baseline(root):
    return manifest.create(root, ["app", "static"], version="0.4.2", commit="a" * 40)


def test_verify_detects_modification_deletion_and_addition(tmp_path):
    root = tree(tmp_path)
    before = baseline(root)
    assert manifest.verify(root, before) == {"changed": [], "missing": [], "added": []}
    (root / "app/main.py").write_text("print('custom')\n")
    (root / "static/index.html").unlink()
    (root / "app/new.py").write_text("new")
    assert manifest.verify(root, before) == {
        "changed": ["app/main.py"], "missing": ["static/index.html"], "added": ["app/new.py"],
    }
    assert before["commit"] == "a" * 40
    assert before["files_sha256"] != baseline(root)["files_sha256"]


def test_runtime_secrets_caches_and_data_are_never_scanned(tmp_path):
    root = tree(tmp_path)
    for rel in ("app/.env.prod", "app/data/key", "app/__pycache__/main.pyc", "app/.git/config"):
        file = root / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("secret")
    assert sorted(baseline(root)["files"]) == ["app/main.py", "static/index.html"]


@pytest.mark.parametrize("scope", ["../outside", "/etc", ".", "app/../data", r"app\other", "app:stream"])
def test_invalid_scope_rejected(tmp_path, scope):
    with pytest.raises(ValueError):
        manifest.create(tree(tmp_path), [scope], version="0.4.2", commit=None)


def test_missing_scope_and_unknown_provenance(tmp_path):
    root = tree(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        manifest.create(root, ["missing"], version="0.4.2", commit=None)
    unknown = manifest.create(root, ["app"], version="0.4.2", commit=None)
    assert unknown["commit"] is None
    with pytest.raises(ValueError, match="commit"):
        manifest.create(root, ["app"], version="0.4.2", commit="main")


def test_symlink_inside_bundle_is_hashed_and_escape_rejected(tmp_path):
    root = tree(tmp_path)
    (root / "app/link").symlink_to("main.py")
    before = baseline(root)
    assert before["files"]["app/link"]["kind"] == "symlink"
    assert manifest.verify(root, before)["changed"] == []
    (root / "app/escape").symlink_to("../../secret")
    with pytest.raises(ValueError, match="symlink"):
        baseline(root)


def test_symlink_to_unhashed_neighbor_rejected(tmp_path):
    root = tree(tmp_path)
    (root / "neighbor.py").write_text("not part of the application")
    (root / "app/link").symlink_to("../neighbor.py")
    with pytest.raises(ValueError, match="outside manifest scopes"):
        baseline(root)


def test_tampered_manifest_rejected_without_scanning_outside(tmp_path):
    root = tree(tmp_path)
    before = baseline(root)
    before["files"]["../secret"] = before["files"]["app/main.py"]
    with pytest.raises(ValueError):
        manifest.verify(root, before)


def test_offline_backup_preserves_bytes_and_never_restores(tmp_path):
    (tmp_path / "installed").mkdir()
    root = tree(tmp_path / "installed")
    before = baseline(root)
    candidate = json.loads(json.dumps(before))
    (root / "app/main.py").write_text("print('custom')\n")
    (root / "app/new.py").write_text("new extension")
    (root / "static/index.html").unlink()
    output = tmp_path / "upgrade"
    report = manifest.backup(root, before, candidate, output)
    assert report["files"]["app/main.py"] == "candidate_unchanged"
    assert report["files"]["app/new.py"] == "local_added"
    assert report["files"]["static/index.html"] == "local_deleted"
    assert (root / "app/main.py").read_text() == "print('custom')\n"
    with zipfile.ZipFile(output / "local-files.zip") as archive:
        assert archive.read("app/main.py") == b"print('custom')\n"
        assert "static/index.html" not in archive.namelist()
    assert manifest.verify(root, before)["missing"] == ["static/index.html"]
    with pytest.raises(FileExistsError):
        manifest.backup(root, before, candidate, output)


def test_upgrade_conflict_and_matching_changes(tmp_path):
    root = tree(tmp_path)
    before = baseline(root)
    (root / "app/main.py").write_text("print('new')\n")
    candidate = baseline(root)
    report = manifest.backup(root, before, candidate, tmp_path / "same")
    assert report["files"]["app/main.py"] == "matches_candidate"
    (root / "app/main.py").write_text("print('custom')\n")
    report = manifest.backup(root, before, candidate, tmp_path / "conflict")
    assert report["files"]["app/main.py"] == "conflict"


def test_backup_fails_if_payload_changes_during_copy(tmp_path, monkeypatch):
    root = tree(tmp_path)
    before = baseline(root)
    (root / "app/main.py").write_text("custom")
    original = manifest._entry
    calls = 0

    def racing_entry(root, rel):
        nonlocal calls
        result = original(root, rel)
        if rel == "app/main.py":
            calls += 1
            if calls == 1:
                (root / rel).write_text("changed while backing up")
        return result

    monkeypatch.setattr(manifest, "_entry", racing_entry)
    output = tmp_path / "race"
    with pytest.raises(ValueError, match="changed"):
        manifest.backup(root, before, before, output)
    assert not (output / "report.json").exists()


def test_cli_roundtrip_and_nonzero_mismatch(tmp_path):
    root = tree(tmp_path)
    command = [sys.executable, str(ROOT / "scripts/build_manifest.py")]
    result = subprocess.run([*command,
        "create", "--root", str(root), "--include", "app", "--include", "static",
        "--version", "0.4.2", "--output", str(root / "build-manifest.json"),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    args = ["verify", "--root", str(root), "--manifest", str(root / "build-manifest.json")]
    assert subprocess.run(command + args, capture_output=True).returncode == 0
    (root / "app/main.py").write_text("changed")
    assert subprocess.run(command + args, capture_output=True).returncode == 2


def test_release_seal_requires_matching_clean_source_and_checks_runtime(tmp_path):
    root = tree(tmp_path)
    payload = baseline(root)
    payload["source_dirty"] = False
    artifact = root / "TSP.tar.gz"
    artifact.write_bytes(b"installer")
    sealed = manifest.seal(root, payload, artifact, "a" * 40)
    assert sealed["artifact"]["name"] == artifact.name
    assert sealed["artifact"]["size"] == 9
    for commit, dirty in [("b" * 40, False), ("a" * 40, True), ("a" * 40, None)]:
        payload["source_dirty"] = dirty
        with pytest.raises(ValueError, match="source commit"):
            manifest.seal(root, payload, artifact, commit)
    payload["source_dirty"] = False
    (root / "app/main.py").write_text("post-build tamper")
    with pytest.raises(ValueError, match="runtime changed"):
        manifest.seal(root, payload, artifact, "a" * 40)


def test_backup_size_limit_and_output_symlink(tmp_path, monkeypatch):
    root = tree(tmp_path)
    before = baseline(root)
    (root / "app/main.py").write_text("12345")
    monkeypatch.setattr(manifest, "MAX_BACKUP_BYTES", 4)
    with pytest.raises(ValueError, match="exceeds"):
        manifest.backup(root, before, before, tmp_path / "limit")
    assert not (tmp_path / "limit").exists()
    monkeypatch.setattr(manifest, "MAX_BACKUP_BYTES", 100)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink output"):
        manifest.backup(root, before, before, tmp_path / "link/escape")


def test_build_workflows_bind_manifests_to_tested_revision_and_upload_sidecar():
    docker = yaml.safe_load((ROOT / ".github/workflows/docker.yml").read_text())
    steps = docker["jobs"]["build-and-push"]["steps"]
    build = next(step for step in steps if step.get("uses", "").startswith("docker/build-push-action"))
    assert "SOURCE_COMMIT=${{ steps.revision.outputs.revision }}" in build["with"]["build-args"]
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = release["jobs"]["build"]["steps"]
    seal_index = next(i for i, step in enumerate(steps) if "build_manifest.py seal" in step.get("run", ""))
    uploads = [
        (i, step) for i, step in enumerate(steps)
        if step.get("uses", "").startswith(("actions/upload-artifact", "softprops/action-gh-release"))
        and "matrix.artifact" in str(step)
    ]
    assert len(uploads) == 2
    for i, step in uploads:
        assert i > seal_index
        assert ".manifest.json" in str(step["with"])
    compile((ROOT / "packaging/tsp.spec").read_text(), "tsp.spec", "exec")
