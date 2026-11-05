# Backtest Entry Capacity and Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the five requested backtest defaults and execute default daily/minute entries through a five-minute, minute-high, 100%-volume-capacity window with partial fills and residual cancellation.

**Architecture:** Add one pure entry-window executor that owns minute selection, price, capacity, partial fills, aggregate fees, and diagnostics. Route generic daily portfolio/full simulation and generic minute replay through that executor, then expose the resulting fills and order summary to the existing result contract. Keep the five-value browser migration isolated in a frontend helper with a one-time version marker.

**Tech Stack:** Python 3.12, Polars, NumPy, Pytest, FastAPI/Pydantic, React 19, TypeScript, Vitest, pnpm.

---

No Git commit, push, deployment, version bump, or local product-service startup is included because the user requested source implementation only and the local environment is paused.

### Task 1: One-time five-value frontend migration

**Files:**
- Create: `frontend/src/pages/backtest/defaults.ts`
- Create: `frontend/src/pages/backtest/defaults.test.ts`
- Modify: `frontend/src/lib/storage.ts`
- Modify: `frontend/src/pages/backtest/StrategyBacktest.tsx`
- Modify: `frontend/src/pages/backtest/StrategyBacktest.test.tsx`

- [ ] **Step 1: Write failing migration tests**

Create tests proving fresh defaults, exact request units, one-time migration, and preservation after manual edits:

```typescript
it('supplies the five requested defaults without saved state', () => {
  expect(loadBacktestDefaults(null)).toMatchObject({
    defaultsVersion: 1,
    maxPositions: '5',
    stampTax: '1',
    slippage: '10',
    minimumCommission: '5',
    transferFee: '5',
  })
})

it('migrates only the five fields once and preserves later edits', () => {
  const first = loadBacktestDefaults({
    selectedStrategy: 'sample',
    maxPositions: '10',
    stampTax: '',
    slippage: '5',
    minimumCommission: '0',
    transferFee: '0',
    initialCapital: '500000',
  })
  expect(first).toMatchObject({
    defaultsVersion: 1,
    maxPositions: '5',
    stampTax: '1',
    slippage: '10',
    minimumCommission: '5',
    transferFee: '5',
    initialCapital: '500000',
  })
  expect(loadBacktestDefaults({ ...first, maxPositions: '7' }).maxPositions).toBe('7')
})
```

Add a page test that runs a backtest and asserts:

```typescript
expect(startBacktest).toHaveBeenCalledWith(expect.objectContaining({
  max_positions: 5,
  stamp_tax_pct: 0.001,
  slippage_bps: 10,
  minimum_commission: 5,
  transfer_fee_pct: 0.0005,
}))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PATH="/Users/bytedance/.local/bin:$PATH" pnpm --dir frontend exec vitest run \
  src/pages/backtest/defaults.test.ts \
  src/pages/backtest/StrategyBacktest.test.tsx
```

Expected: FAIL because the helper/version field does not exist and current defaults are `10 / auto / 5 / 0 / 0`.

- [ ] **Step 3: Implement the migration helper**

Use a single versioned transformation:

```typescript
export const BACKTEST_DEFAULTS_VERSION = 1
export const REQUESTED_BACKTEST_DEFAULTS = {
  maxPositions: '5',
  stampTax: '1',
  slippage: '10',
  minimumCommission: '5',
  transferFee: '5',
} as const

export function loadBacktestDefaults<T extends Record<string, unknown> | null>(saved: T) {
  if ((saved?.defaultsVersion as number | undefined) === BACKTEST_DEFAULTS_VERSION) return saved
  return {
    ...(saved ?? {}),
    ...REQUESTED_BACKTEST_DEFAULTS,
    defaultsVersion: BACKTEST_DEFAULTS_VERSION,
  }
}
```

Add `defaultsVersion?: number` to `strategyBacktestLast`, initialize component state from the migrated object, and persist the version with successful-run settings. Do not alter any other saved field.

- [ ] **Step 4: Run frontend migration tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

### Task 2: Pure five-minute entry-window executor

**Files:**
- Create: `backend/app/backtest/entry_execution.py`
- Create: `backend/tests/backtest/test_entry_execution.py`

- [ ] **Step 1: Write failing pure executor tests**

Cover one-bar full fill, five-bar partial fill, weighted price, zero volume, one-price limit-up, aggregate fees, lunch, and close truncation:

```python
def test_entry_window_fills_at_each_minute_high_and_cancels_residual():
    result = execute_entry_window(
        bars(
            highs=[10.0, 10.2, 10.1, 10.3, 10.4],
            volumes=[1, 1, 1, 1, 1],
        ),
        symbol="600001.SH",
        start_at=datetime(2026, 1, 5, 9, 31),
        prior_close=9.5,
        budget=10_000,
        size_order=lambda price: 1_000,
        buy_value=lambda notional: notional + max(5, notional * 0.0002)
        + notional * (0.0005 + 0.001),
    )
    assert result.target_shares == 1_000
    assert result.filled_shares == 500
    assert result.cancelled_shares == 500
    assert [fill.price for fill in result.fills] == [10.0, 10.2, 10.1, 10.3, 10.4]
    assert result.average_price == pytest.approx(10.2)
    assert sum(fill.fee for fill in result.fills) == pytest.approx(result.fee)
```

```python
def test_zero_volume_and_one_price_limit_up_consume_slots_without_fill():
    result = execute_entry_window(
        bars(highs=[10, 11, 10.2, 10.3, 10.4], lows=[10, 11, 10, 10, 10],
             volumes=[0, 10, 1, 1, 1]),
        symbol="600001.SH", start_at=START, prior_close=10,
        budget=100_000, size_order=lambda _: 500, buy_value=lambda n: n,
    )
    assert [fill.bar for fill in result.fills] == [573, 574, 575]
    assert result.filled_shares == 300
```

- [ ] **Step 2: Run pure tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/backtest/test_entry_execution.py -q
```

Expected: collection FAIL because `entry_execution` does not exist.

- [ ] **Step 3: Implement immutable execution records and executor**

Define:

```python
@dataclass(frozen=True)
class EntryFill:
    datetime: datetime
    bar: int
    shares: int
    price: float
    notional: float
    fee: float
    minute_volume_shares: float
    participation: float


@dataclass(frozen=True)
class EntryExecution:
    target_shares: int
    filled_shares: int
    cancelled_shares: int
    notional: float
    fee: float
    value: float
    average_price: float | None
    fills: tuple[EntryFill, ...]
    status: Literal["filled", "partial", "unfilled"]
```

Implement `execute_entry_window()` with these invariants:

```python
window = expected_trading_minutes(start_at, count=5, same_day=True)
capacity = floor(float(row["volume"]) * 100 / step) * step
price = min(limit_up, float(row["high"]))
before = buy_value(notional)
after = buy_value(notional + quantity * price)
fill_fee = (after - (notional + quantity * price)) - (before - notional)
```

Choose the first valid bar high as the input to `size_order()`. Reduce each proposed fragment by board lot until aggregate `buy_value()` fits the budget. Missing, zero-volume, malformed, or one-price-limit-up bars remain empty slots.

- [ ] **Step 4: Run pure tests and verify GREEN**

Run the command from Step 2. Expected: all executor tests pass.

### Task 3: Daily strategy position/full simulation integration

**Files:**
- Modify: `backend/tests/backtest/test_minute_fill.py`
- Modify: `backend/tests/backtest/test_engine_portfolio.py`
- Modify: `backend/app/backtest/engine.py`
- Modify: `backend/app/backtest/strategy.py`

- [ ] **Step 1: Write failing daily integration tests**

Add one position-mode and one full-mode test. Both must assert:

```python
assert trade.entry_date == "2026-01-06"
assert trade.entry_price == pytest.approx(weighted_high_price)
assert trade.raw_entry_shares == expected_partial_shares
assert result.fills == expected_five_fragments
assert result.stats["execution"]["entry_cancelled_shares"] == expected_cancelled
```

Add a missing-partition test:

```python
assert result.error == "2026-01-06 缺分钟K分区, 无法执行五分钟建仓容量撮合"
```

- [ ] **Step 2: Run daily integration tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/backtest/test_minute_fill.py \
  backend/tests/backtest/test_engine_portfolio.py -q
```

Expected: FAIL because `open_t+1` still uses one daily open/full quantity and minute fill returns one scalar price.

- [ ] **Step 3: Route both daily simulation paths through the executor**

In both account-level and independent simulation:

```python
execution = execute_entry_window(
    minute_frame,
    symbol=symbol,
    start_at=next_session_open_bar(day),
    prior_close=prior_raw_close,
    budget=entry_budget,
    size_order=lambda price: config.buy_quantity(entry_budget, price, symbol),
    buy_value=config.buy_value,
)
```

Create a position only when `execution.filled_shares > 0`; use `execution.average_price`, `execution.value`, and actual shares. Append each `EntryFill` to the result fill ledger and increment `entry_target_shares`, `entry_filled_shares`, `entry_cancelled_shares`, `entry_partial_orders`, and `entry_unfilled_orders`.

Do not call the legacy scalar `_resolve_minute_fill()` for `open_t+1` entries. Retain it only for explicitly legacy close-time/minute-reference behavior and existing exit support.

- [ ] **Step 4: Run daily integration tests and verify GREEN**

Run the command from Step 2. Expected: all selected daily matcher tests pass.

### Task 4: Generic minute-strategy integration

**Files:**
- Modify: `backend/tests/backtest/test_minute_backtest.py`
- Modify: `backend/tests/backtest/test_morning_backtest.py`
- Modify: `backend/app/backtest/minute_replay.py`
- Modify: `backend/app/backtest/morning_backtest.py`
- Modify: `backend/app/backtest/strategy.py`

- [ ] **Step 1: Write failing signal-availability and partial-fill tests**

Add a 90-second-confirmation fixture:

```python
assert signal.available_at == datetime(2026, 1, 5, 14, 31, 30)
assert result.fills[0]["datetime"] == "2026-01-05 14:32:00"
assert all(fill["price"] == source_high[fill["datetime"]] for fill in result.fills)
assert result.trades[0]["raw_entry_shares"] == partial_shares
```

Add a close-boundary test where a `14:58` signal can consume only `14:59` and `15:00`; remaining shares are cancelled that day.

- [ ] **Step 2: Run minute strategy tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/backtest/test_minute_backtest.py \
  backend/tests/backtest/test_morning_backtest.py -q
```

Expected: FAIL because minute replay produces one scalar `raw_entry_price` and full target quantity.

- [ ] **Step 3: Preserve availability and defer sizing to the cash ledger**

Extend `MinuteReplayHit` with `entry_window_start` instead of pre-aggregating a fictitious full fill:

```python
@dataclass(frozen=True)
class MinuteReplayHit:
    ...
    entry_window_start: datetime
```

Derive it causally:

```python
entry_window_start = first_complete_trading_minute_after(
    max(trigger + timedelta(minutes=1), available_at or trigger)
)
```

At the portfolio cash ledger, call `execute_entry_window()` after the target budget is known. Record actual shares, weighted price, fragments, and cancelled residual. Apply one aggregate commission/transfer/slippage calculation per order.

- [ ] **Step 4: Run minute strategy tests and verify GREEN**

Run the command from Step 2. Expected: selected minute and morning tests pass.

### Task 5: Result contract and backtest UI

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/pages/backtest/StrategyBacktest.tsx`
- Modify: `frontend/src/pages/backtest/StrategyBacktest.test.tsx`
- Modify: `frontend/src/components/MinuteExecutionLedger.tsx`

- [ ] **Step 1: Write failing UI contract tests**

Assert the page text and partial order detail:

```typescript
expect(host.textContent).toContain('次日开盘起5分钟容量撮合')
expect(host.textContent).toContain('信号确认后下一分钟起5分钟容量撮合')
expect(host.textContent).toContain('目标 1,000 股')
expect(host.textContent).toContain('成交 500 股')
expect(host.textContent).toContain('撤单 500 股')
```

- [ ] **Step 2: Run UI tests and verify RED**

Run:

```bash
PATH="/Users/bytedance/.local/bin:$PATH" pnpm --dir frontend exec vitest run \
  src/pages/backtest/StrategyBacktest.test.tsx
```

Expected: FAIL because the current minute label says signal-minute close and result types have no order summary.

- [ ] **Step 3: Update types, labels, help, and ledger**

Extend fill/result types:

```typescript
type EntryOrderSummary = {
  symbol: string
  signalTime: string
  windowStart: string
  targetShares: number
  filledShares: number
  cancelledShares: number
  averagePrice: number | null
  status: 'filled' | 'partial' | 'unfilled'
}
```

Render entry fills without nesting cards. Update `FillRuleHint` to state that the rule uses minute highs, five 100%-volume capacity slots, weighted average price, and partial cancellation.

- [ ] **Step 4: Run UI tests and verify GREEN**

Run the command from Step 2. Expected: all selected UI tests pass.

### Task 6: Compatibility, documentation, and final verification

**Files:**
- Modify: `backend/tests/backtest/test_cost_model.py`
- Modify: `backend/tests/backtest/test_overnight_fees.py`
- Modify: `docs/reference/s02.md`
- Modify: `docs/testing.md`
- Modify: `docs/version-baseline.md`

- [ ] **Step 1: Add regression tests for aggregated fees and frozen dedicated execution**

Prove the generic executor charges one minimum commission across five fragments:

```python
assert sum(item.fee for item in execution.fills) == pytest.approx(
    max(5, execution.notional * 0.0002)
    + execution.notional * (0.0005 + 0.001)
)
```

Run the existing dedicated overnight fee tests unchanged to prove its fixed-fee behavior remains separate.

- [ ] **Step 2: Update documentation**

Document:

- the five defaults and units;
- minute-high price rather than next-minute high as a single full fill;
- five-minute capacity/partial-cancel behavior;
- daily next-session and minute-signal availability clocks;
- aggregate order fees;
- no retroactive rewrite of historical frozen results.

- [ ] **Step 3: Run backend verification**

Run:

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/backtest/test_entry_execution.py \
  backend/tests/backtest/test_cost_model.py \
  backend/tests/backtest/test_minute_fill.py \
  backend/tests/backtest/test_engine_portfolio.py \
  backend/tests/backtest/test_minute_backtest.py \
  backend/tests/backtest/test_morning_backtest.py \
  backend/tests/backtest/test_overnight_fees.py -q
```

Expected: all selected backend tests pass with zero failures.

- [ ] **Step 4: Run frontend verification**

Run:

```bash
PATH="/Users/bytedance/.local/bin:$PATH" pnpm --dir frontend exec vitest run \
  src/pages/backtest/defaults.test.ts \
  src/pages/backtest/StrategyBacktest.test.tsx \
  src/lib/backtestTask.test.tsx
PATH="/Users/bytedance/.local/bin:$PATH" pnpm --dir frontend exec eslint \
  src/pages/backtest/defaults.ts \
  src/pages/backtest/defaults.test.ts \
  src/pages/backtest/StrategyBacktest.tsx \
  src/pages/backtest/StrategyBacktest.test.tsx \
  src/components/MinuteExecutionLedger.tsx \
  src/lib/storage.ts \
  src/lib/api.ts
PATH="/Users/bytedance/.local/bin:$PATH" pnpm --dir frontend build
```

Expected: tests, affected lint, TypeScript, and production build pass.

- [ ] **Step 5: Run repository checks and inspect scope**

Run:

```bash
make check
git diff --check
git status --short
```

Expected: repository checks pass; diff contains only requested code/tests/docs plus pre-existing user changes. Do not start `full-dev.py`, PostgreSQL, Colima, or any online deployment command.
