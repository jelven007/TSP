// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it.each([true, false])('distinguishes a confirmed empty buy from an unfinished task: %s', async confirmed => {
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result: ScreenerResult = {
    strategy: 'custom_mvxxcp88', as_of: '2026-11-17', rows: [], total: 0, elapsed_ms: 0,
    section_mode: 'signals', sections: { select: [], buy: [], sell: [] },
    selection_only: !confirmed,
    section_statuses: { buy: confirmed ? 'success' : 'expired' },
    section_errors: confirmed ? {} : { buy: '当日未完成买入确认' },
    execution_status: { state: confirmed ? 'success' : 'expired', updated_at: 1,
      reason: confirmed ? '买入确认完成，0只入场信号；依据当日成功任务回执' : '当日未完成买入确认' },
  }
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={result} activeTab="buy" currentDate="2026-11-17" />
    </QueryClientProvider>))
    expect(host.querySelector('[aria-selected="true"]')?.textContent).toBe(confirmed ? '买 · 0' : '买 · 未确认')
    expect(host.textContent).toContain(confirmed ? '买入确认完成 · 0只' : '今日买入未确认')
    expect(host.textContent).not.toContain('当前展示上次成功结果')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})
