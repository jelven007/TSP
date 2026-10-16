// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, type AuthStatus } from '@/lib/api'
import { Auth } from './Auth'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

let root: Root | null = null

afterEach(async () => {
  vi.restoreAllMocks()
  if (root) {
    await act(async () => root?.unmount())
    root = null
  }
  document.body.innerHTML = ''
})

describe('desktop authentication bypass', () => {
  it('redirects the login route directly to the application', async () => {
    const status: AuthStatus = {
      auth_required: false,
      configured: false,
      has_users: false,
      legacy_migration_required: false,
      registration_enabled: false,
      email_verification_required: false,
      authenticated: true,
      user: null,
    }
    vi.spyOn(api, 'authStatus').mockResolvedValue(status)

    const container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={['/login']}>
          <QueryClientProvider client={queryClient}>
            <Routes>
              <Route path="/login" element={<Auth />} />
              <Route path="/" element={<div>业务页面</div>} />
            </Routes>
          </QueryClientProvider>
        </MemoryRouter>,
      )
    })
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 0))
    })

    expect(document.body.textContent).toContain('业务页面')
    expect(document.body.textContent).not.toContain('创建账户')
    expect(document.body.textContent).not.toContain('登录后继续')
  })
})

describe('password recovery', () => {
  it('requires the registration secret, email code, and matching new passwords', async () => {
    const status: AuthStatus = {
      auth_required: true,
      configured: true,
      has_users: true,
      legacy_migration_required: false,
      registration_enabled: true,
      email_verification_required: true,
      authenticated: false,
      user: null,
    }
    vi.spyOn(api, 'authStatus').mockResolvedValue(status)
    const sendCode = vi.spyOn(api, 'authSendRecoveryCode').mockResolvedValue({
      ok: true,
      cooldown_seconds: 60,
      message: '验证码请求已受理',
    })
    const resetPassword = vi.spyOn(api, 'authResetPassword').mockResolvedValue({
      ok: true,
      message: '密码已重置',
    })

    const container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={['/login']}>
          <QueryClientProvider client={queryClient}>
            <Routes>
              <Route path="/login" element={<Auth />} />
            </Routes>
          </QueryClientProvider>
        </MemoryRouter>,
      )
    })
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 0))
    })

    const forgot = Array.from(document.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === '忘记密码')
    await act(async () => forgot?.click())
    expect(document.body.textContent).toContain('找回密码')
    expect(document.body.textContent).toContain('注册口令')

    const setInput = async (input: HTMLInputElement, value: string) => {
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          'value',
        )?.set
        setter?.call(input, value)
        input.dispatchEvent(new Event('input', { bubbles: true }))
      })
    }
    const email = document.querySelector('input[type="email"]') as HTMLInputElement
    const secret = document.querySelector('input[placeholder="输入注册口令"]') as HTMLInputElement
    await setInput(email, 'owner@example.com')
    await setInput(secret, 'registration-secret')

    const send = Array.from(document.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === '发送验证码')
    await act(async () => send?.click())
    expect(sendCode).toHaveBeenCalledWith('owner@example.com', 'registration-secret')

    const code = document.querySelector('input[autocomplete="one-time-code"]') as HTMLInputElement
    const passwords = Array.from(
      document.querySelectorAll('input[autocomplete="new-password"]'),
    ) as HTMLInputElement[]
    await setInput(code, '654321')
    await setInput(passwords[0], 'new-password-456')
    await setInput(passwords[1], 'new-password-456')
    const form = document.querySelector('form') as HTMLFormElement
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 0))
    })

    expect(resetPassword).toHaveBeenCalledWith(
      'owner@example.com',
      'registration-secret',
      '654321',
      'new-password-456',
    )
    expect(document.body.textContent).toContain('密码已重置，请使用新密码登录')
    expect(document.body.textContent).toContain('登录后继续使用量化工作台')
  })
})
