// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type AuthStatus } from '@/lib/api'
import { accountScope, confirmAccount, hasPermission } from '@/lib/accountSession'
import { TenantSelector } from '@/components/TenantSelector'
import { Auth } from './Auth'
import { SettingsAccountPanel } from './settings/Account'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | null = null
let client: QueryClient
afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  root = null
  client?.clear()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

it('signs in as admin and exposes personal space and settings without tenant requests', async () => {
  let status: AuthStatus = {
    auth_required: true, configured: true, has_users: true, authenticated: false,
    registration_enabled: false, email_verification_required: false,
    legacy_migration_required: false, local_admin_mode: true,
    multitenancy_enabled: false, user: null,
  }
  confirmAccount(status)
  vi.spyOn(api, 'authStatus').mockImplementation(async () => {
    confirmAccount(status)
    return status
  })
  const login = vi.spyOn(api, 'authLogin').mockImplementation(async () => {
    status = { ...status, authenticated: true, user: {
      id: 'local-admin', username: 'admin', email: null, role: 'admin', created_at: 1,
      default_tenant_id: 'personal',
      workspace: { id: 'personal', name: '个人空间' },
    } }
    return { ok: true, authenticated: true, user: status.user }
  })
  const tenants = vi.spyOn(api, 'accountTenants')
  const container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/login']}>
          <Routes>
            <Route path="/login" element={<Auth />} />
            <Route path="/" element={<><TenantSelector /><SettingsAccountPanel /></>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(container.textContent).toContain('进入个人空间')
  expect(container.textContent).not.toContain('注册')
  expect(container.textContent).not.toContain('忘记密码')
  const setInput = async (selector: string, value: string) => {
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>(selector)!
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  await setInput('input[autocomplete="username"]', 'admin')
  await setInput('input[autocomplete="current-password"]', 'Admin123')
  await act(async () => {
    container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(login).toHaveBeenCalledWith('admin', 'Admin123')
  expect(container.textContent).toContain('管理员')
  expect(container.textContent).toContain('个人空间')
  expect(container.textContent).toContain('修改密码')
  expect(container.querySelector('select[aria-label="当前业务租户"]')).toBeNull()
  expect(tenants).not.toHaveBeenCalled()
  expect(accountScope()).toBe('tsp-v1:local-admin:personal:0')
  expect(hasPermission('settings:manage')).toBe(true)
  expect(hasPermission('research:run')).toBe(true)
})
