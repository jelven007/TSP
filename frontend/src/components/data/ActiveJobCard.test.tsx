// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { ActiveJobCard } from './ActiveJobCard'
import { HistoryRow } from './SectionTitle'
import type { PipelineJob } from '@/lib/api'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})
const job = (changes: Partial<PipelineJob> = {}): PipelineJob => ({
  id: 'history-5m', kind: 'kline_5m', status: 'running', stage: 'sync_5m',
  progress: 37, stage_pct: 33, log: [{ ts: '2026-10-26T02:00:00Z', stage: 'sync_5m', msg: '写入 100/250 标的' }],
  started_at: null, finished_at: null, duration_s: null, result: null, error: null, ...changes,
})

it('shows the correct period and both total and stage progress', async () => {
  await act(async () => root.render(<ActiveJobCard job={job()} />))
  expect(host.textContent).toContain('同步5分钟K')
  expect(host.textContent).not.toContain('sync_5m')
  expect(host.querySelector('[role="progressbar"][aria-label="同步总进度"]')?.getAttribute('aria-valuenow')).toBe('37')
  expect(host.querySelector('[role="progressbar"][aria-label="当前阶段进度"]')?.getAttribute('aria-valuenow')).toBe('33')
})

it('shows completion and actual period rows rather than unrelated daily pipeline counts', async () => {
  const result: PipelineJob['result'] = { universe_size: 250, period: '5m', rows: 12000, start: '2026-09-23', end: '2026-10-23' }
  await act(async () => root.render(<ActiveJobCard job={job({ status: 'succeeded', progress: 100, stage: 'done', result })} />))
  expect(host.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('100')
  expect(host.textContent).toContain('5分钟K')
  expect(host.textContent).toContain('12000')
  expect(host.textContent).not.toContain('0 天')
})

it('shows the period and rows in sync history', async () => {
  const result: PipelineJob['result'] = { universe_size: 250, period: '1w', rows: 250, start: '2026-09-23', end: '2026-10-23' }
  await act(async () => root.render(<HistoryRow job={job({ status: 'succeeded', result })} onClick={() => {}} />))
  expect(host.textContent).toContain('周K')
  expect(host.textContent).toContain('250行')
})

it.each(['failed', 'cancelled', 'interrupted'] as const)('retains actual progress for %s jobs', async (status) => {
  await act(async () => root.render(<ActiveJobCard job={job({ status })} />))
  expect(host.querySelector('[aria-label="同步总进度"]')?.getAttribute('aria-valuenow')).toBe('37')
  expect(host.querySelector('[aria-label="当前阶段进度"]')).toBeNull()
})

it('renders an old successful minute job without requiring new result fields', async () => {
  await act(async () => root.render(<ActiveJobCard job={job({
    kind: 'minute_sync', status: 'succeeded', progress: 95,
    result: { minute_rows: 240, universe_size: 1 },
  })} />))
  expect(host.querySelector('[aria-label="同步总进度"]')?.getAttribute('aria-valuenow')).toBe('100')
  expect(host.textContent).toContain('1分钟K')
  expect(host.textContent).toContain('240')
})
