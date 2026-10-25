// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthStatus } from '../api'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

function account(role: 'tenant' | 'admin', multitenancy = true): AuthStatus {
  return {
    auth_required: true,
    configured: true,
    has_users: true,
    legacy_migration_required: false,
    registration_enabled: true,
    email_verification_required: true,
    authenticated: true,
    multitenancy_enabled: multitenancy,
    user: {
      id: role,
      email: `${role}@example.test`,
      created_at: 1,
      role,
      default_tenant_id: 'personal',
      auth_version: 1,
      permissions: [
        'business:read', 'business:write', 'account:manage',
        ...(role === 'admin' ? ['settings:manage', 'data:manage'] : []),
      ],
    },
  }
}

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('minute service status access', () => {
  it('lets tenants read freshness without requesting administrator settings', async () => {
    const { confirmAccount } = await import('../accountSession')
    const { api } = await import('../api')
    const { toast } = await import('@/components/Toast')
    confirmAccount(account('tenant'))
    const fetch = vi.fn(async (path: string) => path.startsWith('/api/settings/')
      ? new Response(JSON.stringify({ detail: '无权使用该功能' }), { status: 403 })
      : new Response(JSON.stringify({ available: true, healthy: true })))
    vi.stubGlobal('fetch', fetch)

    await expect(api.minuteRefreshStatus()).resolves.toEqual({ available: true, healthy: true })

    expect(fetch).toHaveBeenCalledWith(
      '/api/intraday/minute-refresh/status',
      expect.objectContaining({
        headers: expect.objectContaining({ 'X-TSP-Account-Scope': 'tsp-v1:tenant:personal:1' }),
      }),
    )
    expect(toast).not.toHaveBeenCalled()
  })

  it.each([
    ['administrator', 'admin', true],
    ['single account mode', 'tenant', false],
  ] as const)('retains detailed service status for %s', async (_label, role, multitenancy) => {
    const { confirmAccount } = await import('../accountSession')
    const { api } = await import('../api')
    confirmAccount(account(role, multitenancy))
    const status = { available: true, healthy: false, last_error: 'collector unavailable' }
    const fetch = vi.fn(async () => new Response(JSON.stringify(status)))
    vi.stubGlobal('fetch', fetch)

    await expect(api.minuteRefreshStatus()).resolves.toEqual(status)
    expect(fetch).toHaveBeenCalledWith('/api/settings/minute-refresh/status', expect.any(Object))
  })

  it('selects the current account endpoint after switching from administrator to tenant', async () => {
    const { confirmAccount } = await import('../accountSession')
    const { api } = await import('../api')
    const fetch = vi.fn(async (_path: string) => new Response(JSON.stringify({ available: false, healthy: false })))
    vi.stubGlobal('fetch', fetch)

    confirmAccount(account('admin'))
    await api.minuteRefreshStatus()
    confirmAccount(account('tenant'))
    await api.minuteRefreshStatus()

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      '/api/settings/minute-refresh/status',
      '/api/intraday/minute-refresh/status',
    ])
  })
})
