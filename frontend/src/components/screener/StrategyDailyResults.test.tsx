// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, name, navList, onClose, onNavigate }: any) => symbol ? (
    <div role="dialog" aria-label="个股详情">
      <span>{symbol} {name}</span>
      <button onClick={onClose}>关闭个股详情</button>
      <button onClick={() => onNavigate(navList[1].symbol, navList[1].name)}>下一只</button>
    </div>
  ) : null,
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let qc: QueryClient
afterEach(async () => { await act(async () => root?.unmount()); host?.remove(); qc?.clear() })
async function render(result: ScreenerResult) {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <StrategyDailyResults result={result} positionLimit={5} />
  </QueryClientProvider>))
}

describe('StrategyDailyResults', () => {
  it('renders custom signal tabs without portfolio columns and keeps saved selection order', async () => {
    await render({
      as_of: '2026-11-02', strategy: 'custom_test', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals',
      sections: {
        sell: [{ symbol: '600001.SH', entry_date: '2026-10-30', close: 10.2,
          last_datetime: '2026-10-30T14:30:00', change_pct: .02 }],
        select: [{ symbol: '600003.SH', score: 100 }, { symbol: '600002.SH', score: 50 }],
        buy: [],
      },
    })
    expect(host.textContent).toContain('前一交易日')
    expect(host.textContent).toContain('触发时间')
    expect(host.textContent).toContain('2.00%')
    expect(host.textContent).not.toContain('需卖股数')
    const tabs = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    await act(async () => tabs[1].click())
    expect([...host.querySelectorAll('tbody tr')].map(row => row.textContent?.slice(0, 9)))
      .toEqual(['600003.SH', '600002.SH'])
    await act(async () => tabs[2].click())
    expect(host.textContent).toContain('该交易日没有触发入场触发器的股票')
    expect(host.textContent).not.toContain('成交结果')
  })
  it('shows missing prior signal archive explicitly instead of a successful empty sell list', async () => {
    await render({
      as_of: '2026-11-02', strategy: 'custom_test', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', section_errors: { sell: '缺少2026-10-30买入信号归档' },
      sections: { sell: [], select: [], buy: [] },
    })
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('缺少2026-10-30')
    expect(host.querySelector('[role="tab"]')?.textContent).toBe('卖 · 待确认')
    expect(host.textContent).not.toContain('该交易日没有需要卖出的股票')
  })
  it('shows sell select buy in order and keeps full selected rows separate', async () => {
    await render({
      as_of: '2026-10-30', strategy: 's02', rows: [], total: 0, elapsed_ms: 0,
      sections: {
        sell: [{ symbol: '600001.SH', shares: 1000, net_stop_quote: 9.42, gross_target_quote: 10.6 }],
        select: [{ symbol: '600002.SH', return_1430: .02 }, { symbol: '600003.SH', return_1430: .03 }],
        buy: [{ symbol: '600002.SH', filled_shares: 900, buy_price: 10.2 }],
      },
    })
    const tabs = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    expect(tabs.map(el => el.textContent)).toEqual(['卖 · 1', '选 · 2', '买 · 1'])
    expect(host.textContent).toContain('扣费净止损价')
    await act(async () => tabs[1].click())
    expect(host.textContent).toContain('600003.SH')
    await act(async () => tabs[2].click())
    expect(host.textContent).not.toContain('600003.SH')
    expect(host.textContent).toContain('10.20')
  })
  it('shows empty and execution failures explicitly', async () => {
    await render({ as_of: '2026-10-30', strategy: 's02', rows: [], total: 0,
      elapsed_ms: 0, execution_error: '11:30到期剩余100股',
      sections: { sell: [], select: [], buy: [] } })
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('到期剩余')
    expect(host.textContent).toContain('该交易日没有需要卖出的股票')
  })
  it.each(['sell', 'select', 'buy'] as const)('opens stock details from code and name in %s', async section => {
    await render({
      as_of: '2026-07-15', strategy: 's02', rows: [], total: 0, elapsed_ms: 0,
      sections: { sell: [], select: [], buy: [],
        [section]: [{ symbol: '600001.SH', name: '第一只' }, { symbol: '600002.SH', name: '第二只' }] },
    })
    const index = ['sell', 'select', 'buy'].indexOf(section)
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
    for (const label of ['600001.SH', '第一只']) {
      const target = [...host.querySelectorAll<HTMLButtonElement>('[role="tabpanel"] button')]
        .find(button => button.textContent === label)
      expect(target).toBeDefined()
      await act(async () => target!.click())
      expect(host.querySelector('[role="dialog"]')?.textContent).toContain('600001.SH 第一只')
      await act(async () => [...host.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
        .find(button => button.textContent === '下一只')!.click())
      expect(host.querySelector('[role="dialog"]')?.textContent).toContain('600002.SH 第二只')
      await act(async () => [...host.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
        .find(button => button.textContent === '关闭个股详情')!.click())
      expect(host.querySelector('[role="dialog"]')).toBeNull()
    }
  })
})
