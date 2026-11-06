"""Recovery availability and capacity are checked before any switching."""
import json

import pytest
from release import preflight


@pytest.fixture
def recovery(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("hotfix")
    config = tmp_path / "runtime.env"
    config.write_text("SMTP_PASSWORD=private-value")
    manifest = {
        "format": 1,
        "capacity": [{"path": str(tmp_path), "required_bytes": 1, "reserve_bytes": 1}],
        "units": [
            {"version": version, "revision": revision * 40,
             "source": {"root": str(tmp_path), "files": {"source.py": preflight.file_hash(source)}},
             "config": {str(config): preflight.file_hash(config)},
             "images": [
                 {"role": role, "platform": platform, "layout": str(tmp_path),
                  "digest": "sha256:" + "a" * 64}
                 for role in ("runtime", "compute")
                 for platform in ("linux/amd64", "linux/arm64")
             ]}
            for version, revision in (("0.4.50", "a"), ("0.4.49", "b"))
        ],
    }
    def verify_image(layout, digest, platform):
        # Each fixture has a separate layout path, just as the actual eight OCI units do.
        unit, role = str(layout).split("/")[-2:]
        row = manifest["units"][int(unit)]
        return {"status": "passed", "revision": row["revision"], "version": row["version"],
                "role": role, "platform": platform, "index_digest": digest,
                "source_tree": preflight.object_hash(row["source"]["files"])}

    for i, unit in enumerate(manifest["units"]):
        for image in unit["images"]:
            image["layout"] = str(tmp_path / str(i) / image["role"])
    return manifest, verify_image, source, config


def test_private_files_capacity_and_both_recovery_roles(recovery):
    manifest, verify_image, source, _ = recovery
    result = preflight.verify(manifest, image_verifier=verify_image)
    assert result["status"] == "passed" and sum(row["images"] for row in result["units"]) == 8
    assert "private-value" not in json.dumps(result)
    source.write_text("new hotfix")
    with pytest.raises(ValueError, match="source"):
        preflight.verify(manifest, image_verifier=verify_image)


@pytest.mark.parametrize("fault", ["role", "capacity", "config", "identity", "version", "tree"])
def test_missing_recovery_requirements_fail_closed(recovery, fault):
    manifest, verify_image, _, config = recovery
    if fault == "capacity":
        manifest["capacity"][0]["required_bytes"] = 10**30
    elif fault == "role":
        manifest["units"][0]["images"].pop()
    elif fault == "config":
        config.write_text("changed")
    else:
        original = verify_image
        def verify_image(*args):
            row = original(*args)
            row[{"identity": "revision", "version": "version", "tree": "source_tree"}[fault]] = "wrong"
            return row
    with pytest.raises(ValueError):
        preflight.verify(manifest, image_verifier=verify_image)


def test_capacity_budgets_on_same_filesystem_are_combined(recovery, monkeypatch):
    from types import SimpleNamespace
    manifest, verify_image, _, _ = recovery
    manifest["capacity"] *= 2
    for row in manifest["capacity"]:
        row["required_bytes"] = 60
        row["reserve_bytes"] = 10
    monkeypatch.setattr(preflight.shutil, "disk_usage", lambda _: SimpleNamespace(free=100))
    with pytest.raises(ValueError, match="capacity"):
        preflight.verify(manifest, image_verifier=verify_image)
