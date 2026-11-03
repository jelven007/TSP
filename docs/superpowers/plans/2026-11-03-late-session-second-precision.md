# Late-Session Second Precision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the custom late-session entry clock functionally second-precise while continuing to evaluate only complete minute bars.

**Architecture:** Persist the exact `HH:mm:ss` start time and derive two separate timestamps: an exact evaluation time (`start + 90 seconds`) and a minute-floor bar label. The frontend and backend enforce the same inclusive `13:00:00`–`14:58:00` range; frozen legacy definitions with 旧 0 秒、60 秒 confirmation remain whole-minute only.

**Tech Stack:** React 19, TypeScript, Vitest, FastAPI, Python 3.12, Polars, Pytest, Playwright.

---

No Git commit is included because the user authorized implementation and local installation, not committing or pushing.

### Task 1: Backend timing contract

**Files:**
- Modify: `backend/tests/test_late_session_strength.py`
- Modify: `backend/app/strategy/late_session_strength.py`

- [ ] **Step 1: Write failing exact-second timing tests**

Add a test that proves the exact start, minute-floor data label, exact 90-second gate, and boundary validation:

```python
def test_second_start_uses_minute_bar_and_exact_ninety_second_gate():
    from app.strategy.late_session_strength import bar_time, evaluation_time

    sig = definition(time="14:30:20", evaluation_delay_minutes=1.5)
    custom_signals.validate(sig)
    assert bar_time(sig, DAY) == TARGET
    assert evaluation_time(sig, DAY) == TARGET + timedelta(minutes=1, seconds=50)
    assert evaluate(sig=sig, completed_through=TARGET + timedelta(minutes=1, seconds=49)).symbols == []
    assert evaluate(sig=sig, completed_through=TARGET + timedelta(minutes=1, seconds=50)).symbols == POOL[:3]


@pytest.mark.parametrize("patch", [
    {"time": "14:58:01", "evaluation_delay_minutes": 1.5},
    {"time": "14:30:20"},
    {"time": "14:30:20", "evaluation_delay_minutes": 1},
])
def test_second_start_rejects_close_overrun_and_legacy_delays(patch):
    with pytest.raises(ValueError):
        custom_signals.validate(definition(**patch))
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/test_late_session_strength.py::test_second_start_uses_minute_bar_and_exact_ninety_second_gate \
  backend/tests/test_late_session_strength.py::test_second_start_rejects_close_overrun_and_legacy_delays -q
```

Expected: FAIL because `parameters()` rejects `14:30:20` and `bar_time()` does not floor seconds.

- [ ] **Step 3: Implement the minimal backend behavior**

Adjust validation and minute labeling:

```python
at = time.fromisoformat(params["time"])
if at.strftime("%H:%M:%S") != params["time"] or at.tzinfo:
    raise ValueError
if delay != 1.5 and at.second:
    raise ValueError
if not earliest <= at <= latest:
    raise ValueError
```

```python
def bar_time(sig: dict, day: date) -> datetime:
    at = time.fromisoformat(parameters(sig)["time"]).replace(second=0, microsecond=0)
    return datetime.combine(day, at)
```

Update the validation error to state that 90-second definitions support seconds while frozen 0/60-second definitions require `00`.

- [ ] **Step 4: Run backend timing tests and verify GREEN**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_late_session_strength.py -q
```

Expected: all tests pass; exact-second evaluation consumes only the floored minute row.

### Task 2: Frontend picker, validation, and summaries

**Files:**
- Modify: `frontend/src/components/signals/CustomSignalDialog.time.test.tsx`
- Modify: `frontend/src/lib/customSignals.test.ts`
- Modify: `frontend/src/components/signals/CustomSignalDialog.tsx`
- Modify: `frontend/src/lib/customSignals.ts`

- [ ] **Step 1: Write failing dialog and summary tests**

Replace the whole-minute tail test with second-precision assertions:

```typescript
expect(f.time('启动时间')?.textContent).toBe('14:30:00')
await act(async () => f.time('启动时间').click())
expect(document.querySelector('[aria-label="00 秒"]')).not.toBeNull()
await f.select('启动时间', '14:30:20')
expect(document.querySelector('[role="dialog"]')?.textContent).toContain('14:31:50')
expect(document.querySelector('[role="dialog"]')?.textContent).toContain('K线截止 14:30:00')
```

Save, rerender, and assert `time: '14:30:20'`; then accept `14:58:00` and reject `14:58:01`.

Add the summary contract:

```typescript
const exact = lateSessionSummary({
  ...signal,
  params: { ...LATE_SESSION_DEFAULTS, time: '14:30:20' },
}).join(' ')
expect(exact).toContain('启动时间 14:30:20')
expect(exact).toContain('确认时间 14:31:50')
expect(exact).toContain('K线截止 14:30:00')
```

- [ ] **Step 2: Run frontend tests and verify RED**

Run:

```bash
PATH="/Users/bytedance/.trae-cn/binaries/node/versions/24.22.1/bin:$PATH" \
  pnpm --dir frontend test -- \
  src/components/signals/CustomSignalDialog.time.test.tsx \
  src/lib/customSignals.test.ts
```

Expected: FAIL because the tail picker has no second column, saves `:00`, and summaries use the exact start as the bar label.

- [ ] **Step 3: Implement the minimal frontend behavior**

Use the second picker and preserve its exact value:

```tsx
<TimePicker aria-label="启动时间" showSeconds align="left"
  value={lateParams.time} onChange={time => setDraft(d => ({
    ...d, params: { ...lateParams, time },
  }))}
  className="w-full" buttonClassName="h-9 w-full justify-start" />
```

Validate exact seconds and the inclusive range:

```typescript
if (!/^1[34]:[0-5]\d:[0-5]\d$/.test(p.time)
    || p.time < '13:00:00' || p.time > '14:58:00')
  throw new Error('启动时间须为 13:00:00～14:58:00')
```

Derive the data label without changing the saved start:

```typescript
export const lateSessionBarTime = (time: string): string =>
  `${time.slice(0, 5)}:00`
```

Use `lateSessionBarTime(p.time)` in dialog help and summaries.

- [ ] **Step 4: Run frontend tests and verify GREEN**

Run:

```bash
PATH="/Users/bytedance/.trae-cn/binaries/node/versions/24.22.1/bin:$PATH" \
  pnpm --dir frontend test -- \
  src/components/signals/CustomSignalDialog.time.test.tsx \
  src/components/signals/CustomSignalDialog.test.tsx \
  src/components/TimePicker.test.tsx \
  src/lib/customSignals.test.ts
```

Expected: all selected tests pass.

### Task 3: Full scoped regression and documentation

**Files:**
- Modify: `docs/requirements.md`
- Modify: `docs/architecture.md`
- Modify: `docs/secondary-development.md`
- Modify: `docs/testing.md`
- Modify: `docs/operations.md`
- Modify: `docs/release-operations.md`

- [ ] **Step 1: Update six lifecycle documents**

Record the exact contract in each document:

```text
尾盘启动时间保存 HH:mm:ss；14:30:20 使用 14:30:00 分钟K并于 14:31:50 确认。
范围为 13:00:00～14:58:00；旧0/60秒定义保持整分钟。
```

State explicitly that this does not add second-level K lines, matching, backtesting, schema, API fields, or provider capabilities.

- [ ] **Step 2: Run backend, frontend, lint, build, and docs checks**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_late_session_strength.py -q
backend/.venv/bin/python -m ruff check \
  backend/app/strategy/late_session_strength.py backend/tests/test_late_session_strength.py
PATH="/Users/bytedance/.trae-cn/binaries/node/versions/24.22.1/bin:$PATH" \
  pnpm --dir frontend test -- \
  src/components/signals/CustomSignalDialog.time.test.tsx \
  src/components/signals/CustomSignalDialog.test.tsx \
  src/components/TimePicker.test.tsx \
  src/lib/customSignals.test.ts
PATH="/Users/bytedance/.trae-cn/binaries/node/versions/24.22.1/bin:$PATH" \
  pnpm --dir frontend exec eslint \
  src/components/signals/CustomSignalDialog.tsx \
  src/components/signals/CustomSignalDialog.time.test.tsx \
  src/lib/customSignals.ts src/lib/customSignals.test.ts
PATH="/Users/bytedance/.trae-cn/binaries/node/versions/24.22.1/bin:$PATH" \
  pnpm --dir frontend build
backend/.venv/bin/python scripts/check_docs.py
git diff --check
```

Expected: all commands pass. React Router future warnings may remain; no test, lint, build, docs, or whitespace failure is allowed.

### Task 4: Rendered QA and local installation

**Files:**
- Create outside repository: private Playwright acceptance script and installation receipts
- Create: `deliverables/operations/late-session-second-precision-local-20261103/本地安装验收.md`

- [ ] **Step 1: Verify rendered behavior before installation**

Use Playwright because the Browser plugin is unavailable. Test:

```text
/signals → edit tail signal → open second picker → Escape → choose 14:30:20
→ observe 14:31:50 and 14:30:00 → cancel → reopen unchanged.
```

Cover light/dark and `1440×1000`/`390×844`; assert no framework overlay, target API failure, clipping, or signal mutation.

- [ ] **Step 2: Prepare a matched local runtime**

Clone the active local runtime, copy only verified backend/frontend/docs changes, reuse the verified frontend build, rebuild a Compute image with matching backend and static files, and verify both build manifests.

- [ ] **Step 3: Protect state and activate**

Wait for pipeline/research tasks to drain. Stop all writers, create a 双库 backup with two PostgreSQL dumps, restore both into temporary databases, compare every table, and retain controlled configuration, previous runtime source, and previous Compute image.

Activate the new Runtime, frontend pointer, and Compute image without changing the application version or existing signal parameters.

- [ ] **Step 4: Restart and repeat live acceptance**

Verify all supervisor roles, API/Platform/frontend readiness, administrator login, public binding, real Compute health, current-day stock-pool API, exact signal/strategy definitions, and stable configuration. Perform a complete restart and repeat the rendered flow.

- [ ] **Step 5: Write the local acceptance receipt**

Document:

```text
v0.4.46 local iteration; online untouched; no version bump.
Active tail time remains 14:30:00 unless the user edits it.
Second-precision capability is installed; no strategy run, order, or backtest was started.
```

Include actual test counts, restore counts, browser scenarios, known external version-check warnings, rollback assets, and any normal scheduled writes observed during installation.
