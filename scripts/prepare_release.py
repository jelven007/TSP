#!/usr/bin/env python3
"""Resolve desktop releases to an existing immutable mainline version tag."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from advance_version import _git
from check_version import _check_source_versions
from tag_version import _remote_target
from versioning import format_version, parse_version

PLATFORMS = {
    "windows": {"os": "windows-latest", "artifact": "TSP-Setup-x64-{version}.exe"},
    "macos": {
        "os": "macos-26", "artifact": "TSP-macos-arm64-{version}.dmg", "arch": "arm64",
    },
    "linux": {"os": "ubuntu-latest", "artifact": "TSP-linux-x64-{version}.tar.gz"},
}


def is_current_main(root: Path, revision: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("expected a full build revision SHA")
    result = _git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    return result.stdout.split()[0] == revision


def prepare(root: Path, tag: str, platforms: str) -> dict[str, str]:
    requested = [value.strip() for value in platforms.split(",")]
    if (
        not requested or any(value not in PLATFORMS for value in requested)
        or len(set(requested)) != len(requested)
    ):
        raise ValueError("platform selection must contain unique windows,macos,linux values")
    version = format_version(parse_version(tag))
    if tag != f"v{version}":
        raise ValueError("release tag must use canonical va.b.cd format")
    revision = _remote_target(root, tag)
    if revision is None:
        raise ValueError(f"release tag {tag} does not exist; wait for successful main CI")
    _git(root, "fetch", "origin", f"refs/tags/{tag}")
    resolved = _git(root, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
    if resolved != revision:
        raise ValueError(f"release tag {tag} changed while resolving")
    _git(root, "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
    if _git(root, "merge-base", "--is-ancestor", revision, "origin/main", check=False).returncode:
        raise ValueError(f"release tag {tag} is not integrated into main")
    _git(root, "checkout", "--detach", revision)
    actual = (root / "VERSION").read_text().strip()
    if actual != tag:
        raise ValueError(f"release version mismatch: requested {tag}, source contains {actual}")
    _check_source_versions(root, version)
    selected = [
        {
            **PLATFORMS[platform], "platform": platform,
            "artifact": PLATFORMS[platform]["artifact"].format(version=version),
        }
        for platform in requested
    ]
    return {"revision": revision, "ver": version, "matrix": json.dumps({"include": selected})}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    outputs = prepare(root, os.environ["RELEASE_TAG"], os.environ["RELEASE_PLATFORMS"])
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
