"""Bind local/CI validation evidence to the exact source and artifact bytes.

This record collects evidence; it never infers deployment or production approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def digest_files(paths, root):
    values = {}
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError("symbolic links cannot certify artifact bytes")
        if path.is_file():
            values[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"sha256": digest, "files": len(values)}


def collect(evidence, artifacts):
    names = set(git("ls-files", "-z").decode().split("\0"))
    names.update(git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"))
    source = digest_files([ROOT / name for name in names if name], ROOT)
    reports = {}
    for item in evidence:
        name, raw_path = item.split("=", 1)
        path = Path(raw_path)
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("validation evidence must be a JSON object")
        reports[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            **{key: value[key] for key in ("status", "scope", "checks", "limits", "elapsed_seconds")
               if key in value},
        }
    bundles = {}
    for item in artifacts:
        name, raw_path = item.split("=", 1)
        path = Path(raw_path).resolve()
        if not path.exists():
            raise FileNotFoundError("artifact unavailable")
        bundles[name] = digest_files(path.rglob("*") if path.is_dir() else [path],
                                     path if path.is_dir() else path.parent)
    return {
        "schema_version": 1, "recorded_at": datetime.now().astimezone().isoformat(),
        "application_version": (ROOT / "VERSION").read_text().strip(),
        "source": {"commit": git("rev-parse", "HEAD").decode().strip(),
                   "dirty": bool(git("status", "--porcelain")), **source},
        "evidence": reports, "artifacts": bundles,
        "deployment": {"status": "not_verified", "image_digest": None,
                       "database_schema": None, "environment": None},
        "production_acceptance": {"status": "pending",
                                 "required": ["fixed artifact deployment", "24h mixed workload",
                                              "complete trading day", "upgrade and business restore"]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", action="append", default=[], metavar="NAME=JSON")
    parser.add_argument("--artifact", action="append", default=[], metavar="NAME=PATH")
    args = parser.parse_args()
    record = collect(args.evidence, args.artifact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(f"validation record written; production remains {record['production_acceptance']['status']}")


if __name__ == "__main__":
    main()
