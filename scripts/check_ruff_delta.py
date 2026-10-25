"""Reject added Ruff findings while allowing the unchanged historical baseline."""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def findings(root: Path, paths: list[str], ruff: list[str]) -> collections.Counter:
    if not paths:
        return collections.Counter()
    root = root.resolve()
    result = subprocess.run(
        [*ruff, "check", "--output-format=json", "--config", str(ROOT / "backend/pyproject.toml"),
         "--config", 'lint.isort.known-first-party=["app","scripts"]',
         *paths], cwd=root, capture_output=True, text=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr or "Ruff execution failed")
    items = json.loads(result.stdout)
    counter = collections.Counter()
    for item in items:
        path = Path(item["filename"]).relative_to(root)
        lines = (root / path).read_text(encoding="utf-8").splitlines()
        # Line content survives unrelated inserted lines; duplicate findings
        # still count separately. Changed problematic code becomes a new finding.
        row = item["location"]["row"]
        source = lines[row - 1].strip() if row <= len(lines) else ""
        counter[(str(path), item["code"], item["message"], source)] += 1
    return counter


def git(*args) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def check(base: str, head: str | None, ruff: list[str]) -> dict:
    changed = git("diff", "--name-only", "--diff-filter=ACMR", base, *([head] if head else [])).splitlines()
    if head is None:
        changed += git("ls-files", "--others", "--exclude-standard").splitlines()
    paths = sorted({path for path in changed if path.endswith(".py") and
                    (path.startswith("backend/") or path.startswith("scripts/") or path.startswith("tests/tooling/"))})
    with tempfile.TemporaryDirectory(prefix="tsp-ruff-base-") as base_dir:
        before = Path(base_dir)
        existing = []
        for path in paths:
            content = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, check=False)
            if content.returncode == 0:
                target = before / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.stdout)
                existing.append(path)
        baseline = findings(before, existing, ruff)
        if head:
            with tempfile.TemporaryDirectory(prefix="tsp-ruff-head-") as head_dir:
                after = Path(head_dir)
                for path in paths:
                    target = after / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(git("show", f"{head}:{path}"), encoding="utf-8")
                current = findings(after, paths, ruff)
        else:
            current = findings(ROOT, paths, ruff)
    added = current - baseline
    return {"base": base, "head": head or "working-tree", "files": len(paths),
            "baseline_findings": sum(baseline.values()), "current_findings": sum(current.values()),
            "added": [{"path": key[0], "code": key[1], "message": key[2],
                       "source": key[3], "count": count} for key, count in sorted(added.items())]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head")
    parser.add_argument("--ruff", default="ruff")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = check(args.base, args.head, [args.ruff])
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if report["added"] else 0


if __name__ == "__main__":
    sys.exit(main())
