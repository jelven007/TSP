"""Promote verified image digests without replacing conflicting immutable tags."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.image_manifest import validate
from release.oci import hex_digest


def inspect(tag: str) -> str | None:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", tag, "--format", "{{.Manifest.Digest}}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        # Authentication/network failures must not be mistaken for a missing tag.
        if "manifest unknown" in result.stderr.lower():
            return None
        raise ValueError("registry lookup failed")
    digest = result.stdout.strip()
    hex_digest(digest)
    return digest


def publish(tag: str, digest: str) -> None:
    image = tag.rsplit(":", 1)[0]
    subprocess.run(["docker", "buildx", "imagetools", "create", "--tag", tag,
                    image + "@" + digest], check=True, capture_output=True)


def apply(targets: list[tuple[str, str]], *, inspect=inspect, publish=publish) -> None:
    pending = []
    for tag, digest in targets:
        hex_digest(digest)
        existing = inspect(tag)
        if existing is not None and existing != digest:
            raise ValueError("immutable image tag already points to another digest")
        if existing is None:
            pending.append((tag, digest))
    for tag, digest in pending:
        # Recheck immediately before writing to narrow parallel retry races.
        existing = inspect(tag)
        if existing is not None:
            if existing != digest:
                raise ValueError("immutable image tag changed concurrently")
            continue
        publish(tag, digest)
        if inspect(tag) != digest:
            raise ValueError("promoted tag digest mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        if manifest.get("status") != "passed":
            raise ValueError("successful identity receipt required")
        validate(manifest["version"], manifest["revision"], manifest["images"])
        images = {row["image"]: row["index_digest"] for row in manifest["images"]}
        targets = [(image + ":" + tag, digest) for image, digest in images.items()
                   for tag in (manifest["version"], manifest["revision"])]
        apply(targets)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError):
        parser.exit(1, "immutable promotion stopped; reconcile registry state before retry\n")
    print("Verified immutable Runtime/Compute version and full SHA tags")


if __name__ == "__main__":
    main()
