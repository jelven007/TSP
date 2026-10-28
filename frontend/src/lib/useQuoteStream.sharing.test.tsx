// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { accountEventStreamUrl } from './accountSession'
import { createSharedQuoteStream } from './sharedQuoteStream'
import { useQuoteStream } from './useQuoteStream'

const session = vi.hoisted(() => ({
  scope: 'tsp-v1:admin:market:1',
  listeners: new Set<() => void>(),
}))
vi.mock('./accountSession', () => ({
  accountScope: () => session.scope,
  subscribeAccountScopeChanges: (listener: () => void) => {
    session.listeners.add(listener)
    return () => session.listeners.delete(listener)
  },
  accountEventStreamUrl: vi.fn(async (cursor = '') => `/api/events?ticket=test&last_event_id=${cursor}`),
}))
vi.mock('./useQueryConfig', () => ({
  getQueryConfig: () => ({ sse: { reconnectDelay: 100 } }),
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('@/components/AlertToast', () => ({ pushAlertToasts: vi.fn() }))
vi.mock('./reviewStore', () => ({ feedReviewEvent: vi.fn() }))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

class TestChannel {
  static channels = new Set<TestChannel>()
  onmessage: ((event: MessageEvent) => void) | null = null
  constructor(readonly name: string) { TestChannel.channels.add(this) }
  postMessage(data: unknown) {
    for (const peer of TestChannel.channels) {
      if (peer !== this && peer.name === this.name) {
        queueMicrotask(() => {
          if (TestChannel.channels.has(peer)) peer.onmessage?.(new MessageEvent('message', { data }))
        })
      }
    }
  }
  close() { TestChannel.channels.delete(this) }
}

function testLocks() {
  const tails = new Map<string, Promise<void>>()
  return {
    request(name: string, options: { signal: AbortSignal }, callback: () => Promise<void>) {
      const previous = tails.get(name) ?? Promise.resolve()
      let release!: () => void
      const gate = new Promise<void>(resolve => { release = resolve })
      tails.set(name, previous.then(() => gate))
      return new Promise<void>((resolve, reject) => {
        const abort = () => { release(); reject(new DOMException('Cancelled', 'AbortError')) }
        options.signal.addEventListener('abort', abort, { once: true })
        void previous.then(async () => {
          if (options.signal.aborted) return
          options.signal.removeEventListener('abort', abort)
          try { await callback(); resolve() } catch (error) { reject(error) } finally { release() }
        })
      })
    },
  }
}

class TestEventSource {
  static instances: TestEventSource[] = []
  listeners = new Map<string, Array<(event: MessageEvent) => void>>()
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  constructor(readonly url: string) { TestEventSource.instances.push(this) }
  addEventListener(type: string, handler: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler])
  }
  close() { this.closed = true }
  emit(type: string, data = '{}', lastEventId = '') {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(new MessageEvent(type, { data, lastEventId }))
    }
  }
}

const tabs: Array<{ root: Root; client: QueryClient }> = []
function Harness() { useQuoteStream(true, undefined); return null }

async function mountTab() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  tabs.push({ root, client })
  await act(async () => {
    root.render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  })
  return client
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  session.scope = 'tsp-v1:admin:market:1'
  TestEventSource.instances = []
  vi.stubGlobal('EventSource', TestEventSource)
  vi.stubGlobal('BroadcastChannel', TestChannel)
  vi.stubGlobal('navigator', { locks: testLocks() })
})

afterEach(async () => {
  for (const tab of tabs.splice(0)) {
    await act(async () => tab.root.unmount())
    tab.client.clear()
  }
  expect(TestChannel.channels.size).toBe(0)
  expect(session.listeners.size).toBe(0)
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('keeps six pages on one authenticated stream so REST retains browser connections', async () => {
  for (let index = 0; index < 6; index++) await mountTab()
  expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
  expect(accountEventStreamUrl).toHaveBeenCalledTimes(1)
})

it('delivers updates to every page and resumes from its cursor when the owner closes', async () => {
  for (let index = 0; index < 3; index++) await mountTab()
  const invalidations = tabs.map(tab => vi.spyOn(tab.client, 'invalidateQueries'))
  const first = TestEventSource.instances[0]
  await act(async () => {
    first.onopen?.()
    first.emit('strategy_results_updated', '{}', '42')
  })
  for (const invalidate of invalidations) {
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['screener-cached'] })
  }
  const owner = tabs.shift()!
  await act(async () => owner.root.unmount())
  owner.client.clear()
  expect(first.closed).toBe(true)
  expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
  expect(accountEventStreamUrl).toHaveBeenLastCalledWith('42')
})

it('rotates a shared ticket without resetting queries or multiplying streams', async () => {
  for (let index = 0; index < 3; index++) await mountTab()
  const invalidations = tabs.map(tab => vi.spyOn(tab.client, 'invalidateQueries'))
  const first = TestEventSource.instances[0]
  await act(async () => first.emit('review_progress', '{"type":"delta"}', '9'))
  await act(async () => {
    first.emit('access_revoked')
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(first.closed).toBe(true)
  expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
  expect(accountEventStreamUrl).toHaveBeenCalledTimes(2)
  expect(accountEventStreamUrl).toHaveBeenLastCalledWith('9')
  for (const invalidate of invalidations) expect(invalidate).not.toHaveBeenCalled()
})

it('preserves direct connections when browser sharing APIs are unavailable', async () => {
  vi.stubGlobal('navigator', {})
  await mountTab()
  expect(TestEventSource.instances).toHaveLength(1)
  expect(accountEventStreamUrl).toHaveBeenCalledTimes(1)
})

it('rejects messages from another account scope and ignores events after an account switch', async () => {
  const client = await mountTab()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const first = TestEventSource.instances[0]
  const name = [...TestChannel.channels][0].name
  const other = new TestChannel(name)
  await act(async () => other.postMessage({
    scope: 'tsp-v1:other:personal:1', kind: 'event',
    type: 'strategy_results_updated', data: '{}', lastEventId: '99',
  }))
  other.close()
  expect(invalidate).not.toHaveBeenCalled()
  await act(async () => {
    session.scope = 'tsp-v1:other:personal:1'
    for (const listener of session.listeners) listener()
    first.emit('strategy_results_updated', '{}', '100')
    first.onerror?.()
    await vi.advanceTimersByTimeAsync(1_000)
  })
  expect(first.closed).toBe(true)
  expect(invalidate).not.toHaveBeenCalled()
  expect(accountEventStreamUrl).toHaveBeenCalledTimes(1)
})

it('discards a ticket response that arrives after its page has closed', async () => {
  let resolve!: (url: string) => void
  vi.mocked(accountEventStreamUrl).mockImplementationOnce(() => new Promise<string>(done => { resolve = done }))
  await mountTab()
  const tab = tabs.shift()!
  await act(async () => {
    tab.root.unmount()
    resolve('/api/events?ticket=late')
  })
  tab.client.clear()
  expect(TestEventSource.instances).toHaveLength(0)
})

it('allows another page to take over before an abandoned ticket request returns', async () => {
  let resolve!: (url: string) => void
  vi.mocked(accountEventStreamUrl).mockImplementationOnce(() => new Promise<string>(done => { resolve = done }))
  await mountTab()
  await mountTab()
  const owner = tabs.shift()!
  try {
    await act(async () => owner.root.unmount())
    owner.client.clear()
    expect(accountEventStreamUrl).toHaveBeenCalledTimes(2)
    expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
  } finally {
    await act(async () => resolve('/api/events?ticket=abandoned'))
  }
  expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
})

it('keeps a late follower at the current replay cursor during takeover', async () => {
  const owner = createSharedQuoteStream()
  await act(async () => {})
  const first = TestEventSource.instances[0]
  first.onopen?.()
  first.emit('strategy_results_updated', '{}', '73')
  const follower = createSharedQuoteStream()
  try {
    await act(async () => {})
    await act(async () => owner.close())
    expect(accountEventStreamUrl).toHaveBeenLastCalledWith('73')
    expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
  } finally {
    owner.close()
    follower.close()
  }
})

it('shares retry attempts after a connection error and resumes with a fresh ticket', async () => {
  for (let index = 0; index < 3; index++) await mountTab()
  const first = TestEventSource.instances[0]
  await act(async () => first.emit('strategy_results_updated', '{}', '15'))
  await act(async () => {
    first.onerror?.()
    await vi.advanceTimersByTimeAsync(99)
  })
  expect(first.closed).toBe(true)
  expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(0)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(accountEventStreamUrl).toHaveBeenCalledTimes(2)
  expect(accountEventStreamUrl).toHaveBeenLastCalledWith('15')
  expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(1)
})

it('retains the shared cursor when a late page takes over and then rotates its ticket', async () => {
  await mountTab()
  const first = TestEventSource.instances[0]
  await act(async () => {
    first.onopen?.()
    first.emit('strategy_results_updated', '{}', '88')
  })
  await mountTab()
  const owner = tabs.shift()!
  await act(async () => owner.root.unmount())
  owner.client.clear()
  const next = TestEventSource.instances.at(-1)!
  await act(async () => {
    next.emit('access_revoked')
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(accountEventStreamUrl).toHaveBeenLastCalledWith('88')
})

it('uses separate streams and event channels for different authenticated accounts', async () => {
  const connections: ReturnType<typeof createSharedQuoteStream>[] = []
  try {
    for (const scope of ['tsp-v1:first:personal:1', 'tsp-v1:second:personal:1']) {
      vi.resetModules()
      vi.doMock('./accountSession', () => ({
        accountScope: () => scope,
        subscribeAccountScopeChanges: () => () => {},
        accountEventStreamUrl: async () => '/api/events?ticket=scoped',
      }))
      const module = await import('./sharedQuoteStream')
      connections.push(module.createSharedQuoteStream())
    }
    await act(async () => {})
    expect(TestEventSource.instances.filter(stream => !stream.closed)).toHaveLength(2)
    const first = vi.fn()
    const second = vi.fn()
    connections[0].addEventListener('strategy_alert', first)
    connections[1].addEventListener('strategy_alert', second)
    await act(async () => {
      TestEventSource.instances[0].emit('strategy_alert', '{"alerts":["first-private"]}', '1')
    })
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).not.toHaveBeenCalled()
  } finally {
    for (const connection of connections) connection.close()
    vi.doUnmock('./accountSession')
  }
})
