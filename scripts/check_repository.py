"""Check source directories and stable entry points without starting services."""

import argparse
from pathlib import Path

REQUIRED_PATHS = (
    "AGENTS.md", "CONTRIBUTING.md", "README.md", "VERSION", "Makefile",
    "Dockerfile", "docker-compose.yml", ".env.example", "tiers.yaml",
    "dev.sh", "dev.ps1", "update.ps1",
    ".github/workflows/ci.yml", ".github/rulesets",
    "backend/app/api", "backend/app/services", "backend/app/data_providers",
    "backend/app/plugins", "backend/app/tickflow", "backend/app/indicators",
    "backend/app/strategy", "backend/app/factors", "backend/app/backtest",
    "backend/app/jobs", "backend/app/multitenancy", "backend/app/cell/sql",
    "backend/app/extensions", "backend/app/custom", "backend/native/rustdx_native",
    "backend/pyproject.toml", "backend/uv.lock", "backend/tests/fixtures",
    "backend/scripts/acceptance/__init__.py", "backend/scripts/performance/__init__.py",
    "backend/scripts/operations/__init__.py", "backend/scripts/research/__init__.py",
    "backend/scripts/diagnostics/__init__.py", "backend/scripts/demos/__init__.py",
    "frontend/src/pages", "frontend/src/components", "frontend/src/lib",
    "frontend/src/extensions", "frontend/src/custom", "frontend/public",
    "frontend/package.json", "frontend/pnpm-lock.yaml", "mcp-server/src/tsp_mcp_server",
    "deploy/full", "deploy/edge", "deploy/cell/pg", "deploy/windows", "packaging",
    "scripts/acceptance", "scripts/performance", "scripts/research",
    "scripts/release/source_recovery.py", "tests/tooling/test_source_recovery.py",
    "scripts/check_version.py", "scripts/build_manifest.py", "scripts/upgrade_check.py",
    "tests/tooling/conftest.py", "docs/README.md", "docs/requirements.md", "examples/open-api",
    "examples/README.md", "examples/custom-data-source/mock_server.py",
    "examples/custom-data-source/mock_source.yaml",
    "brand", "screenshots", "reports/README.md",
    "scripts/check_docs.py", "tests/tooling/test_documentation_contract.py",
    "docs/architecture.md", "docs/testing.md", "docs/operations.md", "docs/release-operations.md",
    "docs/secondary-development.md", "docs/version-baseline.md",
    "docs/reference/engineering.md", "docs/reference/market-data.md",
    "docs/reference/research.md", "docs/reference/factor-system.md",
    "docs/reference/trading-ai.md", "docs/reference/integrations.md",
    "docs/planning/phase2-implementation-plan.md", "操作说明书.md",
    "reports/production-system-consolidation-20261023/requirement-ids.json",
)


def check(root: Path) -> list[str]:
    errors = [f"missing required path: {name}" for name in REQUIRED_PATHS if not (root / name).exists()]
    if (root / "docs/examples").exists():
        errors.append("examples belong in examples/, not docs/examples/")
    for pattern in ("scripts/test_*.py", "backend/scripts/*.py"):
        for path in sorted(root.glob(pattern)):
            if path.name != "__init__.py":
                errors.append(f"uncategorized script: {path.relative_to(root)}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check(args.root.resolve())
    if errors:
        parser.exit(1, "\n".join(errors) + "\n")
    print(f"Repository layout passed: {len(REQUIRED_PATHS)} required paths.")


if __name__ == "__main__":
    main()
