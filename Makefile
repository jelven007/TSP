PYTHON ?= python3
UV ?= uv
PNPM ?= pnpm

.PHONY: help check check-docs test-tooling test-backend test-frontend build-frontend

help:
	@echo "check           Check structure, documentation, version and diff whitespace"
	@echo "check-docs      Check local Markdown and preserved requirement contracts"
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

test-tooling:
	$(UV) run --project backend --frozen --no-sync pytest tests/tooling -q

test-backend:
	cd backend && $(UV) run --frozen --no-sync pytest tests -q

test-frontend:
	cd frontend && $(PNPM) test

build-frontend:
	cd frontend && $(PNPM) build
