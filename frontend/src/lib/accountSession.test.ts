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
