// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ScheduleOverview } from './ScheduleOverview'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let host: HTMLDivElement
let root: Root
const props = {
  financialAvailable: true,
  depthAvailable: true,
  extConfigs: [{
    id: 'concept', label: '扩展概念',
    pull: { url: 'https://example.test/concepts', enabled: true, schedule_minutes: 1 },
  }],
  onAutoSyncChange: vi.fn(),
  autoSyncDisabled: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

it('keeps category disclosures independent and preserves expansion when live schedules refresh', async () => {
  await act(async () => root.render(<ScheduleOverview {...props} />))
  const groups = [...host.querySelectorAll<HTMLDetailsElement>('details')]
  expect(groups).toHaveLength(5)
  expect(groups.map(group => group.querySelector('summary')?.textContent)).toEqual([
    expect.stringContaining('基础与完整性'),
    expect.stringContaining('实时行情与快照'),
    expect.stringContaining('K线采集与加工'),
    expect.stringContaining('财务与能力保障'),
    expect.stringContaining('扩展数据'),
  ])
  expect(groups.every(group => !group.open)).toBe(true)
  expect(groups[4].textContent).toContain('扩展数据 · 扩展概念')

  await act(async () => groups[0].querySelector('summary')?.click())
  expect(groups[0].open).toBe(true)
  expect(groups.slice(1).every(group => !group.open)).toBe(true)
  await act(async () => groups[4].querySelector('summary')?.click())
  expect(groups[4].open).toBe(true)
  expect(groups[0].open).toBe(true)

  await act(async () => root.render(<ScheduleOverview
    {...props}
    quoteStatus={{ running: true, is_trading_hours: true, interval_s: 1 }}
  />))
  expect(host.querySelector('details')).toBe(groups[0])
  expect(groups[0].open).toBe(true)
  expect(groups[1].open).toBe(false)
  expect(groups[1].textContent).toContain('每 1 秒')
  expect(groups[4].open).toBe(true)

  await act(async () => groups[0].querySelector('summary')?.click())
  expect(groups[0].open).toBe(false)
  expect(groups[4].open).toBe(true)
  expect(props.onAutoSyncChange).not.toHaveBeenCalled()
})

it('retains the accessible sync switch and its saved and pending states without a visible label', async () => {
  await act(async () => root.render(<ScheduleOverview {...props} />))
  const toggle = host.querySelector<HTMLButtonElement>('button[role="switch"]')!
  expect(toggle.getAttribute('aria-label')).toBe('自动同步')
  expect(toggle.getAttribute('aria-checked')).toBe('true')
  expect(host.querySelector('section > div')?.textContent).not.toContain('自动同步总开关')
  await act(async () => toggle.click())
  expect(props.onAutoSyncChange).toHaveBeenLastCalledWith(false)

  await act(async () => root.render(<ScheduleOverview
    {...props} preferences={{ data_sync_auto_enabled: false }} autoSyncDisabled
  />))
  expect(toggle.getAttribute('aria-checked')).toBe('false')
  expect(toggle.disabled).toBe(true)
  await act(async () => toggle.click())
  expect(props.onAutoSyncChange).toHaveBeenCalledTimes(1)

  await act(async () => root.render(<ScheduleOverview
    {...props} preferences={{ data_sync_auto_enabled: false }}
  />))
  await act(async () => toggle.click())
  expect(props.onAutoSyncChange).toHaveBeenLastCalledWith(true)
})
