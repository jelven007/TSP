"""Verify portable private defaults and install missing platform extension metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.multitenancy.business_defaults import Snapshot, canonical, load_snapshot


class Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1]
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: dict[str, str] = Field(max_length=201)


def _file(root: Path, name: str) -> Path:
    if name != "manifest.json" and not re.fullmatch(
        r"ext_data/[A-Za-z0-9_-]{1,128}/config\.json", name,
    ):
        raise ValueError("unsupported defaults bundle path")
    path = root / name
    if root.is_symlink() or any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise ValueError("defaults bundle symlink rejected")
    return path


def load_bundle(root: Path) -> tuple[Snapshot, dict[str, bytes]]:
    """Check every file before any write, including the exact extension factor catalog."""
    from app.factors.ext_factors import ext_factor_specs
    from app.services.ext_data import ExtConfig

    try:
        index_path = root / "bundle.json"
        if index_path.is_symlink() or index_path.stat().st_size > 64 * 1024:
            raise ValueError("invalid defaults bundle index")
        index = Bundle.model_validate_json(index_path.read_bytes())
        if "manifest.json" not in index.files:
            raise ValueError("defaults bundle manifest missing")
        configs = {}
        for name, expected in index.files.items():
            path = _file(root, name)
            if not re.fullmatch(r"[a-f0-9]{64}", expected) or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("invalid defaults bundle file identity")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("defaults bundle file digest mismatch")
            if name != "manifest.json":
                config = ExtConfig.from_dict(json.loads(raw))
                if name != f"ext_data/{config.id}/config.json":
                    raise ValueError("defaults bundle extension identity mismatch")
                if config.mode not in {"snapshot", "timeseries"} or not config.fields:
                    raise ValueError("invalid defaults bundle extension")
                configs[config.id] = raw
        actual = {path.relative_to(root).as_posix() for path in root.rglob("config.json")}
        if actual != set(index.files) - {"manifest.json"}:
            raise ValueError("unindexed defaults bundle extension")
        snapshot = load_snapshot(_file(root, "manifest.json"))
        if snapshot.snapshot_id != index.snapshot_id:
            raise ValueError("defaults bundle snapshot mismatch")
        specs = json.loads(json.dumps([asdict(spec) for spec in ext_factor_specs(root)],
            default=lambda value: sorted(value) if isinstance(value, frozenset) else value))
        expected_specs = [spec for spec in snapshot.payload.factor_catalog if spec["id"].startswith("ext_")]
        if canonical(sorted(specs, key=lambda row: row["id"])) != canonical(
            sorted(expected_specs, key=lambda row: row["id"]),
        ):
            raise ValueError("defaults bundle extension factor catalog mismatch")
        return snapshot, configs
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("defaults bundle incomplete or unreadable") from exc


def install_extensions(root: Path, data_dir: Path, *, apply: bool = False) -> dict:
    """Preview by default; exclusive creation preserves existing platform configurations."""
    snapshot, configs = load_bundle(root)
    receipt = {"snapshot_id": snapshot.snapshot_id, "applied": apply,
               "creates": [], "matching": [], "preserved": []}
    for config_id, raw in configs.items():
        path = data_dir / "ext_data" / config_id / "config.json"
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("extension installation symlink rejected")
        if path.exists():
            receipt["matching" if path.read_bytes() == raw else "preserved"].append(config_id)
        else:
            receipt["creates"].append(config_id)
    if apply:
        for config_id in list(receipt["creates"]):
            path = data_dir / "ext_data" / config_id / "config.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as stream:
                    stream.write(configs[config_id])
            except FileExistsError:
                receipt["creates"].remove(config_id)
                receipt["preserved"].append(config_id)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--platform-data", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and args.platform_data is None:
        parser.error("--apply requires --platform-data")
    if args.platform_data is not None:
        receipt = install_extensions(args.bundle, args.platform_data, apply=args.apply)
    else:
        snapshot, configs = load_bundle(args.bundle)
        receipt = {"snapshot_id": snapshot.snapshot_id, "extensions": len(configs),
                   "documents": len(snapshot.payload.documents)}
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
