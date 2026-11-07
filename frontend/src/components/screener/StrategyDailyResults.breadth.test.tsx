// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it('shows uncertain breadth as a range alongside confirmed buy state', async () => {
  const result: ScreenerResult = {
    as_of: '2026-11-04', strategy: 'custom_mvxxcp88', rows: [], total: 0, elapsed_ms: 0,
    section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
    entry_data_quality: { csgi_late_session_strength: {
      pool_total: 3049, known_positive: 2337, missing_symbols: ['601326.SH'],
      pool_up_pct: null, pool_up_pct_lower: 76.64808133814365,
      pool_up_pct_upper: 76.68087897671367, pool_up_min_pct: 60,
      stage: 'confirmed', warnings: ['缺少昨收'],
    } },
  }
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={result} activeTab="buy" />
    </QueryClientProvider>))
    expect(host.textContent).toContain('76.65%～76.68%')
    expect(host.textContent).toContain('完整池 3049 只')
    expect(host.textContent).toContain('1 只行情未知')
    expect(host.textContent).toContain('已按下界确认')
    expect(host.textContent).not.toContain('买 · 待确认')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})

it.each([
  { usable: 3048, missing: 1, pct: 76.6732283464567, stage: 'confirmed', expected: '76.67%' },
  { usable: 1, missing: 3048, pct: 100, stage: 'confirmed', expected: '100.00%' },
  { usable: 0, missing: 3049, pct: null, stage: 'waiting_data', expected: '无有效行情，等待数据' },
])('shows available breadth and exclusion counts without a missing-percent gate', async (sample) => {
  const result = {
    as_of: '2026-11-04', strategy: 'custom_mvxxcp88', rows: [], total: 0, elapsed_ms: 0,
    section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
    entry_data_quality: { csgi_late_session_strength: {
      pool_total: 3049, pool_usable: sample.usable, known_positive: 2337,
      missing_symbols: Array.from({ length: sample.missing }, (_, i) => `s${i}`),
      pool_up_pct: sample.pct, pool_up_pct_lower: 76.648, pool_up_pct_upper: 76.681,
      pool_up_min_pct: 60, stage: sample.stage, warnings: [],
      breadth_policy: 'available_members_v1',
    } },
  } as ScreenerResult
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={result} activeTab="buy" />
    </QueryClientProvider>))
    expect(host.textContent).toContain(sample.expected)
    expect(host.textContent).toContain(`有效 ${sample.usable} 只`)
    expect(host.textContent).toContain(`剔除 ${sample.missing} 只`)
    expect(host.textContent).not.toContain('已按下界确认')
    expect(host.textContent).not.toContain('区间跨过阈值')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})
