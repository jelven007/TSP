"""Verify two complete recovery units and disk budgets before release switching."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.common import atomic_json, file_hash, identity, object_hash


def verify(manifest: dict, *, image_verifier=None) -> dict:
    if manifest.get("format") != 1 or len(manifest.get("units", [])) != 2:
        raise ValueError("current and previous recovery units required")
    if not manifest.get("capacity"):
        raise ValueError("capacity budgets required")
    capacity = []
    # Budgets sharing a filesystem must be summed rather than each spending the same free bytes.
    filesystems = {}
    for budget in manifest["capacity"]:
        path = Path(budget["path"])
        required = budget["required_bytes"]
        reserve = budget.get("reserve_bytes", 0)
        if (not path.is_absolute() or not path.exists()
                or type(required) is not int or type(reserve) is not int or min(required, reserve) < 0):
            raise ValueError("invalid capacity budget")
        device = path.stat().st_dev
        total, floor = filesystems.get(device, (0, 0))
        filesystems[device] = (total + required, max(floor, reserve))
        capacity.append({"path_sha256": object_hash(str(path)), "required_bytes": required,
                         "free_bytes": shutil.disk_usage(path).free, "device": device})
    for row in capacity:
        total, floor = filesystems[row["device"]]
        if row["free_bytes"] < total + floor:
            raise ValueError("insufficient capacity")
    if image_verifier is None:
        from release.oci import verify_layout
        image_verifier = verify_layout
    units = []
    versions = set()
    for unit in manifest["units"]:
        identity(unit["version"], unit["revision"])
        if unit["version"] in versions:
            raise ValueError("duplicate recovery version")
        versions.add(unit["version"])
        source = unit["source"]
        root = Path(source["root"])
        if not root.is_absolute() or root != root.resolve() or not source.get("files"):
            raise ValueError("actual source inventory required")
        for raw, digest in source["files"].items():
            path = PurePosixPath(raw)
            if path.is_absolute() or ".." in path.parts or (root / raw).resolve().is_relative_to(root.resolve()) is False:
                raise ValueError("unsafe source path")
            if file_hash(root / raw) != digest:
                raise ValueError("source hash mismatch")
        if not unit.get("config"):
            raise ValueError("controlled config fingerprints required")
        for raw, digest in unit["config"].items():
            path = Path(raw)
            if not path.is_absolute() or file_hash(path) != digest:
                raise ValueError("config fingerprint mismatch")
        pairs = set()
        for image in unit["images"]:
            pair = (image["role"], image["platform"])
            if pair in pairs:
                raise ValueError("duplicate recovery image")
            pairs.add(pair)
            result = image_verifier(Path(image["layout"]), image["digest"], image["platform"])
            if any(result.get(field) != expected for field, expected in {
                "status": "passed", "revision": unit["revision"], "version": unit["version"],
                "role": image["role"], "platform": image["platform"], "index_digest": image["digest"],
                "source_tree": object_hash(source["files"]),
            }.items()):
                raise ValueError("recovery image/source identity mismatch")
        if pairs != {(role, platform) for role in ("runtime", "compute")
                     for platform in ("linux/amd64", "linux/arm64")}:
            raise ValueError("both roles and platforms required")
        units.append({"version": unit["version"], "revision": unit["revision"],
                      "source_sha256": object_hash(source["files"]),
                      "config_sha256": object_hash(unit["config"]), "images": len(pairs)})
    if tuple(map(int, units[0]["version"].split("."))) <= tuple(map(int, units[1]["version"].split("."))):
        raise ValueError("current recovery version must follow previous")
    return {"status": "passed", "units": units, "capacity": capacity,
            "input_sha256": object_hash(manifest), "production_deployed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(json.loads(args.manifest.read_text()))
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(1, "release preflight failed: verify private source/config/image/capacity inputs\n")
    atomic_json(args.output, result)
    print("Verified current and previous recovery units and capacity; no services changed")


if __name__ == "__main__":
    main()
