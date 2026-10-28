"""Temporary evidence runner; workspace files are read-only."""
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

root = Path("/Users/bytedance/Documents/TSPP")
base = Path("/tmp/tspp-indices-baseline")
target = Path("/tmp/tspp-indices-reverse")
target.mkdir(exist_ok=True)
backend = target / "backend"
backend.mkdir(exist_ok=True)
shutil.copytree(root / "backend/app", backend / "app", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
tests = backend / "tests"
tests.mkdir(exist_ok=True)
shutil.copy2(root / "backend/tests/test_index_kline_periods.py", tests / "test_index_kline_periods.py")
environment = {
    **os.environ, "PYTHONPATH": str(backend), "TICKFLOW_ENV_FILE": "/dev/null",
    "MULTITENANCY_ENABLED": "false", "SERVICE_ROLE": "standalone",
    "DATA_DIR": str(target / "runtime"),
}
records = []

def inverse_case(label, path, content, selectors):
    current = path.read_text()
    path.write_text(content)
    try:
        command = [str(root / "backend/.venv/bin/python"), "-m", "pytest", *selectors, "-q"]
        run = subprocess.run(command, cwd=backend, env=environment, text=True, capture_output=True)
        (target / f"{label}.log").write_text(run.stdout + run.stderr)
        records.append({"case": label, "exit_code": run.returncode, "summary": run.stdout.strip().splitlines()[-1]})
        assert run.returncode == 1, (label, run.stdout, run.stderr)
    finally:
        path.write_text(current)

const = backend / "app/services/index_const.py"
inverse_case("catalog-four", const,
    const.read_text().replace("for symbol, name in PAGE_INDEX_NAMES.items()", "for symbol, name in list(PAGE_INDEX_NAMES.items())[:4]"),
    ["tests/test_index_kline_periods.py::test_catalog_seven_and_sidebar_four"])
repo = backend / "app/tickflow/repository.py"
inverse_case("period-storage-absent", repo,
    (base / "backend/app/tickflow/repository.py").read_text(),
    ["tests/test_index_kline_periods.py::test_period_store_idempotent_readonly_and_indicators"])
inverse_case("concurrent-snapshot", repo,
    repo.read_text().replace(
        'base_fp = self._partition_fingerprint(out)\n            existing = pl.read_parquet(out) if base_fp is not None else pl.DataFrame()',
        'existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()\n            base_fp = self._partition_fingerprint(out)'),
    ["tests/test_index_kline_periods.py::test_missing_write_rechecks_snapshot_when_concurrent_writer_fills_gap"])
frontend = target / "frontend"
frontend.mkdir(exist_ok=True)
shutil.copytree(root / "frontend/src", frontend / "src", dirs_exist_ok=True)
for filename in ("package.json", "vite.config.ts", "tsconfig.json", "tsconfig.node.json"):
    shutil.copy2(root / "frontend" / filename, frontend / filename)
if not (frontend / "node_modules").exists():
    (frontend / "node_modules").symlink_to(root / "frontend/node_modules", target_is_directory=True)
shutil.copy2(base / "frontend/src/pages/Indices.tsx", frontend / "src/pages/Indices.tsx")
command = ["pnpm", "test", "src/pages/Indices.test.tsx", "-t", "shows seven|opens data repair"]
run = subprocess.run(command, cwd=frontend, text=True, capture_output=True)
(target / "original-index-page.log").write_text(run.stdout + run.stderr)
records.append({"case": "original-index-page", "exit_code": run.returncode, "summary": run.stdout.strip().splitlines()[-3:]})
assert run.returncode == 1, (run.stdout, run.stderr)
(target / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))

paths = [str(p.relative_to(base / "backend")) for p in (base / "backend").rglob("*.py")]
paths += ["app/api/data.py", "app/services/index_kline.py", "tests/test_index_kline_periods.py"]
lint = []
for relative in paths:
    path = root / "backend" / relative
    original = base / "backend" / relative
    if original.exists():
        before = original.read_text()
    elif relative in ("app/services/index_kline.py", "tests/test_index_kline_periods.py"):
        before = ""
    else:
        before = subprocess.check_output(["git", "show", f"HEAD:backend/{relative}"], cwd=root, text=True)
    results = []
    for content in (before, path.read_text()):
        command = ["uv", "run", "--frozen", "--no-sync", "ruff", "check", "--output-format=json", "--stdin-filename", relative, "-"]
        run = subprocess.run(command, input=content, cwd=root / "backend", text=True, capture_output=True)
        diagnostics = json.loads(run.stdout)
        results.append(Counter((d["code"], d["message"]) for d in diagnostics))
    added = results[1] - results[0]
    lint.append({"file": relative, "baseline": sum(results[0].values()), "current": sum(results[1].values()),
                 "added": [list(key) + [count] for key, count in added.items()]})
assert all(not item["added"] for item in lint), lint
(target / "ruff-comparison.json").write_text(json.dumps(lint, ensure_ascii=False, indent=2))
print(json.dumps({"reverse": records, "ruff_files": len(lint), "new_diagnostics": 0}, ensure_ascii=False))
