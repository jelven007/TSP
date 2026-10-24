"""Create or verify six source archives: the tagged main revision plus five ancestors."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

VERSION = r"[0-9]\.[0-9]\.(?:0|[1-9][0-9]?)"
COUNT = 6


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def select_versions(root: Path, revision: str) -> list[dict]:
    head = git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    current = git(root, "show", f"{head}:VERSION")
    if not re.fullmatch(VERSION, current):
        raise ValueError("invalid current VERSION")
    if git(root, "rev-parse", "--verify", f"refs/tags/v{current}^{{commit}}") != head:
        raise ValueError("revision must match its immutable vVERSION tag")
    candidates = {}
    for ref in git(root, "tag", "--list").splitlines():
        match = re.fullmatch(rf"(?:recovery/)?v({VERSION})", ref)
        if not match:
            continue
        version = match[1]
        commit = git(root, "rev-parse", f"refs/tags/{ref}^{{commit}}")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, head], cwd=root,
            capture_output=True, check=False,
        )
        if ancestor.returncode == 1:
            continue
        if ancestor.returncode:
            raise ValueError("cannot verify version ancestry")
        if git(root, "show", f"{commit}:VERSION") != version:
            raise ValueError(f"{ref} disagrees with its VERSION")
        if version not in candidates or not ref.startswith("recovery/"):
            candidates[version] = {"version": version, "ref": ref, "commit": commit}
    versions = sorted(candidates, key=lambda value: tuple(map(int, value.split("."))), reverse=True)[:COUNT]
    if len(versions) != COUNT or versions[0] != current:
        raise ValueError("cannot establish current plus five prior versions")
    return [candidates[version] for version in versions]


def verify(output: Path) -> dict:
    if output.is_symlink() or (output / "index.json").is_symlink():
        raise ValueError("recovery set must not be a symlink")
    manifest = json.loads((output / "index.json").read_text())
    records = manifest["versions"]
    if manifest.get("format") != 1 or len(records) != COUNT:
        raise ValueError("invalid recovery manifest")
    seen = set()
    for record in records:
        version = record["version"]
        if not re.fullmatch(VERSION, version) or version in seen:
            raise ValueError("invalid or duplicate version")
        seen.add(version)
        expected = f"TSP-v{version}.tar.gz"
        if (record["archive"] != expected or record["ref"] not in (f"v{version}", f"recovery/v{version}")
                or not re.fullmatch(r"[0-9a-f]{40}", record["commit"])):
            raise ValueError("invalid source identity or archive path")
        archive = output / expected
        if archive.is_symlink() or hashlib.sha256(archive.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"archive checksum mismatch: {expected}")
        with tarfile.open(archive, "r:gz") as source:
            # Read without extracting any archive paths onto the filesystem.
            member = source.getmember(f"TSP-v{version}/VERSION")
            if not member.isfile():
                raise ValueError("archive VERSION must be a regular file")
            version_file = source.extractfile(member)
            if version_file is None or version_file.read().decode().strip() != version:
                raise ValueError(f"archive VERSION mismatch: {expected}")
    ordered = sorted(seen, key=lambda value: tuple(map(int, value.split("."))), reverse=True)
    if [r["version"] for r in records] != ordered or manifest["latest"] != ordered[0]:
        raise ValueError("invalid version order")
    actual = {path.name for path in output.glob("TSP-v*.tar.gz")}
    if actual != {r["archive"] for r in records}:
        raise ValueError("unexpected source archives in recovery set")
    return manifest


def create(root: Path, revision: str, output: Path) -> dict:
    # Never overwrite an existing recovery set, source tree or user data.
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    if output.parent.absolute() != output.parent.resolve():
        raise ValueError("output parent must not contain symlinks")
    records = select_versions(root, revision)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-recovery-", dir=output.parent) as directory:
        staging = Path(directory)
        for record in records:
            version = record["version"]
            archive = staging / f"TSP-v{version}.tar.gz"
            subprocess.run(
                ["git", "archive", "--format=tar.gz", f"--prefix=TSP-v{version}/",
                 f"--output={archive}", record["commit"]], cwd=root, check=True,
            )
            record.update(archive=archive.name, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        manifest = {
            "format": 1, "policy": "latest_source_plus_five_prior_versions",
            "latest": records[0]["version"], "versions": records,
            "git_history_and_tags_unchanged": True, "production_deployed": False,
        }
        (staging / "index.json").write_text(json.dumps(manifest, indent=2) + "\n")
        rows = [
            "# 源码恢复集合", "",
            "最新源码及五个历史版本。归档不包含用户数据、运行配置或依赖安装包。",
            "Git 历史和标签保持不变。本记录不表示生产部署。", "",
            "| 版本 | 源码引用 | 提交 | SHA-256 |", "| --- | --- | --- | --- |",
        ]
        rows += [
            f"| [{r['version']}]({r['archive']}) | `{r['ref']}` | `{r['commit']}` | `{r['sha256']}` |"
            for r in records
        ]
        (staging / "index.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
        verify(staging)
        # Recheck after archive creation; no writes to Git refs or prior bundles.
        if output.exists() or output.is_symlink():
            raise ValueError("output appeared while archiving")
        staging.rename(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--revision", default="origin/main")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = (create(args.root.resolve(), args.revision, args.output.absolute())
                    if args.action == "create" else verify(args.output.absolute()))
    except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"source recovery failed: {exc}\n")
    print(f"Verified {len(manifest['versions'])} source versions; latest {manifest['latest']}")


if __name__ == "__main__":
    main()
