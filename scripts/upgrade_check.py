#!/usr/bin/env python3
"""Read-only Git overlap and conflict preview for secondary-development branches."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import build_manifest


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "--no-optional-locks", "--literal-pathspecs", "-c", "core.quotePath=false", *args],
        cwd=root,
    )


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "--no-optional-locks", "--literal-pathspecs", "-c", "core.quotePath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result


def _lines(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _resolve_repo(path: Path) -> Path:
    result = _git(path, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def _resolve_commit(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").stdout.strip()


def _merge_tree_conflicts(root: Path, base: str, current: str, target: str) -> list[str]:
    result = _git(root, "merge-tree", base, current, target)
    lines = result.stdout.splitlines()
    conflicts: list[str] = []
    current_path: str | None = None
    current_conflicted = False
    for line in [*lines, ""]:
        if line in {"changed in both", "added in both", "removed in both"}:
            if current_path and current_conflicted:
                conflicts.append(current_path)
            current_path = None
            current_conflicted = False
            continue
        match = re.match(r"\s*(?:base|our|their)\s+\d{6}\s+[0-9a-f]+\s+(.+)$", line)
        if current_path is None and match:
            current_path = match[1]
            if current_path.startswith('"'):
                current_path = json.loads(current_path)
        if line.startswith("+<<<<<<< ") or line.startswith("+=======") or line.startswith("+>>>>>>> "):
            current_conflicted = True
    if current_path and current_conflicted:
        conflicts.append(current_path)
    return sorted(set(conflicts))


def _print_paths(title: str, paths: list[str]) -> None:
    print(f"\n{title}: {len(paths)}")
    for path in paths:
        print(f"  - {path}")


def _paths(root: Path, *args: str) -> set[str]:
    return {p.decode("utf-8") for p in _git_bytes(root, *args).split(b"\0") if p}


def _source_path(path: str) -> bool:
    build_manifest._relative(path)
    if build_manifest._excluded(path):
        return False
    parts = Path(path).parts
    if any(part in {"dist", "build", "Output", "user_prompts", ".tools"} for part in parts):
        return False
    if path.endswith((".parquet", ".db", ".sqlite", ".sqlite3", ".pem", ".key")):
        return False
    return parts[0] in {
        "backend", "frontend", "scripts", "docs", "packaging", "deploy", ".github", "mcp-server",
        "brand", "README.md", "AGENTS.md", "CONTRIBUTING.md", "VERSION", "tiers.yaml",
        "Dockerfile", ".dockerignore", ".gitignore", "操作说明书.md",
    }


def _tree(root: Path, commit: str) -> dict:
    entries = {}
    for entry in _git_bytes(root, "ls-tree", "-rz", commit).split(b"\0"):
        if entry:
            header, path = entry.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            entries[path.decode()] = (mode, kind, oid)
    return entries


def _write_bundle(root: Path, output: Path, report: dict, paths: set[str]) -> None:
    selected = sorted(p for p in paths if _source_path(p))
    if len(selected) > 5000:
        raise ValueError("too many source paths for one upgrade bundle")
    if output.absolute().is_relative_to(root) and not output.absolute().is_relative_to(root / ".dbg"):
        raise ValueError("use an external directory or .dbg/ for the upgrade bundle")
    before_status = _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    worktree = build_manifest._scan(root, selected, missing_ok=True)
    if worktree.keys() - set(selected):
        raise ValueError("source file replaced by a directory; resolve before backing up")
    trees = {name: _tree(root, report[name]) for name in ("base", "current", "target")}
    index = _index_entries(root)
    trees["index"] = index
    for entries in trees.values():
        for path in selected:
            if path in entries and entries[path][0] not in {"100644", "100755"}:
                raise ValueError(f"unsupported symlink/submodule in source bundle: {path}")
    if any(entry["kind"] != "file" for entry in worktree.values()):
        raise ValueError("source bundle does not follow symlinks")
    build_manifest.new_output(output)
    content_index = {}
    total = 0
    with zipfile.ZipFile(output / "sources.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, entries in {**trees, "working": worktree}.items():
            for path in selected:
                if path not in entries:
                    continue
                if name == "working":
                    size = worktree[path]["size"]
                    mode = "100755" if (root / path).stat().st_mode & 0o111 else "100644"
                else:
                    mode, _, oid = entries[path]
                    size = int(_git_bytes(root, "cat-file", "-s", oid))
                total += size
                if total > build_manifest.MAX_BACKUP_BYTES:
                    raise ValueError("source bundle exceeds 256 MiB")
                if name == "working":
                    data = build_manifest._read_expected(root, path, worktree[path])
                else:
                    data = _git_bytes(root, "cat-file", "blob", oid)
                key = f"{name}/{path}"
                info = zipfile.ZipInfo(key)
                info.external_attr = int(mode, 8) << 16
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                content_index[key] = {"sha256": build_manifest._hash(data), "mode": mode}
    for name, refs in {
        "committed": [report["base"], report["current"]],
        "staged": ["--cached", report["current"]],
        "unstaged": [],
    }.items():
        with (output / f"{name}.patch").open("xb") as stream:
            for offset in range(0, len(selected), 200):
                stream.write(_git_bytes(
                    root, "diff", "--binary", "--no-ext-diff", "--no-textconv",
                    *refs, "--", *selected[offset:offset + 200],
                ))
    if (
        before_status != _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        or worktree != build_manifest._scan(root, selected, missing_ok=True)
        or report["current"] != _resolve_commit(root, "HEAD")
        or index != _index_entries(root)
    ):
        raise ValueError("checkout changed while backing up; incomplete bundle")
    report.update({
        "format": 1, "complete": True, "created_at": datetime.now(timezone.utc).isoformat(),
        "excluded_paths": sorted(paths - set(selected)), "entries": content_index,
        "artifacts": {
            name: build_manifest._entry(output.resolve(), name)
            for name in ("sources.zip", "committed.patch", "staged.patch", "unstaged.patch")
        },
        "preview_includes_uncommitted": False, "restores_code": False,
    })
    (output / "report.json").write_bytes(build_manifest._json(report))


def _index_entries(root: Path) -> dict:
    entries = {}
    for item in _git_bytes(root, "ls-files", "--stage", "-z").split(b"\0"):
        if item:
            header, path = item.split(b"\t", 1)
            mode, oid, stage = header.decode().split()
            if stage != "0":
                raise ValueError("unresolved index")
            entries[path.decode()] = (mode, "blob", oid)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读检查当前二开分支升级到目标 Git ref 时的源码重叠与文本冲突",
    )
    parser.add_argument("target", help="目标 Tag/分支/commit, 例如 v0.3.1")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="仓库路径, 默认当前目录",
    )
    parser.add_argument(
        "--bundle", type=Path,
        help="可选: 写入新的备份目录(仓库外或 .dbg/), 保存差异及三方材料; 不改源码",
    )
    args = parser.parse_args(argv)

    try:
        root = _resolve_repo(args.repo.resolve())
        current = _resolve_commit(root, "HEAD")
        target = _resolve_commit(root, args.target)
        base = _git(root, "merge-base", current, target).stdout.strip()
        if not base:
            raise RuntimeError("当前分支与目标版本没有共同基线")

        current_paths = _paths(root, "diff", "--name-only", "-z", f"{base}..{current}")
        target_paths = _paths(root, "diff", "--name-only", "-z", f"{base}..{target}")
        overlap = sorted(current_paths & target_paths)
        conflicts = _merge_tree_conflicts(root, base, current, target)
        dirty = sorted(_lines(_git(root, "status", "--short")))

        print("TSP 二开升级预检")
        print(f"仓库: {root}")
        print(f"当前: {current[:12]}")
        print(f"目标: {args.target} ({target[:12]})")
        print(f"共同基线: {base[:12]}")
        print(f"当前分支修改文件: {len(current_paths)}")
        print(f"目标版本修改文件: {len(target_paths)}")
        _print_paths("双方修改的同一文件", overlap)
        _print_paths("Git 三方预演识别的文本冲突", conflicts)

        if args.bundle:
            work_paths = _paths(root, "diff", "--name-only", "-z", "HEAD")
            untracked = _paths(root, "ls-files", "--others", "--exclude-standard", "-z")
            _write_bundle(root, args.bundle, {
                "base": base, "current": current, "target": target,
                "overlap": overlap, "conflicts": conflicts,
            }, current_paths | target_paths | work_paths | untracked)
            print(f"\n差异备份与三方材料: {args.bundle}")

        if dirty:
            print(f"\n未提交工作区条目: {len(dirty)}")
            print("  注意: 未提交内容不会进入 Git 三方预演, 请先提交到临时分支后再复检。")
        if conflicts:
            print("\n结论: 需要人工解决文本冲突并执行完整回归。")
            return 2
        if overlap:
            print("\n结论: Git 未发现文本冲突, 但重叠文件需要检查语义兼容和测试。")
            return 0
        print("\n结论: 未发现源码重叠; 仍需执行目标版本的验证矩阵。")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"升级预检失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
