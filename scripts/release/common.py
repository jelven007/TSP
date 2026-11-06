"""Small byte-identity and private receipt helpers shared by release CLIs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("identity input must be a regular file")
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def object_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.absolute() != path.parent.resolve():
        raise ValueError("receipt path must not contain symlinks")
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity(version: str, revision: str) -> None:
    if not re.fullmatch(r"[0-9]\.[0-9]\.(?:0|[1-9][0-9]?)", version):
        raise ValueError("invalid version")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid revision")
