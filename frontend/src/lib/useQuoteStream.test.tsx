// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { accountEventStreamUrl } from './accountSession'
import { useQuoteStream } from './useQuoteStream'

vi.mock('./accountSession', () => ({
  accountScope: () => 'tsp-v1:admin:market:1',
  subscribeAccountScopeChanges: () => () => {},
  accountEventStreamUrl: vi.fn(async () => '/api/events?ticket=test'),
}))
vi.mock('./useQueryConfig', () => ({
  getQueryConfig: () => ({ sse: { reconnectDelay: 100 } }),
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('@/components/AlertToast', () => ({ pushAlertToasts: vi.fn() }))
vi.mock('./reviewStore', () => ({ feedReviewEvent: vi.fn() }))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

type EventHandler = (event: MessageEvent) => void

class MockEventSource {
  static instances: MockEventSource[] = []

  readonly listeners = new Map<string, EventHandler[]>()
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(readonly url: string) {
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, handler: EventHandler) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler])
  }

  close() {
    this.closed = true
  }

  emit(type: string, data = '{}') {
    const event = new MessageEvent(type, { data })
    for (const handler of this.listeners.get(type) ?? []) handler(event)
  }
}

function Harness() {
  useQuoteStream(true, undefined)
  return null
}

let root: Root | null = null
let client: QueryClient

beforeEach(async () => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    )
  })
  await act(async () => Promise.resolve())
})

afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  root = null
  client.clear()
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('global event stream refresh scope', () => {
  it('reconnects an expired stream ticket without refreshing page queries', async () => {
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const first = MockEventSource.instances[0]

    expect(first).toBeDefined()
    first.emit('access_revoked')
    expect(first.closed).toBe(true)
    expect(invalidate).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(accountEventStreamUrl).toHaveBeenCalledTimes(2)
    expect(MockEventSource.instances).toHaveLength(2)
  })

  it('refreshes only event-driven query families after an event backlog reset', () => {
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    MockEventSource.instances[0].emit('events_reset')

    expect(invalidate).toHaveBeenCalledTimes(1)
    const options = invalidate.mock.calls[0][0]
    expect(options).toHaveProperty('predicate')
    const predicate = options?.predicate as (query: { queryKey: readonly unknown[] }) => boolean
    expect(predicate({ queryKey: ['alerts'] })).toBe(true)
    expect(predicate({ queryKey: ['alerts-total'] })).toBe(true)
    expect(predicate({ queryKey: ['screener-cached', 'summary'] })).toBe(true)
    expect(predicate({ queryKey: ['review-reports'] })).toBe(true)
    expect(predicate({ queryKey: ['settings'] })).toBe(false)
    expect(predicate({ queryKey: ['overview-market', 'latest'] })).toBe(false)
  })
})
