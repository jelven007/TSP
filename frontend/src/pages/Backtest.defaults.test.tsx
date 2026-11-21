// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { confirmAccount } from '@/lib/accountSession'
import { Backtest } from './Backtest'

vi.mock('./backtest/StrategyBacktest', () => ({
  StrategyBacktest: ({ initializationDefaults }: { initializationDefaults?: { last?: { initialCapital?: string } } }) =>
    <div data-testid="strategy-defaults">{initializationDefaults?.last?.initialCapital ?? 'empty'}</div>,
}))
vi.mock('@/components/PageHeader', () => ({ PageHeader: () => null }))

afterEach(() => vi.restoreAllMocks())

async function setup() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  confirmAccount({
    auth_required: true, configured: true, has_users: true, legacy_migration_required: false,
    registration_enabled: false, email_verification_required: false, authenticated: true,
    multitenancy_enabled: true, user: null,
  })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Backtest /></QueryClientProvider></MemoryRouter>,
  ))
  return { host, cleanup: async () => {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  } }
}

async function settle() {
  for (let i = 0; i < 8; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('waits for server defaults before mounting the strategy form', async () => {
  let resolve!: (value: unknown) => void
  vi.spyOn(api, 'businessInitializationDefaults').mockImplementation(() => new Promise(r => { resolve = r }) as any)
  const { host, cleanup } = await setup()
  try {
    expect(host.querySelector('[data-testid="strategy-defaults"]')).toBeNull()
    await act(async () => resolve({ snapshot_id: 'fixture', backtest: { last: { initialCapital: '1000000' } } }))
    await settle()
    expect(host.querySelector('[data-testid="strategy-defaults"]')?.textContent).toBe('1000000')
  } finally { await cleanup() }
})

it('shows a retry action after a defaults read failure', async () => {
  vi.spyOn(api, 'businessInitializationDefaults').mockRejectedValueOnce(new Error('unavailable'))
    .mockResolvedValue({ snapshot_id: null, backtest: null })
  const { host, cleanup } = await setup()
  try {
    await settle()
    expect(host.querySelector('[data-testid="strategy-defaults"]')).toBeNull()
    const retry = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('重试'))
    expect(retry).toBeDefined()
    await act(async () => retry!.click())
    await settle()
    expect(host.querySelector('[data-testid="strategy-defaults"]')?.textContent).toBe('empty')
  } finally { await cleanup() }
})
