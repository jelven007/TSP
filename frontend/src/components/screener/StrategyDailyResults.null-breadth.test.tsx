// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it.each(['available_members_v1', undefined])(
  'opens the historical S02 buy tab with a disabled breadth threshold (%s)', async policy => {
    const result = {
      as_of: '2026-11-24', strategy: 'custom_mvxxcp88', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
      entry_data_quality: { csgi_late_session_strength: {
        pool_total: 3036, pool_usable: 3033, known_positive: 834,
        missing_symbols: ['000635.SZ', '002872.SZ', '603580.SH'],
        pool_up_pct: 27.4975, pool_up_pct_lower: 27.4704, pool_up_pct_upper: 27.5692,
        pool_up_min_pct: null, stage: 'confirmed', warnings: [],
        breadth_policy: policy,
      } },
    } as ScreenerResult
    const host = document.createElement('div')
    document.body.append(host)
    const root = createRoot(host)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    try {
      await act(async () => root.render(<QueryClientProvider client={client}>
        <StrategyDailyResults result={result} currentDate="2026-11-25" />
      </QueryClientProvider>))
      await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[2].click())
      expect(host.querySelector('[role="tabpanel"]')).not.toBeNull()
      expect(host.textContent).toContain('仅展示，不参与买入筛选')
      expect(host.textContent).not.toContain('阈值 0.00%')
      expect(host.textContent).not.toContain('已按下界确认市场条件')
      expect(host.textContent).toContain('完整池 3036')
    } finally {
      await act(async () => root.unmount())
      client.clear()
      host.remove()
    }
  },
)
