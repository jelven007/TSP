import { expect, it } from 'vitest'
import { groupMonitorTaskHistory } from './monitorTaskHistory'
import type { AlertEvent } from './api'

it('groups historical phases and retries by strategy, date and task while preserving raw receipts', () => {
  const event: AlertEvent = { ts: 1, source: 'system', type: 'signal_calculation_failed',
    strategy_id: 'custom_any', message: '尾盘走强 · 2026-11-19 · 上涨占比失败' }
  const rows = groupMonitorTaskHistory([
    event, { ...event, ts: 2, details: { task: 'buy', attempts: 2 } },
    { ...event, ts: 3, details: { task: 'sell' } },
    { ...event, ts: 4, message: '2026-11-20 买任务' },
    { ...event, ts: 5, strategy_id: 'custom_other' },
    { ...event, ts: 6, type: 'buy_signal', symbol: '600001.SH' },
  ])
  expect(rows).toHaveLength(5)
  const buy = rows.find(row => row.strategy_id === 'custom_any' && row.taskDate === '2026-11-19' && row.taskKind === 'buy')!
  expect(buy.ts).toBe(2)
  expect(buy.taskRecords).toEqual([expect.objectContaining({ ts: 2 }), event])
  expect(rows[0].symbol).toBe('600001.SH')
})
