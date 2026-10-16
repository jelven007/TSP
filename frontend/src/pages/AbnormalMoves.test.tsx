// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { confirmAccount } from '@/lib/accountSession'
import { storage } from '@/lib/storage'
import { AbnormalMoves } from './AbnormalMoves'

vi.mock('@/lib/api', () => ({
  api: {
    abnormalIntraday: vi.fn(),
    auctionSnapshot: vi.fn(),
    auctionBenchmark: vi.fn(),
    abnormalOverview: vi.fn(),
    watchlistList: vi.fn(),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useQuoteStatus: () => ({ data: { is_trading_hours: true } }),
}))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: () => null,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  confirmAccount({
    auth_required: false, authenticated: true, configured: false, has_users: false,
    legacy_migration_required: false, registration_enabled: false, email_verification_required: false,
    user: null, multitenancy_enabled: false,
  })
  localStorage.clear()
  vi.resetAllMocks()
  // 空数据也应正常加载；不能用“未开启”掩盖尚无历史数据。
  vi.mocked(api.abnormalIntraday).mockResolvedValue({
    rows: [], counts: {}, cache_date: null,
  })
  vi.mocked(api.auctionSnapshot).mockResolvedValue({
    state: 'not_collected', rows: [], available_dates: [],
  })
  vi.mocked(api.auctionBenchmark).mockResolvedValue({
    state: 'no_data', items: [],
  })
  vi.mocked(api.abnormalOverview).mockResolvedValue({
    rows: [], rules: [], counts: { triggered: 0, edge: 0, watch: 0 },
    bench_rt_pct: 0, asof: 1, cache_date: null, includes_today: false,
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  localStorage.clear()
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function renderPage() {
  await act(async () => root.render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}><AbnormalMoves /></QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

async function clickButton(text: string) {
  const button = [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find(el => el.textContent === text)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}

it('loads all three abnormal views on first use without enabling a switch', async () => {
  await renderPage()
  expect(api.abnormalIntraday).toHaveBeenCalledWith(500)

  await clickButton('竞价异动')
  expect(api.auctionSnapshot).toHaveBeenCalledWith(undefined)
  expect(api.auctionBenchmark).toHaveBeenCalledOnce()
  expect(host.textContent).toContain('尚无可用竞价历史')

  await clickButton('偏移异动')
  expect(api.abnormalOverview).toHaveBeenCalledWith(0.5, 300)
  expect(host.querySelector('[role="switch"]')?.getAttribute('aria-checked')).toBe('true')
  expect(host.textContent).not.toContain('监控未开启')
})

it('preserves an explicit disabled setting and allows enabling it again', async () => {
  storage.abnormalEnabled.set(false)
  await renderPage()
  await clickButton('偏移异动')
  expect(api.abnormalOverview).not.toHaveBeenCalled()
  expect(host.textContent).toContain('监控未开启')

  await clickButton('开启监控')
  expect(storage.abnormalEnabled.get(false)).toBe(true)
  expect(api.abnormalOverview).toHaveBeenCalledWith(0.5, 300)
})

it('remembers a manual disable across remounts and keeps the last result', async () => {
  await renderPage()
  await clickButton('偏移异动')
  const toggle = host.querySelector<HTMLButtonElement>('[role="switch"]')
  expect(toggle).not.toBeNull()
  await act(async () => toggle!.click())
  expect(storage.abnormalEnabled.get(true)).toBe(false)
  expect(host.textContent).toContain('已暂停计算')

  await act(async () => root.render(null))
  client.clear()
  vi.mocked(api.abnormalOverview).mockClear()
  await renderPage()
  await clickButton('偏移异动')
  expect(api.abnormalOverview).not.toHaveBeenCalled()
  expect(host.textContent).toContain('已暂停计算')
})
