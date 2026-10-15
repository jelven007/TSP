// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Paper } from './Paper'

const calls = vi.hoisted(() => ({
  arena: vi.fn(async (_body: unknown) => ({ created: [{ account: 'new' }] })),
  rule: vi.fn(async (_body: unknown, _account: string) => ({ rule: {} })),
}))
vi.mock('@/lib/api', () => ({
  api: {
    paperCompare: async () => ({ accounts: [{
      account: 'test', name: '测试账户', status: 'active', initial_cash: 100000,
      total: 100000, cash: 100000, market_value: 0, total_pnl: 0, pnl_pct: 0,
      rounds: 0, win_rate: 0, profit_loss_ratio: null, nav: [],
      auto_rules: calls.rule.mock.calls.map(([body]) => body),
    }] }),
    screenerStrategies: async () => ({ presets: [
      { id: 's-one', name: '策略一' }, { id: 's-two', name: '策略二' },
    ] }),
    monitorRulesList: async () => ({ rules: [{ id: 'r-one', name: '价格规则', enabled: true }] }),
    paperArenaCreate: calls.arena,
    paperAutoRuleCreate: calls.rule,
    paperOverview: async () => ({
      initialized: true, status: 'active', initial_cash: 100000,
      cash: 100000, market_value: 0, total: 100000, total_pnl: 0, holdings: [],
    }),
    paperOrders: async () => ({ orders: [] }),
    paperTrades: async () => ({ fills: [] }),
    paperNav: async () => ({ nav: [] }),
    paperStats: async () => ({
      rounds: 0, win_rate: 0, profit_loss_ratio: null, avg_holding_days: 0,
      realized_pnl: 0, max_drawdown: null,
    }),
    paperAutoRules: async () => ({ rules: [] }),
    backtestCandidates: async () => ({ candidates: [] }),
    instrumentSearch: async () => ({ results: [] }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useQuoteStatus: () => ({ data: { is_trading_hours: false } }),
}))
vi.mock('@/components/PageHeader', () => ({
  PageHeader: ({ title, right }: { title: string; right: React.ReactNode }) => <div>{title}{right}</div>,
}))
vi.mock('@/lib/accountSession', () => ({ accountStorage: window.localStorage }))

let root: Root
let host: HTMLDivElement
let client: QueryClient

async function settle() {
  for (let i = 0; i < 4; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.arena.mockClear()
  calls.rule.mockClear()
  window.localStorage.clear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  await act(async () => root.render(<QueryClientProvider client={client}><Paper /></QueryClientProvider>))
  await settle()
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

function button(text: string, scope: ParentNode = document) {
  const found = [...scope.querySelectorAll<HTMLButtonElement>('button')]
    .find(item => item.textContent?.trim() === text)
  expect(found, `button: ${text}`).toBeDefined()
  return found!
}

async function click(text: string, scope: ParentNode = document) {
  await act(async () => button(text, scope).click())
  await settle()
}

async function input(label: string, value: string, scope: ParentNode = document) {
  const found = [...scope.querySelectorAll('label')].find(item => item.textContent === label)
  expect(found, `label: ${label}`).toBeDefined()
  const field = found!.parentElement!.querySelector('input')!
  expect(field).not.toBeNull()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
}

async function batch() {
  await click('批量创建账户')
  await click('全部策略一键加入 (2)')
  return document.querySelector<HTMLElement>('[role="dialog"]')!
}

it('submits auto direction and a shared position limit for strategy accounts', async () => {
  const dialog = await batch()
  await input('最大持仓标的数 (0 不限)', '-1', dialog)
  expect(button('创建 2 个账户', dialog).disabled).toBe(true)
  await input('最大持仓标的数 (0 不限)', '3', dialog)
  await click('创建 2 个账户', dialog)
  const body = calls.arena.mock.calls[0][0] as { sources: { side: string; max_positions: number }[] }
  expect(body.sources).toHaveLength(2)
  expect(body.sources.every(row => row.side === 'auto' && row.max_positions === 3)).toBe(true)
})

it('requires an explicit fixed direction when batch sources include a monitor rule', async () => {
  const dialog = await batch()
  await click('监控规则', dialog)
  await click('添加', dialog)
  expect(button('创建 3 个账户', dialog).disabled).toBe(true)
  expect(button('自动', dialog).disabled).toBe(true)
  await click('买入', dialog)
  await click('创建 3 个账户', dialog)
  const body = calls.arena.mock.calls[0][0] as { sources: { side: string; match_kind: string }[] }
  expect(body.sources.map(row => row.side)).toEqual(['buy', 'buy', 'buy'])
  expect(body.sources[2].match_kind).toBe('rule')
})

it('creates a single strategy rule with auto direction and rejects a fractional limit', async () => {
  const row = [...host.querySelectorAll('tbody tr')].find(item => item.textContent?.includes('测试账户'))!
  await act(async () => (row as HTMLElement).click())
  await settle()
  await click('新建规则')
  await input('规则名称', '自动全平')
  await input('策略 (输入中文名 / ID 联想, 留空聚焦看全部)', 's-one')
  await input('最大持仓标的数 (0 不限)', '1.5')
  expect(button('创建规则').disabled).toBe(true)
  await input('最大持仓标的数 (0 不限)', '2')
  await input('单股仓位上限 %（0 不限）', '101')
  expect(button('创建规则').disabled).toBe(true)
  await input('单股仓位上限 %（0 不限）', '25')
  await input('账户总仓位上限 %（0 不限）', '80')
  const risk = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  expect(risk).not.toBeNull()
  await act(async () => risk.click())
  await settle()
  await click('创建规则')
  expect(calls.rule).toHaveBeenCalledWith(expect.objectContaining({
    side: 'auto', match_kind: 'strategy', max_positions: 2,
    candidate_priority: 'score', max_symbol_exposure_pct: 25,
    max_total_exposure_pct: 80, risk_follow: true,
  }), 'test')
  expect([...host.querySelectorAll<HTMLElement>('span[title]')].some(item =>
    item.title.includes('自动方向，退出全平') && item.title.includes('持仓上限 2只'),
  )).toBe(true)
})
