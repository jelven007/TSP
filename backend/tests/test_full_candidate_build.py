"""A dirty checkout must retain exact source provenance without claiming a commit."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("build_full_candidate", SCRIPTS / "build_full_candidate.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_snapshot_records_dirty_base_and_does_not_follow_later_edits(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "VERSION").write_text("v0.4.8")
    (root / "README.md").write_text("full product")
    monkeypatch.setattr(builder.build_manifest, "SOURCE_SCOPES", ["VERSION"])
    monkeypatch.setattr(builder, "command",
                        lambda *args, **kwargs: "a" * 40 if args[1] == "rev-parse" else "M VERSION")
    output = tmp_path / "build"
    context, manifest = builder.snapshot(root, output)
    assert manifest["version"] == "0.4.8"
    assert manifest["base_commit"] == "a" * 40
    assert manifest["commit"] is None and manifest["source_dirty"] is True
    (root / "README.md").write_text("changed later")
    assert not any(builder.build_manifest.verify(context, manifest).values())
    assert builder.build_manifest.verify(root, manifest)["changed"] == ["README.md"]
    with pytest.raises(FileExistsError):
        builder.snapshot(root, output)


def test_snapshot_rejects_source_changed_while_copying(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "VERSION").write_text("0.4.8")
    (root / "README.md").write_text("before")
    monkeypatch.setattr(builder.build_manifest, "SOURCE_SCOPES", ["VERSION"])
    monkeypatch.setattr(builder, "command",
                        lambda *args, **kwargs: "a" * 40 if args[1] == "rev-parse" else "")
    copy = builder.shutil.copy2

    def racing_copy(source, target):
        copy(source, target)
        if source.name == "README.md":
            source.write_text("changed during copy")

    monkeypatch.setattr(builder.shutil, "copy2", racing_copy)
    with pytest.raises(ValueError, match="checkout changed"):
        builder.snapshot(root, tmp_path / "build")
    assert not (tmp_path / "build/source-manifest.json").exists()


@pytest.mark.parametrize("fault", ["tag", "parent", "files", None])
def test_derived_build_requires_original_parent_and_identical_files(tmp_path, monkeypatch, fault):
    source = {"files_sha256": "a" * 64, "commit": None, "base_commit": "b" * 40,
              "source_dirty": True, "version": "0.4.8"}
    monkeypatch.setattr(builder, "snapshot", lambda *_: (tmp_path, source))
    monkeypatch.setattr(builder.build_manifest, "verify", lambda *_: {})
    builds = []
    monkeypatch.setattr(builder.subprocess, "run", lambda args, **_: builds.append(args))
    runtime_reads = 0

    def command(*args):
        nonlocal runtime_reads
        if args[:3] == ("docker", "image", "inspect"):
            role = args[3].split(":")[0].removeprefix("tsp-full-")
            if role == "runtime":
                runtime_reads += 1
            identity = "replaced" if fault == "tag" and runtime_reads > 1 else role
            layers = ["foreign"] if fault == "parent" and role == "compute" else ["base"]
            return json.dumps([{
                "Id": identity, "Architecture": "arm64", "RootFS": {"Layers": layers},
                "Config": {"Labels": {"io.tsp.source-tree": source["files_sha256"]}},
            }])
        if "cat" in args:
            digest = "changed" if fault == "files" and "broker" in args else "same"
            return json.dumps({"files_sha256": digest})
        return ""

    monkeypatch.setattr(builder, "command", command)
    if fault:
        with pytest.raises(ValueError, match={"tag": "tag changed", "parent": "pinned runtime",
                                             "files": "source files differ"}[fault]):
            builder.build(tmp_path, tmp_path, "docker.io")
        assert not (tmp_path / "build.json").exists()
    else:
        assert builder.build(tmp_path, tmp_path, "docker.io")["status"] == "passed"
        assert len(builds) == 3
        assert any(arg.startswith("RUNTIME_IMAGE=tsp-full-runtime:tree-") for arg in builds[1])
