PYTHON ?= python3
UV ?= uv
PNPM ?= pnpm
RUFF ?= ruff

.PHONY: help check check-docs check-release test-tooling test-backend test-frontend build-frontend

help:
	@echo "check           Check structure, documentation, version and diff whitespace"
	@echo "check-docs      Check local Markdown and preserved requirement contracts"
	@echo "check-release   Run fast checks, release Ruff and focused tooling tests"
	@echo "test-tooling    Run repository CLI and workflow regression tests"
	@echo "test-backend    Run backend tests"
	@echo "test-frontend   Run frontend tests"
	@echo "build-frontend  Compile TypeScript and build frontend assets"

check:
	$(PYTHON) scripts/check_repository.py
	$(PYTHON) scripts/check_docs.py
	$(PYTHON) scripts/check_version.py --current VERSION
	git diff --check

check-docs:
	$(PYTHON) scripts/check_docs.py

check-release: check
	$(RUFF) check --no-cache --config backend/pyproject.toml scripts/release/common.py scripts/release/pipeline.py scripts/release/preflight.py scripts/release/business_diff.py scripts/release/oci.py scripts/release/image_manifest.py scripts/release/promote.py scripts/release/source_recovery.py tests/tooling/test_release_*.py tests/tooling/test_source_recovery.py tests/tooling/test_version_workflows.py
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m pytest tests/tooling/test_release_*.py tests/tooling/test_source_recovery.py tests/tooling/test_version_workflows.py -q -p no:cacheprovider

test-tooling:
	$(UV) run --project backend --frozen --no-sync pytest tests/tooling -q

test-backend:
	@tmp_dir="$$(mktemp -d "$${TMPDIR:-/tmp}/tsp-tests.XXXXXX")"; \
	trap 'rm -rf "$$tmp_dir"' 0; \
	cd backend && \
	TICKFLOW_ENV_FILE=/dev/null \
	MULTITENANCY_ENABLED=false \
	SERVICE_ROLE=standalone \
	DATA_DIR="$$tmp_dir" \
	POLARS_MAX_THREADS=4 \
	$(UV) run --frozen --no-sync pytest tests -q

test-frontend:
	cd frontend && $(PNPM) test

build-frontend:
	cd frontend && $(PNPM) build
