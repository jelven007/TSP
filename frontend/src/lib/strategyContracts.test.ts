// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api, type CustomSignal } from './api'
import * as customSignals from './customSignals'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const signal = (kind: 'entry' | 'exit', params: CustomSignal['params']): CustomSignal => ({
  id: `${kind}_s01`, name: kind, kind, enabled: true, timeframe: 'intraday',
  template: kind === 'entry' ? 's01_late_entry' : 's01_morning_exit', conditions: [], params,
})

it('reads saved S01 parameters without inventing removed exit thresholds or modifying the definitions', () => {
  const entry = signal('entry', {
    current_amount_min: 1e8, current_return_min: .02, current_return_max: .03,
    current_range_max: .0525, market_positive_min: .65,
  })
  const exit = signal('exit', {
    decline_3m: .004, volume_ratio_min: 1.7,
    start_time: '11:15:00', stop_time: '11:26:00', fallback_time: '13:30:00',
  })
  const before = structuredClone({ entry, exit })
  expect(typeof customSignals.parameterSignalValues).toBe('function')
  expect(customSignals.parameterSignalValues({ entry, exit })).toEqual({
    ...entry.params, ...exit.params,
  })
  expect(customSignals.parameterSignalValues({ entry, exit })).not.toHaveProperty('vwap_buffer')
  expect({ entry, exit }).toEqual(before)
  expect(customSignals.parameterSignalValues()).toEqual({})
  expect(customSignals.parameterSignalValues({ exit })).toEqual(exit.params)
})

it('loads the saved strategy pool and revision through the existing endpoint', async () => {
  const saved = { ids: ['custom_mvxxcs01'], revision: 2 }
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(saved), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  expect(typeof api.strategyPool).toBe('function')
  await expect(api.strategyPool()).resolves.toEqual(saved)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/strategies/pool')
  expect(fetchMock.mock.calls[0][1]?.method ?? 'GET').toBe('GET')
})

it.each([
  { ids: [], revision: 2 },
  { ids: ['custom_mvxxcs01'], revision: 0, migrate: true },
])('preserves empty or migration pool requests with their revision: %j', async body => {
  const saved = { ids: body.ids, revision: body.revision + 1 }
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(saved), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  expect(typeof api.strategyPoolSave).toBe('function')
  await expect(api.strategyPoolSave(body)).resolves.toEqual(saved)
  expect(fetchMock).toHaveBeenCalledWith('/api/strategies/pool', expect.objectContaining({
    method: 'PUT', body: JSON.stringify(body),
  }))
})

it('propagates a strategy pool revision conflict to the caller', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    detail: '策略池已在其他页面更新',
  }), { status: 409, headers: { 'Content-Type': 'application/json' } })))
  expect(typeof api.strategyPoolSave).toBe('function')
  await expect(api.strategyPoolSave({ ids: [], revision: 2 })).rejects.toMatchObject({
    status: 409, message: '策略池已在其他页面更新',
  })
})
