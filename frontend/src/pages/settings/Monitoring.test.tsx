// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import type { Preferences } from '@/lib/api'
import { SettingsMonitoringPanel } from './Monitoring'

let prefs: Partial<Preferences> | undefined
let quoteInterval: { interval: number } | undefined
const minuteStatus = vi.hoisted(() => ({ available: true, capability_ok: false }))
const { updateEmailSmtp, updateInterval, saveMonitoring } = vi.hoisted(() => ({
  updateEmailSmtp: vi.fn((toAddresses: string[]) => Promise.resolve({
    email_smtp_config: { to_addresses: toAddresses },
    email_smtp_password_set: true,
  })),
  updateInterval: vi.fn(),
  saveMonitoring: vi.fn(),
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: prefs }),
  useCapabilities: () => ({ data: undefined }),
  useQuoteStatus: () => ({ data: undefined }),
  useQuoteInterval: () => ({ data: quoteInterval }),
}))
vi.mock('@/lib/useSharedMutations', () => ({
  useUpdateQuoteInterval: () => ({ mutate: updateInterval }),
  useToggleRealtimeQuotes: () => ({ mutateAsync: vi.fn() }),
}))
vi.mock('@/components/data/DepthConfigCard', () => ({ DepthConfigContent: () => null }))
vi.mock('@/lib/api', () => ({
  api: {
    minuteRefreshStatus: () => Promise.resolve({ ...minuteStatus }),
    updateEmailSmtp,
    updateRealtimeMonitorConfig: saveMonitoring,
  },
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
  quoteInterval = undefined
  minuteStatus.capability_ok = false
  vi.clearAllMocks()
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
  expect(password.disabled).toBe(true)
  expect(inputs.filter(input => input.type !== 'checkbox' && !input.disabled).map(input => input.value))
    .toEqual(['user@example.com'])
  const security = section.querySelector('select') as HTMLSelectElement
  expect(security.value).toBe('starttls')
  expect(security.disabled).toBe(true)
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

it('saves only editable recipients while system SMTP fields remain unchanged', async () => {
  prefs = {
    email_smtp_config: {
      host: 'smtp.example.com', port: 587, security: 'starttls',
      username: 'sender@example.com', from_address: 'sender@example.com',
      to_addresses: ['user@example.com'],
    },
    email_smtp_password_set: true,
  }
  render()
  const section = emailSection()
  act(() => section.querySelector('span')!.parentElement!.click())
  const recipient = section.querySelector('input[placeholder="alerts@example.com"]') as HTMLInputElement
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    setValue.call(recipient, ' chosen@example.com；second@example.com, ')
    recipient.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(recipient.value).toBe(' chosen@example.com；second@example.com, ')
  const saveButton = [...section.querySelectorAll('button')].find(button => button.textContent === '保存')!
  await act(async () => saveButton.click())

  expect(updateEmailSmtp.mock.calls).toEqual([[['chosen@example.com', 'second@example.com']]])
  expect(recipient.value).toBe('chosen@example.com, second@example.com')
  const lockedInputs = [...section.querySelectorAll('input:disabled')] as HTMLInputElement[]
  expect(lockedInputs.map(input => input.value)).toEqual([
    'smtp.example.com', '587', 'sender@example.com', '', 'sender@example.com',
  ])
})

it('shows initialization intervals before preferences arrive without saving them', () => {
  render()
  const inputs = [...host!.querySelectorAll<HTMLInputElement>('input[type="range"]')]
  expect(inputs.map(input => input.value)).toEqual(['5'])
  expect(updateInterval).not.toHaveBeenCalled()
  expect(saveMonitoring).not.toHaveBeenCalled()
})

it('hydrates saved intervals without replacing them with initialization values', () => {
  render()
  quoteInterval = { interval: 1 }
  prefs = { minute_intraday_refresh_interval: 30, minute_refresh_interval: 1 }
  render()
  const inputs = [...host!.querySelectorAll<HTMLInputElement>('input[type="range"]')]
  expect(inputs.map(input => input.value)).toEqual(['30'])
  expect(updateInterval).not.toHaveBeenCalled()
  expect(saveMonitoring).not.toHaveBeenCalled()
})

it('removes quote polling and minute writer controls from monitoring settings', async () => {
  minuteStatus.capability_ok = true
  prefs = { minute_refresh_enabled: false, minute_refresh_interval: 60 }
  render()
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
  expect(host!.textContent).not.toContain('盘中分钟落盘')
  expect(host!.textContent).not.toContain('每轮拉取全市场')
  expect(host!.textContent).not.toContain('全量分钟')
  expect(host!.querySelectorAll<HTMLInputElement>('input[type="range"]')).toHaveLength(1)
  expect(saveMonitoring).not.toHaveBeenCalled()
})
