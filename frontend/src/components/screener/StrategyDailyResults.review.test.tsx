// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it('shows the postclose review reason and zero-buy market rejection without missing quotes', async () => {
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result: ScreenerResult = {
    strategy: 'custom_mvxxcp88', as_of: '2026-11-09', rows: [], total: 0, elapsed_ms: 0,
    section_mode: 'signals', sections: { select: [{ symbol: '600001.SH' }], buy: [], sell: [] },
    section_statuses: { select: 'success', buy: 'success', sell: 'success' },
    execution_status: { state: 'success', reason: '盘后复核完成；当天未完成盘中确认', updated_at: 123 },
    entry_data_quality: { late: { pool_total: 3049, pool_usable: 3049,
      known_positive: 1339, missing_symbols: [], breadth_policy: 'available_members_v1',
      pool_up_pct: 43.916038, pool_up_pct_lower: 43.916038, pool_up_pct_upper: 43.916038,
      pool_up_min_pct: 60, stage: 'breadth_rejected', warnings: [] } },
  }
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={result} currentDate="2026-11-09" />
    </QueryClientProvider>))
    expect(host.querySelector('[role="status"]')?.textContent).toContain('盘后复核完成；当天未完成盘中确认')
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[2].click())
    expect(host.querySelector('[aria-selected="true"]')?.textContent).toBe('买 · 0')
    expect(host.textContent).toContain('上涨比例 43.92%')
    expect(host.textContent).toContain('阈值 60.00%')
    expect(host.textContent).toContain('完整池 3049 只 · 有效 3049 只 · 剔除 0 只')
    expect(host.textContent).toContain('市场条件未满足')
    expect(host.querySelector('[role="alert"]')).toBeNull()
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})
