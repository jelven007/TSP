// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { PostCloseIntegritySummary } from './PostCloseIntegritySummary'
import { ScheduleOverview } from './ScheduleOverview'
import type { PostCloseIntegrityStatus } from '@/lib/api'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let host: HTMLDivElement
let root: Root
const base: PostCloseIntegrityStatus = {
  available: true, enabled: true, scheduled_time: '15:45', timezone: 'Asia/Shanghai',
  today: '2026-11-11', day: '2026-11-11', is_trading_day: true, status: 'not_run',
}
beforeEach(() => {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

it('shows fixed schedule, actual success diagnostics and Beijing retry times', async () => {
  await act(async () => root.render(<PostCloseIntegritySummary enabled status={{
    ...base, status: 'succeeded', attempts: 1,
    result: { repaired_symbols: ['000001.SZ'], gap_check: { rows: 2, remaining: 0 }, data_release_id: 'r' },
  }} />))
  expect(host.textContent).toContain('交易日 15:45（北京时间）')
  expect(host.textContent).toContain('已完成')
  expect(host.textContent).toContain('2026-11-11')
  expect(host.textContent).toContain('修复日线/收盘证据 1 只')
  await act(async () => root.render(<PostCloseIntegritySummary enabled status={{
    ...base, status: 'failed', attempts: 2, error: '源端快照时间不足',
    next_retry_at: '2026-11-11T07:46:00Z',
    result: { gap_check: { remaining: 3 }, data_release_id: 'r' },
  }} />))
  expect(host.textContent).toContain('仍有异常')
  expect(host.textContent).toContain('源端快照时间不足')
  expect(host.textContent).toContain('15:46:00')
  expect(host.textContent).toContain('剩余缺口 3')
  expect(host.textContent).not.toContain('已完成')
})

it('distinguishes pause, unavailable, holiday, cancelled and prior-day results', async () => {
  for (const [status, enabled, expected] of [
    [{ ...base, status: 'succeeded' }, false, '已暂停'],
    [{ ...base, available: false, status: 'unavailable' }, true, '状态不可用'],
    [{ ...base, is_trading_day: false }, true, '非交易日'],
    [{ ...base, status: 'cancelled' }, true, '已取消'],
    [{ ...base, day: '2026-11-10', status: 'succeeded' }, true, '当日尚未执行'],
  ] as const) {
    await act(async () => root.render(<PostCloseIntegritySummary enabled={enabled} status={status} />))
    expect(host.querySelector('[role="status"]')?.textContent).toBe(expected)
  }
  expect(host.textContent).toContain('最近记录 2026-11-10 · 已完成')
  expect(host.querySelector('[role="status"]')?.textContent).not.toContain('已完成')
  await act(async () => root.render(<PostCloseIntegritySummary enabled unavailable />))
  expect(host.textContent).toContain('状态不可用')
})

it('shows the previous failure and actual timestamps alongside current-day status', async () => {
  await act(async () => root.render(<PostCloseIntegritySummary enabled status={{
    ...base, day: '2026-11-10', status: 'failed', attempts: 92,
    last_started_at: '2026-11-10T07:45:02Z', last_finished_at: '2026-11-10T07:47:06Z',
    diagnostics: { close_review: {
      before: { checked_count: 3, valid_count: 1, invalid_count: 2 },
      after: { checked_count: 3, valid_count: 2, invalid_count: 1 },
    } },
  }} />))
  expect(host.querySelector('[role="status"]')?.textContent).toBe('当日尚未执行')
  expect(host.textContent).toContain('最近记录 2026-11-10 · 仍有异常')
  expect(host.textContent).toContain('已尝试 92 次')
  expect(host.textContent).toContain('最近实际开始 15:45:02 · 结束 15:47:06')
  expect(host.textContent).toContain('修复后复核 3 只 · 有效 2 只 · 异常 1 只 · 修复前异常 2 只')
})

it('renders inside post-close management and preserves category expansion across updates', async () => {
  const props = { financialAvailable: true, depthAvailable: true, extConfigs: [],
    onAutoSyncChange: () => {}, autoSyncDisabled: false }
  await act(async () => root.render(<ScheduleOverview {...props} postCloseIntegrity={base} />))
  const section = host.querySelector('[aria-label="当日遗留复核与修复"]')!
  expect(section.parentElement?.textContent).toContain('盘后数据管理')
  const group = section.closest('details')!
  await act(async () => group.querySelector('summary')?.click())
  expect(group.open).toBe(true)
  await act(async () => root.render(<ScheduleOverview {...props}
    preferences={{ data_sync_auto_enabled: false }} postCloseIntegrity={{ ...base, status: 'running' }} />))
  expect(section.closest('details')).toBe(group)
  expect(group.open).toBe(true)
  expect(section.querySelector('[role="status"]')?.textContent).toBe('已暂停')
})
