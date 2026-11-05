# Remove builtin S02 Implementation Plan

> **For agentic workers:** Execute these scoped steps inline. Deletion is explicitly authorized; no commit, push, version increment, new backtest or remote deployment.

**Goal:** Remove local builtin strategy ID `s02` and its owned research data while preserving custom ID `custom_mvxxcp88`.

**Architecture:** Remove only the discovered builtin entry and its family membership. Use existing tenant result lifecycle and research deletion transactions, then align local host and Compute catalogs. Delete confirmed builtin or mixed comparison artifacts by a bounded manifest; retain independent custom results and shared market data.

**Tech Stack:** Python, PostgreSQL tenant documents, FastAPI, Docker Compute, pytest.

---

- [x] Add a catalog/API regression in `backend/tests/test_builtin_strategy_families.py`: builtin `s02` absent, `s01` present, custom strategy named `s02` still accessible; confirm failure before removal.
- [x] Delete `backend/app/strategy/builtin/s02.py`, remove its family member, adapt catalog expectations and use a test-local momentum fixture.
- [x] Verify affected catalog, monitor, shared momentum and deletion regressions: 109 passed; isolated PostgreSQL follow-up: 23 passed.
- [x] Delete exact tenant-owned results, research, configuration and monitor/pool task references; verify non-target data and tombstones.
- [x] Align current local host runtime and Compute; restart and verify actual builtin detail 404, custom detail 200.
- [x] Delete 349 manifest-confirmed builtin/mixed files and verify 110 retained custom files.
- [x] Record counts and verification in `reports/operations/builtin-s02-local-retirement-20261104/`; update current lifecycle documentation.
- [x] Verify fresh SQL/API/Compute/file checks, Ruff, diff formatting and 278 Markdown documents.
