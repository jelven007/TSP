// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { S01SignalParamsEditor } from './S01SignalParamsEditor'

vi.mock('@/components/TimePicker', () => ({
  TimePicker: ({ value, onChange, showSeconds, ...props }: any) =>
    <input aria-label={props['aria-label']} value={value} data-seconds={String(showSeconds)}
      onChange={e => onChange(e.target.value)} />,
}))
afterEach(() => vi.restoreAllMocks())

it('shows three explicit clocks and saves a whole remaining-position stop with seconds intact', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  const root = createRoot(host)
  const changed = vi.fn()
  const params = { decline_3m: .002, volume_ratio_min: 1.5,
    start_time: '11:15:00', stop_time: '11:26:00', fallback_time: '13:30:00' }
  try {
    await act(async () => root.render(<S01SignalParamsEditor signal={{
      id: 'morning_liquidation_s01', name: 's01_早盘清仓', kind: 'exit', enabled: true,
      timeframe: 'intraday', template: 's01_morning_exit', conditions: [], params: params as any,
    }} onChange={changed} />))
    for (const [label, clock] of [['启动时间', '11:15:00'], ['止损时间', '11:26:00'], ['兜底时间', '13:30:00']]) {
      const input = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)
      expect(input?.value).toBe(clock)
      expect(input?.dataset.seconds).toBe('true')
    }
    expect(host.textContent).toContain('无条件全部清仓')
    expect(host.querySelectorAll('input[type="number"]')).toHaveLength(2)
    expect(host.textContent).toContain('同时满足')
    expect(host.textContent).not.toContain('均价偏离')
    const stop = host.querySelector<HTMLInputElement>('input[aria-label="止损时间"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(stop, '11:27:00')
      stop.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(changed).toHaveBeenCalledWith({ ...params, stop_time: '11:27:00' })
  } finally { await act(async () => root.unmount()) }
})
