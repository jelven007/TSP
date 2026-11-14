// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it } from 'vitest'
import { StrategyDailyResultToolbar } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const result: ScreenerResult = {
  as_of: '2026-11-11', strategy: 'custom_mvxxcp88', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: { sell: [], select: [], buy: [] },
}
const receipt = {
  schema: 'late_session_progress_v1', as_of: '2026-11-11', cohort: 'frozen_selection',
  observed_at: '2026-11-11T14:30:30', bar_at: '2026-11-11T14:30:00',
  params: { pool_up_min_pct: 59, change_min_pct: .4, change_max_pct: 4,
    ma5_1m_count: 7, ma5_5m_count: 4 },
  steps: {
    breadth: { state: 'success', scheduled_at: '2026-11-11T14:30:15',
      processed: 3050, total: 3050, passed: 1802, missing: 0, up_pct: 59.081967,
      started_at: '2026-11-11T14:38:08', finished_at: '2026-11-11T14:38:09', elapsed_ms: 1000 },
    change: { state: 'success', scheduled_at: '2026-11-11T14:30:15',
      processed: 216, total: 216, passed: 100, missing: 0, pool_passed: 1222 },
    ma1: { state: 'success', scheduled_at: '2026-11-11T14:30:30',
      processed: 100, total: 100, passed: 30, missing: 2 },
    ma5: { state: 'success', scheduled_at: '2026-11-11T14:30:30',
      processed: 30, total: 30, passed: 0, missing: 1 },
  },
}

async function check(sample: ScreenerResult, run: (host: HTMLDivElement, rerender: (r: ScreenerResult) => Promise<void>) => Promise<void>) {
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const rerender = async (r: ScreenerResult) => {
    await act(async () => root.render(<StrategyDailyResultToolbar result={r}
      currentDate="2026-11-11" activeTab="buy" onTabChange={() => {}} inline />))
  }
  try { await rerender(sample); await run(host, rerender) }
  finally { await act(async () => root.unmount()); host.remove() }
}
const saved = (progress: unknown): ScreenerResult => ({ ...result, entry_data_quality: {
  csgi_tail: { pool_total: 3050, known_positive: 1802, missing_symbols: [],
    pool_up_pct: 59.081967, pool_up_pct_lower: 59.081967, pool_up_pct_upper: 59.081967,
    pool_up_min_pct: 59, stage: 'confirmed', warnings: [], progress },
} } as ScreenerResult)

it('renders four real receipts after buy and distinguishes planned and actual time', async () => {
  await check(saved(receipt), async host => {
    expect(host.textContent).toContain('上涨占比 59.08% / 下限 59%')
    expect(host.textContent).toContain('涨跌幅 0.4～4%')
    expect(host.textContent).toContain('预选 100/216')
    expect(host.textContent).toContain('1分钟MA5')
    expect(host.textContent).toContain('检查 100/100 · 通过 30 · 缺数据 2')
    expect(host.textContent).toContain('5分钟MA5')
    expect(host.textContent).toContain('计划 14:30:15')
    expect(host.textContent).toContain('最近结果 14:38:08')
    expect(host.textContent).toContain('首次计算未保存')
    expect(host.textContent).not.toContain('每交易日一份结果')
    expect(host.querySelectorAll('[data-progress-step]')).toHaveLength(4)
  })
})

it('keeps first timing and counts separate from the latest result', async () => {
  await check(saved({ ...receipt, steps: { ...receipt.steps,
    breadth: { ...receipt.steps.breadth, passed: 1900,
      first_receipt: { ...receipt.steps.breadth, passed: 1802,
        started_at: '2026-11-11T14:30:43', finished_at: '2026-11-11T14:30:44' } },
  } }), async (host, rerender) => {
    const breadth = host.querySelector('[data-progress-step="breadth"]')!
    expect(breadth.textContent).toContain('首次计算 14:30:43～14:30:44')
    expect(breadth.textContent).toContain('首次上涨 1802')
    expect(breadth.textContent).toContain('最近结果 14:38:08～14:38:09')
    expect(breadth.textContent).toContain('上涨 1900')
    await rerender(saved(receipt))
    expect(breadth.textContent).toContain('首次计算未保存')
    expect(breadth.textContent).not.toContain('首次计算 14:38:08')
  })
})

it('shows old history as unsaved and current missing receipt as unexecuted', async () => {
  await check(result, async (host, rerender) => {
    expect(host.textContent).toContain('未执行 / 等待状态回执')
    await rerender({ ...result, as_of: '2026-11-10' })
    expect(host.textContent).toContain('未保存阶段进展')
    expect(host.textContent).not.toContain('14:30:15')
  })
})

it('uses rejection reason and unknown counts for stages not run', async () => {
  await check(saved({ ...receipt, steps: { ...receipt.steps,
    breadth: { ...receipt.steps.breadth, state: 'rejected' },
    ma1: { state: 'skipped', scheduled_at: '2026-11-11T14:30:30',
      processed: null, total: null, passed: null, missing: null, reason: '上涨占比未达下限' },
  } }), async host => {
    const ma = host.querySelector('[data-progress-step="ma1"]')!
    expect(ma.textContent).toContain('跳过')
    expect(ma.textContent).toContain('上涨占比未达下限')
    expect(ma.textContent).not.toContain('检查 0')
  })
})

it('updates from existing result props and labels a retained receipt after execution failure', async () => {
  await check(result, async (host, rerender) => {
    await rerender({ ...saved(receipt), showing_last_success: true,
      execution_status: { state: 'failed', reason: '公共行情不可读', updated_at: 1 } })
    expect(host.textContent).toContain('上次成功回执')
    expect(host.textContent).toContain('公共行情不可读')
    expect(host.textContent).toContain('1分钟MA5')
  })
})

it('ignores a mismatched date receipt and preserves other strategy toolbar', async () => {
  await check(saved({ ...receipt, as_of: '2026-11-10' }), async (host, rerender) => {
    expect(host.textContent).toContain('未执行 / 等待状态回执')
    await rerender({ ...result, strategy: 'other' })
    expect(host.textContent).toContain('每交易日一份结果')
    expect(host.querySelectorAll('[data-progress-step]')).toHaveLength(0)
  })
})
