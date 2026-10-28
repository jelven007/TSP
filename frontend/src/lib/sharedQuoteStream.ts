import { accountEventStreamUrl, accountScope, subscribeAccountScopeChanges } from './accountSession'

type Handler = (event: MessageEvent) => void
export type QuoteStreamConnection = {
  readonly lastEventId: string
  onopen: (() => void) | null
  onerror: (() => void) | null
  addEventListener: (type: string, handler: Handler) => void
  close: () => void
}

const EVENT_TYPES = [
  'quotes_updated', 'strategy_results_updated', 'depth_updated', 'strategy_alert',
  'review_progress', 'events_reset', 'access_revoked',
] as const

type StreamMessage =
  | { scope: string; kind: 'hello' }
  | { scope: string; kind: 'open'; cursor: string }
  | { scope: string; kind: 'error' }
  | { scope: string; kind: 'event'; type: string; data: string; lastEventId: string }

/**
 * HTTP/1.1 browsers allow only six connections per origin. Elect one SSE owner
 * across pages in the same account scope; followers consume its events locally.
 * Keep retries in useQuoteStream and exchange short-lived tickets only after
 * acquiring the lock, so a waiting page cannot use an expired ticket.
 */
export function createSharedQuoteStream(lastEventId = ''): QuoteStreamConnection {
  const scope = accountScope()
  const handlers = new Map<string, Handler[]>()
  const controller = new AbortController()
  let source: EventSource | null = null
  let channel: BroadcastChannel | null = null
  let release: (() => void) | undefined
  let closed = false
  let opened = false
  let cursor = lastEventId
  let unsubscribe = () => {}
  const active = () => !closed && accountScope() === scope
  const remember = (id: string) => {
    if (/^\d+$/.test(id) && (!cursor || BigInt(id) > BigInt(cursor))) cursor = id
  }
  const connection: QuoteStreamConnection = {
    get lastEventId() { return cursor },
    onopen: null,
    onerror: null,
    addEventListener(type, handler) {
      handlers.set(type, [...(handlers.get(type) ?? []), handler])
    },
    close() {
      if (closed) return
      closed = true
      source?.close()
      source = null
      channel?.close()
      channel = null
      controller.abort()
      release?.()
      unsubscribe()
      handlers.clear()
    },
  }
  unsubscribe = subscribeAccountScopeChanges(() => connection.close())

  const receive = (message: StreamMessage) => {
    if (!active() || message.scope !== scope) return
    if (message.kind === 'open') {
      remember(message.cursor)
      opened = true
      connection.onopen?.()
    } else if (message.kind === 'error') {
      connection.onerror?.()
    } else if (message.kind === 'event' && EVENT_TYPES.includes(message.type as typeof EVENT_TYPES[number])) {
      remember(message.lastEventId)
      const event = new MessageEvent(message.type, { data: message.data, lastEventId: message.lastEventId })
      for (const handler of handlers.get(message.type) ?? []) handler(event)
    } else if (message.kind === 'hello' && source && opened) {
      channel?.postMessage({ scope, kind: 'open', cursor } satisfies StreamMessage)
    }
  }
  const publish = (message: StreamMessage) => {
    if (!active()) return
    // Post first: local handlers may close the connection on errors or expiry.
    channel?.postMessage(message)
    receive(message)
  }
  const connect = async () => {
    try {
      const url = await accountEventStreamUrl(cursor)
      if (!active()) return
      source = new EventSource(url)
      source.onopen = () => publish({ scope, kind: 'open', cursor })
      source.onerror = () => publish({ scope, kind: 'error' })
      for (const type of EVENT_TYPES) {
        source.addEventListener(type, (event: MessageEvent) => {
          publish({ scope, kind: 'event', type, data: event.data, lastEventId: event.lastEventId })
        })
      }
    } catch {
      publish({ scope, kind: 'error' })
    }
  }

  if (typeof BroadcastChannel !== 'undefined' && navigator.locks?.request) {
    try {
      const name = `tsp:quote-stream:${scope}`
      channel = new BroadcastChannel(name)
      channel.onmessage = (event: MessageEvent<StreamMessage>) => receive(event.data)
      channel.postMessage({ scope, kind: 'hello' } satisfies StreamMessage)
      void navigator.locks.request(name, { signal: controller.signal }, async () => {
        if (!active()) return
        const held = new Promise<void>(resolve => { release = resolve })
        // Unmount must release the lock even while a ticket request is pending.
        void connect()
        await held
      }).catch(() => {
        if (!active()) return
        channel?.close()
        channel = null
        void connect()
      })
    } catch {
      channel?.close()
      channel = null
      void connect()
    }
  } else {
    // Older browsers retain the existing single-page transport.
    void connect()
  }
  return connection
}
