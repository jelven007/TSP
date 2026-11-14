// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it } from 'vitest'
import { SignalPicker } from './SignalPicker'
import { QK } from '@/lib/queryKeys'

it('offers the minute entry template only to supported consumers and uses its minute column', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient()
  qc.setQueryData(QK.customSignals, { signals: [{
    id: 'late_session_strength', name: '尾盘走强', kind: 'entry', enabled: true,
    timeframe: 'intraday', template: 'late_session_strength', conditions: [],
  }] })
  const host = document.createElement('div')
  const root = createRoot(host)
  let selected: string[] = []
  const render = (allowIntraday: boolean) => root.render(<QueryClientProvider client={qc}>
    <SignalPicker signals={[]} kind="entry" onChange={value => { selected = value }}
      options={{ builtinSignals: [], allowIntraday }} />
  </QueryClientProvider>)
  await act(async () => render(false))
  expect(host.textContent).not.toContain('尾盘走强')
  await act(async () => render(true))
  expect(host.textContent).toContain('尾盘走强')
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(selected).toEqual(['csgi_late_session_strength'])
  await act(async () => root.unmount())
})

it('offers holding exits to minute strategy settings and hides them in backtests and raw monitors', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient()
  qc.setQueryData(QK.customSignals, { signals: [{
    id: 'morning_liquidation', name: '早盘清仓', kind: 'exit', enabled: true,
    timeframe: 'intraday', template: 'morning_liquidation', conditions: [],
  }] })
  const host = document.createElement('div')
  const root = createRoot(host)
  const render = (options: any) => root.render(<QueryClientProvider client={qc}>
    <SignalPicker signals={[]} kind="exit" onChange={() => {}} options={{ builtinSignals: [], allowIntraday: true, ...options }} />
  </QueryClientProvider>)
  await act(async () => render({ allowHoldingExit: false }))
  expect(host.textContent).not.toContain('早盘清仓')
  await act(async () => render({ filterCustomByKind: false }))
  expect(host.textContent).not.toContain('早盘清仓')
  await act(async () => render({}))
  expect(host.textContent).toContain('早盘清仓')
  await act(async () => root.unmount())
})

it('keeps a configured holding exit visible with its backtest limitation', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient()
  qc.setQueryData(QK.customSignals, { signals: [{
    id: 'morning_liquidation', name: '早盘清仓', kind: 'exit', enabled: true,
    timeframe: 'intraday', template: 'morning_liquidation', conditions: [],
  }] })
  const host = document.createElement('div')
  const root = createRoot(host)
  let changed = false
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <SignalPicker signals={['csgi_morning_liquidation']} kind="exit" onChange={() => { changed = true }}
      options={{ builtinSignals: [], allowIntraday: true, allowHoldingExit: false }} />
  </QueryClientProvider>))
  try {
    const button = host.querySelector<HTMLButtonElement>('button')
    expect(host.textContent).toContain('早盘清仓')
    expect(button?.textContent).toContain('回测暂不支持')
    expect(button?.disabled).toBe(true)
    expect(changed).toBe(false)
  } finally {
    await act(async () => root.unmount())
    qc.clear()
  }
})

it('adds S01 templates to the ordinary choices and supports multiple selection and clearing', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient()
  qc.setQueryData(QK.customSignals, { signals: [
    { id: 'entry_s01', name: '尾盘建仓_s01', kind: 'entry', enabled: true,
      timeframe: 'intraday', template: 's01_late_entry', conditions: [] },
    { id: 'entry_s02', name: '尾盘建仓_s02', kind: 'entry', enabled: true,
      timeframe: 'intraday', template: 'late_session_strength', conditions: [] },
  ] })
  const host = document.createElement('div')
  const root = createRoot(host)
  let selected: string[] = []
  const render = (s01: boolean) => root.render(<QueryClientProvider client={qc}>
    <SignalPicker signals={['csgi_entry_s01']} kind="entry" onChange={value => { selected = value }}
      options={{ allowIntraday: true, ...(s01 ? { s01Template: 's01_late_entry' } : {}) } as any} />
  </QueryClientProvider>)
  try {
    await act(async () => render(false))
    expect(host.textContent).not.toContain('尾盘建仓_s01')
    await act(async () => render(true))
    expect(host.textContent).toContain('尾盘建仓_s01')
    expect(host.textContent).toContain('尾盘建仓_s02')
    expect(host.textContent).toContain('MACD金叉')
    const button = [...host.querySelectorAll<HTMLButtonElement>('button')]
      .find(b => b.textContent?.includes('尾盘建仓_s01'))!
    expect(button.getAttribute('aria-pressed')).toBe('true')
    const other = [...host.querySelectorAll<HTMLButtonElement>('button')]
      .find(b => b.textContent?.includes('尾盘建仓_s02'))!
    await act(async () => other.click())
    expect(selected).toEqual(['csgi_entry_s01', 'csgi_entry_s02'])
    await act(async () => button.click())
    expect(selected).toEqual([])
  } finally {
    await act(async () => root.unmount())
    qc.clear()
  }
})
