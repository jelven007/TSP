// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type TenantDirectoryEntry } from '@/lib/api'
import { hasPermission } from '@/lib/accountSession'
import { TenantSelector } from './TenantSelector'

vi.mock('@/lib/api', () => ({
  api: {
    authStatus: vi.fn(),
    accountTenants: vi.fn(),
    accountSelectTenant: vi.fn(),
  },
}))
vi.mock('@/lib/accountSession', () => ({
  clearAccountCache: vi.fn(),
  hasPermission: vi.fn(() => true),
  multitenancyEnabled: vi.fn(() => true),
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | null = null
let queryClient: QueryClient

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(hasPermission).mockReturnValue(true)
  vi.mocked(api.authStatus).mockResolvedValue({
    auth_required: true, configured: true, has_users: true,
    legacy_migration_required: false, registration_enabled: true,
    email_verification_required: true, authenticated: true, multitenancy_enabled: true,
    user: { id: 'admin', email: null, created_at: 0, default_tenant_id: 'legacy' },
  })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  root = null
  queryClient.clear()
  document.body.innerHTML = ''
})

async function renderSelector(entry: TenantDirectoryEntry) {
  vi.mocked(api.accountTenants).mockResolvedValue({ tenants: [entry] })
  const container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => {
    root?.render(
      <QueryClientProvider client={queryClient}>
        <TenantSelector />
      </QueryClientProvider>,
    )
  })
  await act(async () => {
    await new Promise(resolve => window.setTimeout(resolve, 0))
  })
  return container
}

describe('administrator tenant labels', () => {
  it.each(['owner@example.test', 'tenant_alice'])('displays owner %s while preserving the tenant ID', async displayName => {
    const container = await renderSelector({
      id: 'legacy', name: '个人工作空间', display_name: displayName, status: 'active', cell_id: 'local',
    })
    const select = container.querySelector<HTMLSelectElement>('select')
    const option = select?.querySelector<HTMLOptionElement>('option[value="legacy"]')
    expect(option?.textContent).toBe(displayName)
    expect(option?.disabled).toBe(false)
    expect(select?.value).toBe('legacy')
    expect(container.textContent).not.toContain('个人工作空间')
  })

  it('keeps unavailable tenants disabled with their specific name', async () => {
    const container = await renderSelector({
      id: 'legacy', name: '个人工作空间', display_name: 'owner@example.test',
      status: 'suspended', cell_id: 'local',
    })
    const option = container.querySelector<HTMLOptionElement>('option[value="legacy"]')
    expect(option?.textContent).toBe('owner@example.test（不可用）')
    expect(option?.disabled).toBe(true)
  })

  it('supports older directory responses without a display name', async () => {
    const container = await renderSelector({
      id: 'legacy', name: '机构研究组', status: 'active', cell_id: 'local',
    })
    expect(container.querySelector('option[value="legacy"]')?.textContent).toBe('机构研究组')
  })

  it('does not expose the administrator directory to ordinary tenants', async () => {
    vi.mocked(hasPermission).mockReturnValue(false)
    const container = await renderSelector({
      id: 'legacy', name: '个人工作空间', status: 'active', cell_id: 'local',
    })
    expect(container.querySelector('select')).toBeNull()
    expect(api.accountTenants).not.toHaveBeenCalled()
  })
})
