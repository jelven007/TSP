// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import type { AuthStatus } from '@/lib/api'
import { accountScope, accountStorage, confirmAccount } from '@/lib/accountSession'
import { useStrategyPool } from '@/lib/useStrategyPool'
import { AccountScopeBoundary } from './AccountScopeBoundary'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

function authenticated(tenant: string): AuthStatus {
  return {
    auth_required: true, configured: true, has_users: true, legacy_migration_required: false,
    registration_enabled: true, email_verification_required: true, authenticated: true,
    multitenancy_enabled: true,
    user: {
      id: 'admin', email: 'admin@example.test', created_at: 1, role: 'admin',
      default_tenant_id: tenant, auth_version: 1, permissions: ['business:read'],
    },
  }
}

function PrivateValue({ load }: { load: () => Promise<string> }) {
  const query = useQuery({ queryKey: ['private-value'], queryFn: load, staleTime: Infinity })
  return <div>{query.data ?? 'loading'}</div>
}

function SelectedStrategies() {
  const { pool } = useStrategyPool()
  return <div>{pool.join(',')}</div>
}

let root: Root | null = null
let client: QueryClient | null = null

afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  root = null
  client?.clear()
  client = null
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

it('drops the previous account cache and remounts queries inside the current page', async () => {
  confirmAccount(authenticated('first'))
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['private-value'], 'first tenant')
  const load = vi.fn(async () => 'second tenant')
  const container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)

  await act(async () => {
    root?.render(
      <QueryClientProvider client={client!}>
        <AccountScopeBoundary>
          <PrivateValue load={load} />
        </AccountScopeBoundary>
      </QueryClientProvider>,
    )
  })
  expect(container.textContent).toBe('first tenant')

  await act(async () => {
    confirmAccount(authenticated('second'))
    await new Promise(resolve => window.setTimeout(resolve, 0))
  })
  await act(async () => {
    await new Promise(resolve => window.setTimeout(resolve, 10))
  })

  expect(load).toHaveBeenCalledTimes(1)
  expect(container.textContent).toBe('second tenant')
  expect(client.getQueryData(['auth-status'])).toMatchObject({
    user: { default_tenant_id: 'second' },
  })
})

it('retains each tenant selection through real boundary remounts without sharing private cached results', async () => {
  localStorage.clear()
  confirmAccount(authenticated('original'))
  const originalScope = accountScope()
  accountStorage.setItem('strategy-pool', '["s02","custom_existing"]')
  accountStorage.setItem('tf-query-cache', 'original private result')
  confirmAccount(authenticated('different'))
  accountStorage.setItem('strategy-pool', '["s01"]')
  confirmAccount(authenticated('original'))
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => {
    root?.render(
      <QueryClientProvider client={client!}>
        <AccountScopeBoundary><SelectedStrategies /></AccountScopeBoundary>
      </QueryClientProvider>,
    )
  })
  expect(container.textContent).toBe('s02,custom_existing')

  await act(async () => { confirmAccount(authenticated('different')) })
  expect(container.textContent).toBe('s01')
  expect(localStorage.getItem(`${originalScope}:tf-query-cache`)).toBeNull()
  await act(async () => { confirmAccount(authenticated('original')) })
  expect(container.textContent).toBe('s02,custom_existing')
})
