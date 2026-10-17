// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PaperCorporateActions } from './PaperCorporateActions'

const mocks = vi.hoisted(() => ({ status: vi.fn(), subscribe: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  paperCorporateActions: mocks.status, paperSubscribeRights: mocks.subscribe,
} }))

let root: Root
let node: HTMLDivElement
let client: QueryClient
const event = {
  id: 'rights-one', symbol: '600519.SH', ex_date: '2026-09-22', record_date: '2026-09-21',
  cash_per_share: 0, bonus_per_share: 0, rights_per_share: .2, rights_price: 5,
  base_qty: 1000, subscribed_qty: 0, stages: ['register'], can_subscribe: true,
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  node = document.createElement('div')
  document.body.append(node)
  root = createRoot(node)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  mocks.status.mockResolvedValue({ mode: 'events', events: [event], blocked: [] })
  mocks.subscribe.mockResolvedValue({})
})
afterEach(() => {
  act(() => root.unmount())
  client.clear()
  node.remove()
})
async function render() {
  await act(async () => root.render(
    <QueryClientProvider client={client}><PaperCorporateActions account="mine" receivables={1000} /></QueryClientProvider>,
  ))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
}

it('shows cash, dates and explicit payment before subscribing', async () => {
  await render()
  expect(node.textContent).toContain('应收资产 ¥1000.00')
  expect(node.textContent).toContain('缴款 ¥1000.00')
  expect(node.textContent).toContain('个人红利税未模拟')
  expect(mocks.subscribe).not.toHaveBeenCalled()
  await act(async () => node.querySelector<HTMLButtonElement>('button')!.click())
  expect(mocks.subscribe).toHaveBeenCalledExactlyOnceWith('mine', 'rights-one', 200)
})

it('does not offer payment for missing evidence', async () => {
  mocks.status.mockResolvedValue({ mode: 'events', events: [{ ...event, reason: '日期未知', can_subscribe: false }], blocked: [] })
  await render()
  expect(node.querySelector('button')).toBeNull()
  expect(node.textContent).toContain('日期证据不足')
})

it('explains legacy accounts without exposing an event subscription', async () => {
  mocks.status.mockResolvedValue({ mode: 'legacy_factor', events: [], blocked: [] })
  await render()
  expect(node.textContent).toContain('存量账户沿用价格因子')
  expect(node.querySelector('button')).toBeNull()
})

it('shows load errors', async () => {
  mocks.status.mockRejectedValue(new Error('offline'))
  await render()
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('offline')
})
