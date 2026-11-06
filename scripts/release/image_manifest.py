"""Record the four release image identities directly from registry manifests."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.common import atomic_json, identity
from release.oci import Registry, hex_digest, select


def validate(version: str, revision: str, records: list[dict]) -> dict:
    identity(version, revision)
    pairs, trees, roles = set(), set(), {}
    for row in records:
        if row["revision"] != revision or row["version"] != version:
            raise ValueError("image version/revision mismatch")
        pair = row["role"], row["platform"]
        if pair in pairs:
            raise ValueError("duplicate image matrix entry")
        pairs.add(pair)
        for field in ("index_digest", "manifest_digest", "config_digest"):
            hex_digest(row[field])
        if not isinstance(row.get("layers"), list):
            raise ValueError("image layer byte identities required")
        for layer in row["layers"]:
            hex_digest(layer["digest"])
            if type(layer["size"]) is not int or layer["size"] < 0:
                raise ValueError("invalid layer size")
        tree = row.get("source_tree")
        if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{64}", tree):
            raise ValueError("image source tree identity required")
        trees.add(tree)
        if "image" in row:
            Registry(row["image"])
            current = row["image"], row["index_digest"]
            if roles.setdefault(row["role"], current) != current:
                raise ValueError("role repository/index mismatch")
    if pairs != {(role, platform) for role in ("runtime", "compute")
                 for platform in ("linux/amd64", "linux/arm64")}:
        raise ValueError("four role/platform image matrix required")
    if len(trees) != 1 or len(roles) != 2 or roles["runtime"][0] == roles["compute"][0]:
        raise ValueError("incoherent source tree or role repositories")
    return {"format": 1, "status": "passed", "version": version, "revision": revision,
            "images": records, "production_deployed": False, "docker_ids": "collect after engine import"}


def collect(image: str, digest: str, role: str) -> list[dict]:
    registry = Registry(image)

    def read(reference, size=None):
        data = registry.manifest(reference)
        if hashlib.sha256(data).hexdigest() != hex_digest(reference) or (
            size is not None and len(data) != size
        ):
            raise ValueError("registry manifest checksum/size mismatch")
        return json.loads(data)

    index = read(digest)
    records = []
    for platform in ("linux/amd64", "linux/arm64"):
        descriptor = select(index, platform)
        manifest = read(descriptor["digest"], descriptor["size"])
        stream, _, _ = registry.blob(manifest["config"]["digest"])
        with stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        if (len(data) != manifest["config"]["size"]
                or hashlib.sha256(data).hexdigest() != hex_digest(manifest["config"]["digest"])):
            raise ValueError("config checksum/size mismatch")
        config = json.loads(data)
        labels = config.get("config", {}).get("Labels", {})
        if labels.get("io.tsp.role") != role or f"{config['os']}/{config['architecture']}" != platform:
            raise ValueError("image role/platform mismatch")
        records.append({"role": role, "platform": platform, "image": image,
                        "revision": labels.get("org.opencontainers.image.revision"),
                        "version": labels.get("org.opencontainers.image.version"),
                        "source_tree": labels.get("io.tsp.source-tree"),
                        "index_digest": digest, "manifest_digest": descriptor["digest"],
                        "config_digest": manifest["config"]["digest"], "layers": manifest["layers"]})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("version", "revision", "runtime-image", "runtime-digest", "compute-image", "compute-digest"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        records = collect(args.runtime_image, args.runtime_digest, "runtime")
        records += collect(args.compute_image, args.compute_digest, "compute")
        atomic_json(args.output, validate(args.version, args.revision, records))
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(1, "image identity matrix verification failed\n")
    print("Verified Runtime/Compute for amd64/arm64 identities")


if __name__ == "__main__":
    main()
