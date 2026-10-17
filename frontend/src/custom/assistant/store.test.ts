// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AssistantSession } from './store'
import type { SessionCollection } from './client'

const remote = vi.hoisted(() => ({ revision: 0, sessions: [] as AssistantSession[], fail: false }))
const stream = vi.hoisted(() => ({ finish: undefined as (() => void) | undefined }))
vi.mock('./client', () => {
  class SessionSyncError extends Error {
    constructor(public status: number, message: string) { super(message) }
  }
  return {
    SessionSyncError,
    assistantSessions: vi.fn(async (body?: SessionCollection) => {
      if (remote.fail) throw new Error('offline')
      if (body) {
        if (body.revision !== remote.revision) throw new SessionSyncError(409, 'conflict')
        remote.revision += 1
        remote.sessions = structuredClone(body.sessions)
      }
      return { revision: remote.revision, sessions: structuredClone(remote.sessions) }
    }),
    assistantChatStream: async function* () {
      yield { type: 'delta', content: '生成中' }
      await new Promise<void>(resolve => { stream.finish = resolve })
      yield { type: 'delta', content: '已完成' }
      yield { type: 'done' }
    },
    decideAssistantAction: vi.fn(),
  }
})

const history: AssistantSession = {
  id: 'one', title: '历史', createdAt: 100,
  messages: [{ id: 'msg', role: 'user', content: '内容', ts: 100 }],
}

let act: typeof import('react')['act']
let unmount: (() => void) | undefined
async function renderStore(store: typeof import('./store')) {
  const { createElement } = await import('react')
  const { createRoot } = await import('react-dom/client')
  const result = { current: undefined as unknown as ReturnType<typeof store.useAssistantStore> }
  function Harness() {
    result.current = store.useAssistantStore()
    return null
  }
  const node = document.createElement('div')
  const root = createRoot(node)
  act(() => root.render(createElement(Harness)))
  unmount = () => { act(() => root.unmount()); node.remove() }
  return { result }
}

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  ;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
  act = (await import('react')).act
  window.localStorage.clear()
  remote.revision = 0
  remote.sessions = []
  remote.fail = false
  stream.finish = undefined
  const account = await import('@/lib/accountSession')
  account.confirmAccount({
    auth_required: false, authenticated: false, configured: false, has_users: false,
    legacy_migration_required: false, registration_enabled: false,
    email_verification_required: false, multitenancy_enabled: false, user: null,
  })
})

afterEach(() => { unmount?.(); unmount = undefined; vi.clearAllTimers(); vi.useRealTimers() })

it('migrates local history only against revision zero and reloads other-device updates', async () => {
  localStorage.setItem('assistant.sessions.v1', JSON.stringify({ sessions: [history], activeId: 'one' }))
  const store = await import('./store')
  await store.syncAssistant()
  expect(remote.revision).toBe(1)
  expect(remote.sessions).toEqual([history])
  remote.sessions = [{ ...history, title: '另一设备' }]
  remote.revision = 2
  await store.syncAssistant()
  expect(store.activeSession()?.title).toBe('另一设备')
})

it('retains offline drafts, rejects stale deletes and resolves only by an explicit choice', async () => {
  remote.revision = 1
  remote.sessions = [history]
  const store = await import('./store')
  const { result } = await renderStore(store)
  await act(() => store.syncAssistant())
  act(() => store.deleteSession('one'))
  remote.revision = 2
  remote.sessions = [{ ...history, title: '更新' }]
  await act(() => store.syncAssistant())
  expect(result.current.sync).toBe('conflict')
  expect(result.current.sessions).toEqual([])
  expect(remote.sessions[0].title).toBe('更新')
  await act(() => store.adoptServerSessions())
  expect(result.current.sync).toBe('synced')
  expect(store.activeSession()?.title).toBe('更新')
  expect(localStorage.getItem('assistant.sessions.conflict-backup.v1')).toBe('[]')
})

it('does not resurrect a deleted collection from a pre-sync browser history', async () => {
  localStorage.setItem('assistant.sessions.v1', JSON.stringify({ sessions: [history] }))
  remote.revision = 8
  const store = await import('./store')
  const { result } = await renderStore(store)
  await act(() => store.syncAssistant())
  expect(result.current.sync).toBe('conflict')
  expect(store.activeSession()?.id).toBe('one')
  expect(remote.sessions).toEqual([])
})

it('retries network failure with the same revision and persists deletion after restart', async () => {
  remote.sessions = [history]
  remote.revision = 1
  const store = await import('./store')
  await store.syncAssistant()
  remote.fail = true
  store.deleteSession('one')
  await store.syncAssistant()
  expect(JSON.parse(localStorage.getItem('assistant.sessions.v1')!).dirty).toBe(true)
  remote.fail = false
  await store.syncAssistant()
  expect(remote.sessions).toEqual([])
  expect(remote.revision).toBe(2)
  expect(JSON.parse(localStorage.getItem('assistant.sessions.v1')!).dirty).toBe(false)
})

it('does not sync or switch sessions during generation, then saves the finished reply', async () => {
  remote.sessions = [history]
  remote.revision = 1
  const store = await import('./store')
  await store.syncAssistant()
  store.sendMessage('新问题')
  await Promise.resolve()
  await Promise.resolve()
  store.newSession()
  store.selectSession('elsewhere')
  store.deleteSession('one')
  await store.syncAssistant()
  expect(store.activeSession()?.id).toBe('one')
  expect(remote.revision).toBe(1)
  stream.finish!()
  await vi.advanceTimersByTimeAsync(500)
  expect(remote.revision).toBe(2)
  expect(remote.sessions[0].messages.at(-1)).toMatchObject({
    role: 'assistant', content: '生成中已完成', streaming: false,
  })
})
