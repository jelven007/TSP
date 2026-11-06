"""Run a controlled release plan with private atomic receipts and no blind replay."""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.common import atomic_json, file_hash, identity, object_hash

PHASES = ("preflight", "quality", "artifacts", "backup", "switch", "acceptance",
          "retain", "publish", "cleanup", "final")
PAUSE_MARKER = Path.home() / ".cache/tsp-dev/TSPP/local.paused"


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def lock_path(plan: dict) -> Path:
    return Path(plan["lock_dir"]) / (object_hash(plan["environment"]) + ".lock")


def validate(plan: dict) -> None:
    identity(plan["version"], plan["revision"])
    if plan.get("format") != 1 or not plan.get("environment") or not plan.get("steps"):
        raise ValueError("invalid plan")
    if not Path(plan["lock_dir"]).is_absolute() or not plan.get("inputs"):
        raise ValueError("absolute lock directory and frozen inputs required")
    names, previous = set(), -1
    for step in plan["steps"]:
        name = step["id"]
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or name in names:
            raise ValueError("invalid step id")
        names.add(name)
        if step["phase"] not in PHASES or PHASES.index(step["phase"]) < previous:
            raise ValueError("invalid phase order")
        previous = PHASES.index(step["phase"])
        if step["effect"] not in ("read", "write"):
            raise ValueError("invalid effect")
        if type(step.get("timeout_seconds", 3600)) is not int or step.get("timeout_seconds", 3600) <= 0:
            raise ValueError("positive timeout required")
        for field in ("argv", "probe", "postcheck"):
            argv = step[field]
            if not isinstance(argv, list) or not argv or not all(
                isinstance(arg, str) and arg and "\0" not in arg for arg in argv
            ):
                raise ValueError("commands require argv arrays")


def execute(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    # The plan is trusted private executable input, never a shell template.
    # Do not copy command output or arguments (which may contain secrets) to receipts.
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)


def check(argv: list[str], timeout: int, *, post: bool = False) -> str:
    result = execute(argv, timeout)
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("postcheck/probe must return JSON") from exc
    if result.returncode or not isinstance(value, dict) or (post and value.get("status") != "passed"):
        raise ValueError("postcheck/probe failed")
    return object_hash(value)


def run(plan_path: Path, receipts: Path, *, reconcile: bool = False,
        resume_local: bool = False) -> dict:
    plan = json.loads(plan_path.read_text())
    validate(plan)
    for raw, digest in plan["inputs"].items():
        path = Path(raw)
        if not path.is_absolute() or file_hash(path) != digest:
            raise ValueError("frozen input changed or unavailable")
    if plan["environment"] == "local" and any(s["effect"] == "write" for s in plan["steps"]):
        if PAUSE_MARKER.exists() or Path(plan.get("pause_marker", PAUSE_MARKER)).exists():
            raise ValueError("local environment is paused")
        if not resume_local:
            raise ValueError("local write requires explicit user resume authorization")
    lock_file = lock_path(plan)
    lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock_file.is_symlink() or lock_file.parent.absolute() != lock_file.parent.resolve():
        raise ValueError("lock path contains symlinks")
    with lock_file.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("environment is locked by another release") from exc
        path = receipts / "receipt.json"
        fingerprint = object_hash(plan)
        record = json.loads(path.read_text()) if path.exists() else {
            "format": 1, "plan_sha256": fingerprint, "environment": plan["environment"],
            "revision": plan["revision"], "version": plan["version"], "steps": {},
            "started_at": timestamp(),
        }
        if record["plan_sha256"] != fingerprint:
            raise ValueError("plan input changed; use a new receipt directory")
        record.update(status="running", attempt_started_at=timestamp())
        atomic_json(path, record)
        started = time.monotonic()
        try:
            run_steps(plan, record, path, reconcile=reconcile)
            record["status"] = "passed"
            record.pop("active_step", None)
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
            record["status"] = "failed"
            raise
        finally:
            record.update(ended_at=timestamp(), attempt_elapsed_seconds=round(time.monotonic() - started, 6))
            atomic_json(path, record)
        return record


def run_steps(plan: dict, record: dict, path: Path, *, reconcile: bool) -> None:
    fingerprint = record["plan_sha256"]
    predecessors = {}
    for step in plan["steps"]:
        name = step["id"]
        record["active_step"] = name
        old = record["steps"].get(name)
        timeout = step.get("timeout_seconds", 3600)
        base_input = object_hash({"plan": fingerprint, "predecessors": predecessors})
        if old and old["base_input_sha256"] != base_input:
            raise ValueError("upstream input changed")
        if old and old["status"] == "passed":
            if check(step["postcheck"], timeout, post=True) != old["output_sha256"]:
                raise ValueError("postcheck identity changed; prior receipt is stale")
            predecessors[name] = old["output_sha256"]
            continue
        if old and step["effect"] == "write":
            if not reconcile:
                raise ValueError("interrupted write requires --reconcile; action will not replay")
            started = time.monotonic()
            output = check(step["postcheck"], timeout, post=True)
            old.update(status="passed", reconciled=True, reconciled_at=timestamp(),
                       reconcile_elapsed_seconds=round(time.monotonic() - started, 6),
                       output_sha256=output)
            atomic_json(path, record)
            predecessors[name] = old["output_sha256"]
            continue
        probe = check(step["probe"], timeout)
        row = {"phase": step["phase"], "effect": step["effect"], "status": "running",
               "started_at": timestamp(), "base_input_sha256": base_input,
               "input_sha256": object_hash({"base": base_input, "probe": probe}),
               "attempt": old.get("attempt", 1) + 1 if old else 1,
               "prior_attempts": [*old.get("prior_attempts", []),
                   {key: value for key, value in old.items() if key != "prior_attempts"}
               ] if old else []}
        record["steps"][name] = row
        atomic_json(path, record)
        started = time.monotonic()
        try:
            result = execute(step["argv"], timeout)
            if result.returncode:
                row["returncode"] = result.returncode
                raise ValueError("stage command failed")
            row["output_sha256"] = check(step["postcheck"], timeout, post=True)
            row["status"] = "passed"
        except (ValueError, OSError, subprocess.TimeoutExpired):
            row["status"] = "failed"
            row["failure_class"] = "reconcile_required" if step["effect"] == "write" else "retryable"
            raise
        finally:
            row.update(ended_at=timestamp(), elapsed_seconds=round(time.monotonic() - started, 6))
            atomic_json(path, record)
        predecessors[name] = row["output_sha256"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--resume-local", action="store_true",
                        help="only after explicit user authorization to resume local services")
    args = parser.parse_args()
    try:
        result = run(args.plan, args.receipts, reconcile=args.reconcile, resume_local=args.resume_local)
    except (ValueError, TypeError, OSError, KeyError, subprocess.TimeoutExpired):
        parser.exit(1, "release stopped; inspect private receipt status and reconcile actual state\n")
    print(f"Verified {len(result['steps'])} stages for {result['version']}; see private receipts")


if __name__ == "__main__":
    main()
