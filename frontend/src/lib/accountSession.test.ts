// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import type { AuthStatus } from './api'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

function authenticated(id: string, tenant = 'personal', version = 1): AuthStatus {
  return {
    auth_required: true, configured: true, has_users: true, legacy_migration_required: false,
    registration_enabled: true, email_verification_required: true, authenticated: true,
    multitenancy_enabled: true,
    user: { id, email: `${id}@example.test`, created_at: 1, role: 'tenant',
      default_tenant_id: tenant, auth_version: version,
      permissions: ['business:read', 'business:write', 'account:manage'] },
  }
}

beforeEach(() => {
  vi.resetModules()
  vi.unstubAllGlobals()
  localStorage.clear()
  window.history.replaceState(null, '', '/login')
})

it('separates private drafts, caches and task IDs by account, tenant and auth version', async () => {
  const session = await import('./accountSession')
  localStorage.setItem('tf-query-cache', 'old unscoped private data')
  session.confirmAccount(authenticated('first'))
  session.accountStorage.setItem('strategy-draft', 'first strategy')
  session.confirmAccount(authenticated('second'))
  expect(session.accountStorage.getItem('strategy-draft')).toBeNull()
  session.accountStorage.setItem('strategy-draft', 'second strategy')
  session.confirmAccount(authenticated('first', 'another'))
  expect(session.accountStorage.getItem('strategy-draft')).toBeNull()
  session.confirmAccount(authenticated('first', 'personal', 2))
  expect(session.accountStorage.getItem('strategy-draft')).toBeNull()
  session.confirmAccount(authenticated('first'))
  expect(session.accountStorage.getItem('strategy-draft')).toBe('first strategy')
  session.clearAccountCache()
  expect(session.accountStorage.getItem('strategy-draft')).toBeNull()
  expect(localStorage.getItem('tf-query-cache')).toBeNull()
  session.confirmAccount(authenticated('second'))
  expect(session.accountStorage.getItem('strategy-draft')).toBe('second strategy')
})

it.each([
  ['strategy-pool', ['s02', 'custom_existing']],
  ['strategy-pool-1m', ['custom_minute']],
  ['tf-page-zoom', 1.2],
  ['watchlist_columns', [{ key: 'symbol' }]],
  ['default-strategy-basic-filter', { float_cap_min: 5000000000 }],
])('retains the %s preference when authentication cache is cleared', async (key, value) => {
  const session = await import('./accountSession')
  session.confirmAccount(authenticated('first'))
  const encoded = JSON.stringify(value)
  session.accountStorage.setItem(key as string, encoded)
  for (const transient of ['tf-query-cache', 'strategy-draft', 'strategy-backtest-last', 'abnormal-last-result', 'private-task-id']) {
    session.accountStorage.setItem(transient, 'private result')
  }

  session.clearAccountCache()

  expect(session.accountStorage.getItem(key as string)).toBe(encoded)
  for (const transient of ['tf-query-cache', 'strategy-draft', 'strategy-backtest-last', 'abnormal-last-result', 'private-task-id']) {
    expect(session.accountStorage.getItem(transient)).toBeNull()
  }
})

it('restores the same account selection after logout and a new auth version', async () => {
  const session = await import('./accountSession')
  session.confirmAccount(authenticated('first'))
  const previousScope = session.accountScope()
  session.accountStorage.setItem('strategy-pool', '["s02","custom_existing"]')
  session.accountStorage.setItem('private-task-id', 'old task')
  session.clearAccountCache()
  session.confirmAccount({ ...authenticated('first'), authenticated: false, user: null })
  expect(session.accountStorage.getItem('strategy-pool')).toBeNull()

  session.confirmAccount(authenticated('first', 'personal', 2))

  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
  expect(session.accountStorage.getItem('private-task-id')).toBeNull()
  expect(session.verifyResponseScope(previousScope)).toBe(false)
})

it.each([
  ['second', 'personal'],
  ['first', 'another'],
])('keeps persistent selection isolated when switching to %s/%s', async (id, tenant) => {
  const session = await import('./accountSession')
  session.confirmAccount(authenticated('first'))
  const previousScope = session.accountScope()
  session.accountStorage.setItem('strategy-pool', '["s02","custom_existing"]')
  session.accountStorage.setItem('tf-query-cache', 'first private cache')
  session.confirmAccount(authenticated(id, tenant))
  expect(session.accountStorage.getItem('strategy-pool')).toBeNull()
  session.accountStorage.setItem('strategy-pool', '["s01"]')
  session.accountStorage.setItem('tf-query-cache', 'current private cache')

  session.clearAccountCache(previousScope)

  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s01"]')
  expect(session.accountStorage.getItem('tf-query-cache')).toBe('current private cache')
  session.confirmAccount(authenticated('first'))
  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
  expect(session.accountStorage.getItem('tf-query-cache')).toBeNull()
})

it('migrates legacy preferences from the latest version of only the current account and tenant', async () => {
  const session = await import('./accountSession')
  localStorage.setItem('tsp-v1:first:personal:1:strategy-pool', '["s01"]')
  localStorage.setItem('tsp-v1:first:personal:2:strategy-pool', '["s02","custom_existing"]')
  localStorage.setItem('tsp-v1:second:personal:3:strategy-pool', '["foreign"]')
  localStorage.setItem('tsp-v1:first:another:4:strategy-pool', '["other_tenant"]')
  localStorage.setItem('tsp-v1:first:personal:2:tf-query-cache', 'private cached rows')
  session.confirmAccount(authenticated('first', 'personal', 3))

  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
  expect(session.accountStorage.getItem('tf-query-cache')).toBeNull()
  session.clearAccountCache('tsp-v1:first:personal:2')
  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
  expect(localStorage.getItem('tsp-v1:second:personal:3:strategy-pool')).toBe('["foreign"]')
})

it('preserves unread legacy preferences while clearing a previous account scope', async () => {
  const session = await import('./accountSession')
  localStorage.setItem('tsp-v1:first:personal:1:strategy-pool', '["s02","custom_existing"]')
  localStorage.setItem('tsp-v1:first:personal:1:private-task-id', 'first task')
  session.confirmAccount(authenticated('second'))

  session.clearAccountCache('tsp-v1:first:personal:1')

  expect(session.accountStorage.getItem('strategy-pool')).toBeNull()
  expect(localStorage.getItem('tsp-v1:first:personal:1:private-task-id')).toBeNull()
  session.confirmAccount(authenticated('first', 'personal', 2))
  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
})

it('does not replace a newer preference when retiring a legacy scope', async () => {
  const session = await import('./accountSession')
  session.confirmAccount(authenticated('first', 'personal', 2))
  session.accountStorage.setItem('strategy-pool', '["s02","custom_existing"]')
  localStorage.setItem('tsp-v1:first:personal:1:strategy-pool', '["s01"]')

  session.clearAccountCache('tsp-v1:first:personal:1')

  expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
})

it('does not resurrect a removed preference from any legacy auth version', async () => {
  const session = await import('./accountSession')
  localStorage.setItem('tsp-v1:first:personal:1:strategy-pool-1m', '["old_minute"]')
  localStorage.setItem('tsp-v1:first:personal:2:strategy-pool-1m', '["new_minute"]')
  session.confirmAccount(authenticated('first', 'personal', 3))
  expect(session.accountStorage.getItem('strategy-pool-1m')).toBe('["new_minute"]')

  session.accountStorage.removeItem('strategy-pool-1m')

  expect(session.accountStorage.getItem('strategy-pool-1m')).toBeNull()
  session.confirmAccount(authenticated('first', 'personal', 4))
  expect(session.accountStorage.getItem('strategy-pool-1m')).toBeNull()
})

it('keeps an unread legacy preference if migration cannot write, while still clearing private caches', async () => {
  const session = await import('./accountSession')
  localStorage.setItem('tsp-v1:first:personal:1:strategy-pool', '["s02","custom_existing"]')
  localStorage.setItem('tsp-v1:first:personal:1:tf-query-cache', 'private data')
  session.confirmAccount(authenticated('first'))
  const blocked = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('full', 'QuotaExceededError')
  })
  try {
    expect(session.accountStorage.getItem('strategy-pool')).toBe('["s02","custom_existing"]')
    session.clearAccountCache()
    expect(localStorage.getItem('tsp-v1:first:personal:1:strategy-pool')).toBe('["s02","custom_existing"]')
    expect(localStorage.getItem('tsp-v1:first:personal:1:tf-query-cache')).toBeNull()
  } finally {
    blocked.mockRestore()
  }
})

it('rejects private late responses after an account change', async () => {
  const session = await import('./accountSession')
  const { request } = await import('./api/client')
  session.confirmAccount(authenticated('first'))
  let release!: (response: Response) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { release = resolve })))
  const pending = request('/api/watchlist')
  session.confirmAccount(authenticated('second'))
  release(new Response(JSON.stringify({ symbols: ['first-private'] })))
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
})

it('reports account scope changes without navigating the browser', async () => {
  const session = await import('./accountSession')
  const changes: Array<{ previousScope: string; nextScope: string }> = []
  const unsubscribe = session.subscribeAccountScopeChanges(change => changes.push(change))
  window.history.replaceState(null, '', '/watchlist')

  session.confirmAccount(authenticated('first'))
  session.confirmAccount(authenticated('second', 'research'))

  expect(changes).toHaveLength(1)
  expect(changes[0]).toMatchObject({
    previousScope: 'tsp-v1:first:personal:1',
    nextScope: 'tsp-v1:second:research:1',
  })
  expect(session.accountScope()).toBe('tsp-v1:second:research:1')
  expect(session.verifyResponseScope('tsp-v1:first:personal:1')).toBe(false)
  expect(window.location.pathname).toBe('/watchlist')
  unsubscribe()
})

it('exchanges the browser session for a tenant event ticket', async () => {
  const session = await import('./accountSession')
  session.confirmAccount(authenticated('first'))
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify({
    ticket: 'tss_signed',
    expires_in: 60,
    stream: '/api/events?ticket=tss_signed',
  }), { status: 200 }))
  vi.stubGlobal('fetch', fetch)

  await expect(session.accountEventStreamUrl('42')).resolves.toBe(
    '/api/events?ticket=tss_signed&last_event_id=42',
  )
  const [path, init] = fetch.mock.calls[0]
  expect(path).toBe('/api/events/ticket')
  expect(init?.method).toBe('POST')
  expect(new Headers(init?.headers).get('X-TSP-Account-Scope')).toContain('tsp-v1:first:')
})

it('rejects external, login and forbidden redirects while retaining business links', async () => {
  const session = await import('./accountSession')
  session.confirmAccount(authenticated('first'))
  for (const value of ['//hostile.test', '/\\hostile.test', '/login', '/data', '/settings/keys', '/settings?tab=ai', '/dev']) {
    expect(session.safeLoginRedirect(value)).toBe('/')
  }
  expect(session.safeLoginRedirect('/watchlist?group=mine')).toBe('/watchlist?group=mine')
  expect(session.safeLoginRedirect('/settings?tab=account')).toBe('/settings?tab=account')
})
