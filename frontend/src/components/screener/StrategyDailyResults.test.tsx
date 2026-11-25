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
  it('removes S01 explanation on every result tab', async () => {
    await render({
      as_of: '2026-11-23', strategy: 'custom_mvxxcs01', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
    })
    for (const index of [0, 1, 2]) {
      await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
      expect([...host.querySelectorAll('summary')].some(s => s.textContent === '策略说明')).toBe(false)
      expect(host.querySelector('[aria-label="上涨占比"]')?.textContent).toBe('上涨占比 · —')
    }
  })
  it('removes the S02 explanation in sell, select and buy while reserving four metrics', async () => {
    await render({
      as_of: '2026-11-23', strategy: 'custom_mvxxcp88', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
    })
    for (const index of [0, 1, 2]) {
      await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
      expect([...host.querySelectorAll('summary')].some(s => s.textContent === '策略说明')).toBe(false)
      expect(host.querySelectorAll('[data-progress-step]')).toHaveLength(4)
    }
  })
  it('keeps historical S03 score labels and does not invent five-dimensional columns', async () => {
    const rows = [{ symbol: '600001.SH', name: '旧归档', score: 100,
      atr14_previous_close: .05, atr_rank: 1 }]
    await render({
      as_of: '2026-11-20', strategy: 'custom_mvxxcs03', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: rows, buy: [] },
    })
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
    const headers = [...host.querySelectorAll('th')].map(th => th.textContent)
    expect(headers).toContain('选股评分')
    expect(headers).toContain('ATR14÷昨收')
    expect(headers).not.toContain('综合分')
    expect(headers).not.toContain('技术分')
  })
  it('shows S03 saved five-dimensional scores and removes the explanation on every tab', async () => {
    const rows = [{ symbol: '600002.SH', name: '第一只', score: 85, tech_score: 90,
      fund_score: 80, emotion_score: 85, fundamental_score: 70, news_score: 60 },
      { symbol: '600001.SH', name: '第二只', score: 70, tech_score: 70,
        fund_score: 60, emotion_score: 80, fundamental_score: 50, news_score: 50 }]
    await render({
      as_of: '2026-11-23', strategy: 'custom_mvxxcs03', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: rows, buy: [] },
      selection_snapshot: { as_of: '2026-11-23', source_date: '2026-11-20',
        generated_at: '2026-11-20T21:10:00+08:00', rows,
        scoring_policy: 's03_five_dimensions_v1', coverage: { candidate_total: 36, scorable_total: 35 } },
    })
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
    expect(host.querySelector('details')).toBeNull()
    const headers = [...host.querySelectorAll('th')].map(th => th.textContent)
    for (const label of ['综合分', '技术分', '资金分', '情绪分', '基本面分', '消息分']) {
      expect(headers).toContain(label)
    }
    expect(headers).toContain('ATR14÷昨收')
    const symbols = [...host.querySelectorAll('tbody tr')].map(tr => tr.textContent)
    // 共享结果组件保持已保存顺位，不在浏览器按代码重新排列。
    expect(symbols[0]).toContain('600002.SH')
    expect(symbols[1]).toContain('600001.SH')
    for (const index of [0, 2]) {
      await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
      expect(host.querySelector('details')).toBeNull()
    }
  })
  it('shows S01 saved cumulative metrics and keeps stock navigation in all tabs', async () => {
    const row = { symbol: '600001.SH', name: '第一只', current_return: .025,
      current_range: .031, amount: 210000000, score: .014 }
    await render({
      as_of: '2026-11-05', strategy: 'custom_mvxxcs01', rows: [row], total: 1, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [row], select: [row], buy: [row] },
    })
    for (const index of [0, 1, 2]) {
      await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
      expect(host.textContent).not.toContain('ATR')
      expect(host.textContent).not.toContain('94%')
      expect(host.textContent).not.toContain('10万元')
      expect(host.querySelector('button[title="查看个股详情"]')).not.toBeNull()
      if (index !== 1) {
        expect(host.textContent).toContain('2.50%')
        expect(host.textContent).toContain('3.10%')
        expect(host.textContent).toContain('累计成交额')
      }
    }
  })
  it('keeps strategy explanations collapsed while errors remain visible, preserving disclosure on tab and data changes', async () => {
    const result: ScreenerResult = {
      as_of: '2026-11-05', strategy: 'custom_other', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
      section_errors: { buy: '分钟文件不可读' }, execution_error: '行情读取失败',
      execution_status: { state: 'retrying', reason: '等待行情恢复', updated_at: 1793840000000 },
    }
    await render(result)
    const disclosure = host.querySelector('details')!
    expect(disclosure).not.toBeNull()
    expect(disclosure.open).toBe(false)
    expect(disclosure.querySelector('summary')?.textContent).toBe('策略说明')
    expect(disclosure.textContent).toContain('卖：前一交易日')
    expect(host.querySelector('[role="status"]')?.closest('details')).toBeNull()
    expect(host.querySelector('[role="alert"]')?.closest('details')).toBeNull()
    await act(async () => disclosure.querySelector('summary')!.click())
    expect(disclosure.open).toBe(true)
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[2].click())
    expect(disclosure.open).toBe(true)
    expect([...host.querySelectorAll('[role="alert"]')].map(alert => alert.textContent))
      .toEqual(['行情读取失败', '分钟文件不可读'])
    expect([...host.querySelectorAll('[role="alert"]')].every(alert => !alert.closest('details'))).toBe(true)
    await act(async () => root.render(<QueryClientProvider client={qc}>
      <StrategyDailyResults result={{ ...result, computed_at: 1793840001000 }} positionLimit={5} />
    </QueryClientProvider>))
    expect(host.querySelector('details')).toBe(disclosure)
    expect(disclosure.open).toBe(true)
    await act(async () => disclosure.querySelector('summary')!.click())
    expect(disclosure.open).toBe(false)
  })
  it('distinguishes a failed buy from zero hits and labels the previous success', async () => {
    await render({
      as_of: '2026-11-05', strategy: 'custom_test', rows: [], total: 1, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy: [{ symbol: '600001.SH' }] },
      section_errors: { buy: '分钟文件不可读' }, section_statuses: { buy: 'failed' },
      execution_status: { state: 'retrying', reason: '分钟文件不可读', updated_at: 1793840000000 },
      last_success_at: 1793831400000, showing_last_success: true,
    } as ScreenerResult)
    expect(host.querySelectorAll('[role="tab"]')[2].textContent).toBe('买 · 失败')
    expect(host.textContent).toContain('重试中')
    expect(host.textContent).toContain('上次成功结果')
  })
  it.each(['sell', 'buy'] as const)('shows saved ATR ranks and scores in %s without live repricing', async section => {
    const rows = [
      { symbol: '600002.SH', score: 100, atr14_previous_close: .083, atr_rank: 1 },
      { symbol: '600001.SH', score: 0, atr14_previous_close: .041, atr_rank: 2 },
    ]
    await render({
      as_of: '2026-11-05', strategy: 'custom_test', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [], buy: [], [section]: rows },
    })
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[section === 'sell' ? 0 : 2].click())
    expect(host.textContent).toContain('ATR14÷昨收')
    expect(host.textContent).toContain('排名分数')
    expect([...host.querySelectorAll('tbody [data-column="atr14_previous_close"]')].map(cell => cell.textContent))
      .toEqual(['8.30%', '4.10%'])
    expect([...host.querySelectorAll('tbody [data-column="atr_rank"]')].map(cell => cell.textContent)).toEqual(['1', '2'])
    expect([...host.querySelectorAll('tbody [data-column="score"]')].map(cell => cell.textContent)).toEqual(['100.00', '0.00'])
    if (section === 'sell') expect(host.textContent).toContain('继承前一交易日')
  })
  it('opens saved preselection on select and shows source time without a target-day return', async () => {
    await render({
      as_of: '2026-11-02', strategy: 'custom_test', rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', selection_only: true,
      selection_snapshot: { as_of: '2026-11-02', source_date: '2026-10-30',
        generated_at: '2026-10-30T16:01:00+08:00', rows: [{ symbol: '600001.SH' }] },
      sections: { sell: [], select: [{ symbol: '600001.SH', reference_date: '2026-10-30' }], buy: [] },
      section_errors: { buy: '目标交易日买入信号尚未计算' },
    })
    expect(host.querySelector('[aria-selected="true"]')?.textContent).toContain('选')
    expect(host.textContent).toContain('2026-10-30')
    expect(host.textContent).toContain('16:01')
    expect(host.textContent).toContain('600001.SH')
    expect(host.textContent).not.toContain('当日涨幅')
  })
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
