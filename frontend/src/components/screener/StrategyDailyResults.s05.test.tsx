// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

it('shows canonical branch score in the same column on buy and next-day sell', async () => {
  const host = document.createElement('div')
  const root = createRoot(host)
  const client = new QueryClient()
  const a = { symbol: '600001.SH', score: 96, signal_score: 0.2, signal_score_rule: 'return_5m' }
  const b = { symbol: '600002.SH', score: 70, signal_score: 3.2, signal_score_rule: 'return_5m' }
  try {
    for (const side of ['buy', 'sell'] as const) {
      const value = {
        as_of: '2026-11-26', strategy: 'custom_mvxxcs05', section_mode: 'signals',
        rows: [], total: 2, elapsed_ms: 0,
        sections: { buy: [], sell: [], select: [], [side]: [a, b] },
        selection_snapshot: { as_of: '2026-11-26', branch_code: side === 'buy' ? 1 : 4 },
      } as unknown as ScreenerResult
      await act(async () => root.render(<QueryClientProvider client={client}>
        <StrategyDailyResults result={value} activeTab={side} currentDate="2026-11-27" />
      </QueryClientProvider>))
      expect(host.querySelector('tbody tr')?.textContent).toContain('600002.SH')
      expect(host.querySelector('td[data-column="score"]')?.textContent).toBe('3.20%')
      expect(host.querySelector('th[data-column="score"]')?.getAttribute('aria-sort')).toBe('descending')
      expect(host.textContent).not.toContain('ATR14÷昨收')
    }
  } finally {
    await act(async () => root.unmount())
    client.clear()
  }
})

it('changes the buy sort with the archived branch and preserves an empty result note', async () => {
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const render = async (branch: 'tail' | 'low_gap' | 'board' | null, sort: string, empty = false) => {
    const buy = empty ? [] : [
      { symbol: '600001.SH', score: 90, entry_sort_value: 1.25 },
      { symbol: '600002.SH', score: 82, entry_sort_value: 2.5 },
    ]
    const result = {
      as_of: '2026-11-24', strategy: 'custom_mvxxcs05', rows: buy, total: buy.length, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy },
      selection_snapshot: { as_of: '2026-11-24', source_date: '2026-11-23',
        generated_at: '2026-11-23T16:30:00', rows: [], entry_routing: 's02_173004552_v1', entry_branch: branch,
        ...(branch === null ? { entry_route: { schema: 's05_market_route_v2', matched: [] } } : {}) },
      entry_data_quality: { active: { stage: 'confirmed', entry_branch: branch, sort_by: sort, fund_weight_pct: 26 } },
    } as unknown as ScreenerResult
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={result} activeTab="buy" currentDate="2026-11-26" />
    </QueryClientProvider>))
  }
  try {
    await render('tail', 'return_5m')
    const sort = () => host.querySelector('th[data-column="entry_sort_value"]')
    expect(sort()?.textContent).toContain('近5分钟涨幅')
    expect(sort()?.getAttribute('aria-sort')).toBe('descending')
    expect(host.querySelector('tbody tr')?.textContent).toContain('600002.SH')
    expect(host.querySelector('tbody td[data-column="entry_sort_value"]')?.textContent).toBe('2.50%')
    await render('low_gap', 'fund_tech')
    expect(sort()?.textContent).toContain('资金技术加权分')
    expect(host.textContent).toContain('资金26%＋技术74%')
    expect(host.querySelector('tbody td[data-column="entry_sort_value"]')?.textContent).toBe('2.50')
    await render('board', 'four_dimension', true)
    expect(host.textContent).toContain('买入排序：四维综合分降序')
    expect(host.textContent).not.toContain('资金技术加权分')
    await render(null, '', true)
    expect(host.textContent).toContain('买 · 无分支')
    expect(host.textContent).toContain('三种市场分支均未满足，本交易日不新买')
    expect(host.textContent).not.toContain('四维综合分降序')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})

it('skips only new buys on branch four and still displays the sell roster', async () => {
  const value = {
    as_of: '2026-11-27', strategy: 'custom_mvxxcs05', section_mode: 'signals', rows: [], total: 0, elapsed_ms: 0,
    sections: { sell: [{ symbol: '600001.SH', name: '卖出关注' }], select: [], buy: [] },
    monitor_tasks: { sell: { completion_reason: 's05_no_daily_branch' } },
    selection_snapshot: { as_of: '2026-11-27', source_date: '2026-11-26', rows: [],
      entry_branch: null, branch_code: 4, entry_route: { schema: 's05_daily_branch_v3', matched: [] } },
  } as unknown as ScreenerResult
  const client = new QueryClient()
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={value} activeTab="buy" currentDate="2026-11-26" />
    </QueryClientProvider>))
    expect(host.textContent).toContain('交易日分支参数：4 · 均不满足 · 2026-11-27')
    expect(host.textContent).toContain('买 · 无分支')
    expect(host.textContent).toContain('卖 · 1')
    expect(host.textContent).toContain('22:00～23:00')
    expect(host.textContent).toContain('三种市场分支均未满足，本交易日不新买')
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={value} activeTab="sell" currentDate="2026-11-26" />
    </QueryClientProvider>))
    expect(host.textContent).toContain('此日期的卖任务为旧规则归档')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})
