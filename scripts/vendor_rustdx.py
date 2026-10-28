#!/usr/bin/env python3
"""Check fixed rustdx source identity offline; explicitly refresh audited hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c4c0273456874898aed50447fd900521d9ffd3b8"
VERSION = "2.0.4"
CORE = Path("backend/native/rustdx_native/vendor/rustdx-core")
IDENTITY = Path("backend/app/plugins/rustdx/_native_identity.py")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def files(core: Path) -> dict[str, str]:
    paths = [core / name for name in ("Cargo.toml", "Cargo.lock", "LICENSE", "README.md", "PATCHES.md")]
    paths += list((core / "src").rglob("*"))
    paths += list((core / "tests/fixtures").rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("vendored source cannot contain symlinks")
    return {
        path.relative_to(core).as_posix(): digest(path.read_bytes())
        for path in sorted(paths) if path.is_file()
    }


def artifacts(original: dict, adapted: dict) -> tuple[dict, bytes, str]:
    sums = "".join(f"{sha}  {name}\n" for name, sha in sorted(adapted.items())).encode()
    patches = {
        name: {"upstream": original.get(name), "adapted": sha}
        for name, sha in adapted.items() if sha != original.get(name)
    }
    patch_hash = digest(json.dumps(patches, sort_keys=True, separators=(",", ":")).encode())
    payload = {
        "repository": "https://github.com/jelven007/rustdx", "revision": REVISION,
        "version": VERSION, "original_sha256": original, "adapted_sha256": adapted,
        "patches": patches, "patch_sha256": patch_hash, "core_sha256": digest(sums),
    }
    identity = (
        '"""Generated from audited vendored core; see scripts/vendor_rustdx.py."""\n'
        f'CORE_SHA256 = "{payload["core_sha256"]}"\n'
        f'PATCH_SHA256 = "{patch_hash}"\n'
    )
    return payload, sums, identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--upstream", type=Path)
    args = parser.parse_args()
    core = args.root / CORE
    adapted = files(core)
    if args.refresh:
        if args.upstream is None:
            parser.error("--refresh requires --upstream at the audited revision")
        entries = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", REVISION, "--", "crates/rustdx-core/src"],
            cwd=args.upstream, text=True,
        ).splitlines()
        if len(entries) != 101:
            raise ValueError(f"expected 101 upstream source files, found {len(entries)}")
        upstream_paths = {path.removeprefix("crates/rustdx-core/"): path for path in entries}
        upstream_paths.update({
            "LICENSE": "LICENSE", "README.md": "crates/rustdx-core/README.md",
            "Cargo.toml": "crates/rustdx-core/Cargo.toml",
            "tests/fixtures/kline/stock-day.response.hex":
                "crates/rustdx-core/tests/fixtures/kline/stock-day.response.hex",
        })
        original = {
            name: digest(subprocess.check_output(["git", "show", f"{REVISION}:{path}"], cwd=args.upstream))
            for name, path in sorted(upstream_paths.items())
        }
    else:
        original = json.loads((core / "UPSTREAM.json").read_text())["original_sha256"]
    payload, sums, identity = artifacts(original, adapted)
    json_data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    expected = {
        core / "UPSTREAM.json": json_data.encode(),
        core / "SOURCE-SHA256SUMS": sums,
        args.root / IDENTITY: identity.encode(),
    }
    if args.refresh:
        for path, content in expected.items():
            path.write_bytes(content)
    elif any(path.read_bytes() != content for path, content in expected.items()):
        raise ValueError("vendored core or generated identity has unregistered changes")
    print(json.dumps({
        "revision": REVISION, "files": len(adapted), "core_sha256": payload["core_sha256"],
        "patch_sha256": payload["patch_sha256"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
