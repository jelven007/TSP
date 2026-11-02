// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { attachBacktestJob, useBacktestTask, type BacktestTask } from './backtestTask'

vi.mock('./accountSession', () => ({
  accountFetch: vi.fn(async () => ({ ok: true, body: { cancel: async () => {} } })),
  accountStorage: { setItem: vi.fn(), removeItem: vi.fn(), getItem: vi.fn() },
  accountStreamUrl: (url: string) => url,
}))

it('restores a rejected cash ledger from the terminal error event', async () => {
  const handlers = new Map<string, (event: { data: string }) => void>()
  class Stream {
    onopen = null
    addEventListener(type: string, handler: (event: { data: string }) => void) { handlers.set(type, handler) }
    close() {}
  }
  vi.stubGlobal('EventSource', Stream)
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  let snapshot: BacktestTask | null = null
  function Observer() { snapshot = useBacktestTask(); return null }
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => { root.render(<Observer />); attachBacktestJob('failed-job') })
    const result = { error: '截止余股', fills: [{ shares: 4600 }],
      stats: { minute_execution: 'generic_morning_liquidation_resume_v2',
        execution: { remaining_positions: [{ symbol: '600373.SH', remaining: 700 }] } } }
    await act(async () => handlers.get('error')!({ data: JSON.stringify({ message: result.error, result }) }))
    expect(snapshot).toMatchObject({ isPending: false, error: '截止余股', result })
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
