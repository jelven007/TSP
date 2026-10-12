// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import type { Preferences } from '@/lib/api'
import { SettingsMonitoringPanel } from './Monitoring'

let prefs: Partial<Preferences> | undefined
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: prefs }),
  useCapabilities: () => ({ data: undefined }),
  useQuoteStatus: () => ({ data: undefined }),
  useQuoteInterval: () => ({ data: undefined }),
}))
vi.mock('@/lib/useSharedMutations', () => ({
  useUpdateQuoteInterval: () => ({ mutate: vi.fn() }),
  useToggleRealtimeQuotes: () => ({ mutateAsync: vi.fn() }),
}))
vi.mock('@/components/data/DepthConfigCard', () => ({ DepthConfigContent: () => null }))
vi.mock('@/lib/api', () => ({
  api: { minuteRefreshStatus: () => Promise.resolve({ available: false }) },
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | null = null
let host: HTMLDivElement | null = null
let client: QueryClient | null = null

afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  root = null
  prefs = undefined
})

function render() {
  if (!root) {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  }
  act(() => root?.render(
    <QueryClientProvider client={client!}>
      <SettingsMonitoringPanel />
    </QueryClientProvider>,
  ))
}

function emailSection(): HTMLElement {
  const label = [...host!.querySelectorAll('span')].find(node => node.textContent === '邮件')
  return label!.parentElement!.parentElement!
}

it('hydrates registration mail defaults after preferences load without a plaintext password', () => {
  render()
  prefs = {
    webhook_default_channels: ['email'],
    email_smtp_config: {
      host: 'smtp.example.com', port: 587, security: 'starttls',
      username: 'sender@example.com', from_address: 'sender@example.com',
      to_addresses: ['user@example.com'],
    },
    email_smtp_password_set: true,
  }
  render()

  const section = emailSection()
  expect((section.querySelector('input[type="checkbox"]') as HTMLInputElement).checked).toBe(true)
  act(() => section.querySelector('span')!.parentElement!.click())

  const inputs = [...section.querySelectorAll('input')] as HTMLInputElement[]
  expect(inputs.map(input => input.value)).toContain('smtp.example.com')
  expect(inputs.map(input => input.value)).toContain('user@example.com')
  const password = section.querySelector('input[type="password"]') as HTMLInputElement
  expect(password.value).toBe('')
  expect(password.placeholder).toContain('已配置')
  expect(section.textContent).toContain('注册邮箱')
  const testButton = [...section.querySelectorAll('button')].find(button => button.textContent === '测试')
  expect(testButton?.disabled).toBe(false)
})

it('keeps saved channel opt-out and disables testing for an unconfigured transport', () => {
  prefs = { webhook_default_channels: [], email_smtp_password_set: false }
  render()
  const section = emailSection()
  expect((section.querySelector('input[type="checkbox"]') as HTMLInputElement).checked).toBe(false)
  act(() => section.querySelector('span')!.parentElement!.click())
  const testButton = [...section.querySelectorAll('button')].find(button => button.textContent === '测试')
  expect(testButton?.disabled).toBe(true)
})
