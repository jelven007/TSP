#!/usr/bin/env python3
"""Advance the application version after a GitHub push integrates into main."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from check_version import _check_source_versions
from versioning import next_version, parse_version

# Only current application metadata/docs belong here. Dependency versions,
# independent packages, examples and historical reports must remain untouched.
REPLACEMENTS = {
    "frontend/package.json": (r'(?m)^(\s*"version": ")[^"]+(")',),
    "backend/pyproject.toml": (r'(?m)^(version = ")[^"]+(")',),
    "backend/uv.lock": (
        r'(\[\[package\]\]\nname = "tsp-backend"\nversion = ")[^"]+(")',
    ),
    "backend/app/__init__.py": (r'(_FALLBACK_VERSION = ")[^"]+(")',),
    "README.md": (r"(应用语义版本为 `)[0-9.]+(`)",),
    "docs/README.md": (r"(应用语义版本为 `)[0-9.]+(`)",),
    "docs/version-baseline.md": (r"(\| 应用语义版本 \| `)[0-9.]+(` \|)",),
    "docs/requirements.md": (
        r"(?m)^(# TSP v)[0-9.]+( 需求基线)",
        r"(本文档定义 TSP `)[0-9.]+(` 的产品目标)",
    ),
    "操作说明书.md": (r"(适用版本：)[0-9.]+(（)",),
}


def update_versions(root: Path) -> str:
    """Validate every input before writing the exact next application version."""
    previous = (root / "VERSION").read_text(encoding="utf-8").strip()
    parse_version(previous)
    version = next_version(previous)
    updates = {"VERSION": f"v{version}\n"}
    for name, patterns in REPLACEMENTS.items():
        content = (root / name).read_text(encoding="utf-8")
        for pattern in patterns:
            content, count = re.subn(
                pattern, lambda match: f"{match[1]}{version}{match[2]}", content
            )
            if count != 1:
                raise ValueError(f"{name}: expected one version field, found {count}")
        updates[name] = content
    try:
        _check_source_versions(root, previous.removeprefix("v"))
    except SystemExit as error:
        raise ValueError(str(error)) from error

    for name, content in updates.items():
        (root / name).write_text(content, encoding="utf-8")
    return version


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _validate_version_commit(root: Path, revision: str) -> str:
    """Recognize a version commit by its exact changes, not a copied trailer."""
    message = _git(root, "show", "-s", "--format=%B", revision).stdout
    sources = re.findall(r"(?m)^TSP-Version-Source: ([0-9a-f]{40})$", message)
    parents = _git(root, "show", "-s", "--format=%P", revision).stdout.split()
    if len(sources) != 1 or len(parents) != 1:
        raise ValueError(f"invalid automatic version commit: {revision}")
    parent = parents[0]
    previous = _git(root, "show", f"{parent}:VERSION").stdout.strip()
    version = next_version(previous)
    changes = _git(root, "diff", "--name-only", "-z", parent, revision).stdout.rstrip("\0").split("\0")
    if set(changes) != {"VERSION", *REPLACEMENTS}:
        raise ValueError(f"invalid automatic version commit: {revision}")
    if _git(root, "show", f"{revision}:VERSION").stdout != f"v{version}\n":
        raise ValueError(f"invalid automatic version commit: {revision}")
    for name, patterns in REPLACEMENTS.items():
        content = _git(root, "show", f"{parent}:{name}").stdout
        for pattern in patterns:
            content, count = re.subn(
                pattern, lambda match: f"{match[1]}{version}{match[2]}", content,
            )
            if count != 1:
                raise ValueError(f"invalid automatic version commit: {revision}")
        if content != _git(root, "show", f"{revision}:{name}").stdout:
            raise ValueError(f"invalid automatic version commit: {revision}")
    if _git(root, "merge-base", "--is-ancestor", sources[0], parent, check=False).returncode:
        raise ValueError(f"invalid automatic version commit: {revision}")
    return sources[0]


def advance_main(root: Path, source: str, *, attempts: int = 5) -> str:
    """Publish one increment per source commit, retrying only concurrent pushes."""
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "push"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("version advancement requires a GitHub Actions push to main")
    if re.fullmatch(r"[0-9a-f]{40}", source) is None:
        raise ValueError("expected a full source commit SHA")
    if _git(root, "status", "--porcelain").stdout.strip():
        raise ValueError("version advancement requires a clean checkout")

    for _ in range(attempts):
        _git(root, "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
        base = _git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        integrated = _git(root, "merge-base", "--is-ancestor", source, base, check=False)
        if integrated.returncode != 0:
            raise ValueError(f"{source} is not integrated into origin/main")

        message = _git(root, "show", "-s", "--format=%B", source).stdout
        if re.search(r"(?m)^TSP-Version-Source:", message):
            _validate_version_commit(root, source)
            return source

        # The trailer survives a workflow rerun and also deduplicates two workers
        # handling the same push. Search only the mainline's own commits.
        existing = _git(
            root, "log", "--first-parent", "-1", "--format=%H",
            f"--grep=^TSP-Version-Source: {source}$", base,
        ).stdout.strip()
        if existing:
            _validate_version_commit(root, existing)
            return existing

        last_version = _git(
            root, "log", "--first-parent", "-1", "--format=%H",
            "--grep=^TSP-Version-Source:", base,
        ).stdout.strip()
        if last_version:
            _validate_version_commit(root, last_version)
            expected = _git(root, "show", f"{last_version}:VERSION").stdout.strip()
            actual = _git(root, "show", f"{base}:VERSION").stdout.strip()
            if actual != expected:
                raise ValueError(
                    f"main must retain the last allocated version {expected}, got {actual}; "
                    "restore version metadata through a PR before rerunning CI"
                )

        _git(root, "checkout", "--detach", base)
        version = update_versions(root)
        _git(root, "add", "--", "VERSION", *REPLACEMENTS)
        _git(
            root,
            "-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "-c", "commit.gpgsign=false",
            "commit", "-m", f"chore: advance application version to {version}",
            "-m", f"TSP-Version-Source: {source}",
        )
        revision = _git(root, "rev-parse", "HEAD").stdout.strip()
        pushed = _git(root, "push", "origin", "HEAD:refs/heads/main", check=False)
        if pushed.returncode == 0:
            return revision

        _git(root, "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
        latest = _git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        if latest == base:
            raise RuntimeError(
                "cannot push automatic version commit; check Actions write "
                f"permission and main protection rules:\n{pushed.stderr.strip()}"
            )
        # Rebuild from the new remote head instead of force-pushing or rebasing
        # version files that may already have been advanced by another worker.
    raise RuntimeError("origin/main kept changing; rerun the failed CI job")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    revision = advance_main(root, os.environ.get("GITHUB_SHA", ""))
    version = _git(root, "show", f"{revision}:VERSION").stdout.strip()
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"revision={revision}\n")
    print(f"main application version: {version} ({revision})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
