"""Promote verified image digests without replacing conflicting immutable tags."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.image_manifest import validate
from release.oci import MAX_JSON, Registry, hex_digest


def inspect(tag: str) -> str | None:
    image, reference = tag.rsplit(":", 1)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", reference):
        raise ValueError("invalid image tag")
    registry = Registry(image)
    try:
        with registry.request("/manifests/" + reference) as response:
            data = response.read(MAX_JSON + 1)
            declared = response.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError as exc:
        with exc:
            data = exc.read(MAX_JSON + 1)
        try:
            errors = json.loads(data).get("errors", []) if len(data) <= MAX_JSON else []
            missing = errors and all(row.get("code") == "MANIFEST_UNKNOWN" for row in errors)
        except (ValueError, AttributeError, TypeError):
            missing = False
        # Only the Registry's explicit missing-manifest response permits a write.
        if exc.code == 404 and missing:
            return None
        raise ValueError("registry lookup failed") from None
    except (OSError, urllib.error.URLError):
        raise ValueError("registry lookup failed") from None
    if len(data) > MAX_JSON:
        raise ValueError("registry manifest too large")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("schemaVersion") != 2:
        raise ValueError("invalid registry manifest")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if declared is not None and declared != digest:
        raise ValueError("registry manifest digest mismatch")
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
