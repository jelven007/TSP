#!/usr/bin/env python3
"""Publish an immutable version tag for the main commit tested by CI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess

from advance_version import _git
from check_version import _check_source_versions
from versioning import format_version, parse_version


def _remote_target(root: Path, tag: str) -> str | None:
    ref = f"refs/tags/{tag}"
    result = _git(root, "ls-remote", "--tags", "origin", ref, f"{ref}^{{}}")
    refs = dict(line.split()[::-1] for line in result.stdout.splitlines())
    # Annotated tags resolve to their commit, while lightweight tags point directly.
    return refs.get(f"{ref}^{{}}", refs.get(ref))


def tag_main(root: Path, revision: str) -> str:
    """Tag only the checked-out, tested revision; never overwrite an existing tag."""
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "push"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("tag publication requires a GitHub Actions push to main")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("expected a full tested commit SHA")
    if _git(root, "status", "--porcelain").stdout.strip():
        raise ValueError("tag publication requires a clean checkout")
    if _git(root, "rev-parse", "HEAD").stdout.strip() != revision:
        raise ValueError("checkout does not match the tested revision")

    value = (root / "VERSION").read_text(encoding="utf-8").strip()
    version = format_version(parse_version(value))
    if value.removeprefix("v") != version:
        raise ValueError(f"expected canonical application version, got {value!r}")
    try:
        _check_source_versions(root, version)
    except SystemExit as error:
        raise ValueError(str(error)) from error
    tag = f"v{version}"

    _git(root, "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
    integrated = _git(
        root, "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main",
        check=False,
    )
    if integrated.returncode:
        raise ValueError(f"{revision} is not integrated into origin/main")

    existing = _remote_target(root, tag)
    if existing == revision:
        return tag
    if existing is not None:
        raise ValueError(f"{tag} already points to {existing}; refusing to move it")

    # Push the explicit SHA instead of HEAD/latest main to handle overlapping runs.
    pushed = _git(root, "push", "origin", f"{revision}:refs/tags/{tag}", check=False)
    existing = _remote_target(root, tag)
    if existing == revision:
        return tag
    if existing is not None:
        raise ValueError(f"{tag} already points to {existing}; refusing to move it")
    raise RuntimeError(
        f"cannot publish {tag}; check Actions write permission and tag rules:\n"
        f"{pushed.stderr.strip()}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision", help="exact commit checked by the quality jobs")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    tag = tag_main(root, args.revision)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"tag={tag}\nrevision={args.revision}\n")
    print(f"main version tag: {tag} ({args.revision})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
