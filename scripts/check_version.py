#!/usr/bin/env python3
"""Validate the repository application version and optional mainline progression."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from versioning import format_version, parse_version, validate_progression

DOCUMENT_VERSION_PATTERNS = {
    "README.md": (r"应用语义版本为 `([0-9.]+)`",),
    "docs/README.md": (r"应用语义版本为 `([0-9.]+)`",),
    "docs/version-baseline.md": (r"\| 应用语义版本 \| `([0-9.]+)` \|",),
    "docs/requirements.md": (
        r"(?m)^# TSP v([0-9.]+) 需求基线",
        r"本文档定义 TSP `([0-9.]+)` 的产品目标",
    ),
    "操作说明书.md": (r"适用版本：([0-9.]+)（",),
}


def _check_document_versions(root: Path, expected: str) -> None:
    for name, patterns in DOCUMENT_VERSION_PATTERNS.items():
        path = root / name
        if not path.is_file():
            raise SystemExit(f"missing current version document: {name}")
        content = path.read_text(encoding="utf-8")
        for pattern in patterns:
            values = re.findall(pattern, content)
            if values != [expected]:
                raise SystemExit(
                    f"document version mismatch: {name}, expected {expected}, got {values}"
                )


def _check_source_versions(root: Path, expected: str) -> None:
    sources = {
        "frontend/package.json": json.loads(
            (root / "frontend/package.json").read_text(encoding="utf-8")
        )["version"],
        "backend/pyproject.toml": re.search(
            r'^version = "([^"]+)"',
            (root / "backend/pyproject.toml").read_text(encoding="utf-8"),
            re.MULTILINE,
        ).group(1),
        "backend/uv.lock": re.search(
            r'\[\[package\]\]\nname = "tsp-backend"\nversion = "([^"]+)"',
            (root / "backend/uv.lock").read_text(encoding="utf-8"),
        ).group(1),
        "backend/app/__init__.py": re.search(
            r'_FALLBACK_VERSION = "([^"]+)"',
            (root / "backend/app/__init__.py").read_text(encoding="utf-8"),
        ).group(1),
    }
    mismatches = {
        path: value for path, value in sources.items() if value != expected
    }
    if mismatches:
        details = ", ".join(f"{path}={value}" for path, value in mismatches.items())
        raise SystemExit(f"version source mismatch: expected {expected}, {details}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", default="VERSION", help="current VERSION file")
    parser.add_argument(
        "--previous",
        help="previous mainline VERSION value or file; requires its exact successor",
    )
    args = parser.parse_args()

    current_path = Path(args.current)
    current = current_path.read_text(encoding="utf-8").strip()
    current_canonical = format_version(parse_version(current))
    if current.removeprefix("v") != current_canonical:
        raise SystemExit(
            f"{current_path}: use canonical version {current_canonical!r}, "
            f"got {current!r}"
        )
    _check_source_versions(current_path.resolve().parent, current_canonical)
    _check_document_versions(current_path.resolve().parent, current_canonical)

    if args.previous is None:
        print(f"valid application version: {current_canonical}")
        return 0

    previous_path = Path(args.previous)
    previous = (
        previous_path.read_text(encoding="utf-8").strip()
        if previous_path.is_file()
        else args.previous
    )
    previous_canonical = format_version(parse_version(previous))
    validate_progression(previous_canonical, current_canonical)
    print(f"version progression: {previous_canonical} -> {current_canonical}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(str(error)) from error
