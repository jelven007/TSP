// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { accountEventStreamUrl } from './accountSession'
import { useQuoteStream } from './useQuoteStream'
import { QK } from './queryKeys'

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

function Harness({ pages }: { pages?: Record<string, boolean> }) {
  useQuoteStream(true, pages)
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
  it.each([undefined, { 'watchlist-enriched': true }])(
    'finishes a slow watchlist refresh before starting another for pages %s',
    async (pages) => {
      await act(async () => {
        root?.render(
          <QueryClientProvider client={client}>
            <Harness pages={pages} />
          </QueryClientProvider>,
        )
      })
      const resolvers: Array<(value: { rows: string[] }) => void> = []
      // The API client owns its AbortController; cancelling a query cannot
      // cancel this pending HTTP transport. Match that contract here.
      const fetchRows = vi.fn(() => new Promise<{ rows: string[] }>(resolve => {
        resolvers.push(resolve)
      }))
      const observer = new QueryObserver(client, {
        queryKey: ['watchlist-enriched', ''],
        queryFn: fetchRows,
        initialData: { rows: ['saved'] },
        staleTime: Infinity,
      })
      const unsubscribe = observer.subscribe(() => {})
      try {
        const stream = MockEventSource.instances[0]
        await act(async () => { stream.emit('quotes_updated') })
        expect(fetchRows).toHaveBeenCalledTimes(1)
        await act(async () => {
          stream.emit('quotes_updated')
          stream.emit('quotes_updated')
        })
        expect(fetchRows).toHaveBeenCalledTimes(1)
        expect(observer.getCurrentResult().data?.rows).toEqual(['saved'])
        await act(async () => { resolvers[0]({ rows: ['fresh'] }) })
        expect(observer.getCurrentResult().data?.rows).toEqual(['fresh'])
        expect(observer.getCurrentResult().fetchStatus).toBe('idle')
        await act(async () => { stream.emit('quotes_updated') })
        expect(fetchRows).toHaveBeenCalledTimes(2)
        await act(async () => { resolvers[1]({ rows: ['latest'] }) })
        expect(observer.getCurrentResult().data?.rows).toEqual(['latest'])
      } finally {
        unsubscribe()
        for (const resolve of resolvers) resolve({ rows: ['cleanup'] })
      }
    },
  )

  it('removes deleted paper account caches and updates the shared comparison', async () => {
    client.setQueryData(QK.paperOverview('deleted'), { initialized: true })
    client.setQueryData(QK.paperOverview('kept'), { initialized: true })
    client.setQueryData(QK.paperCompare, { accounts: [{ account: 'deleted' }, { account: 'kept' }] })
    await act(async () => {
      MockEventSource.instances[0].emit('paper_changed', '{"account_id":"deleted","action":"deleted"}')
      await Promise.resolve()
    })
    expect(client.getQueryData(QK.paperOverview('deleted'))).toBeUndefined()
    expect(client.getQueryData(QK.paperOverview('kept'))).toEqual({ initialized: true })
    expect(client.getQueryData(QK.paperCompare)).toEqual({ accounts: [{ account: 'kept' }] })
  })

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
    expect(predicate({ queryKey: QK.paperCompare })).toBe(true)
    expect(predicate({ queryKey: ['screener-cached', 'summary'] })).toBe(true)
    expect(predicate({ queryKey: ['review-reports'] })).toBe(true)
    expect(predicate({ queryKey: ['settings'] })).toBe(false)
    expect(predicate({ queryKey: ['overview-market', 'latest'] })).toBe(false)
  })
})
