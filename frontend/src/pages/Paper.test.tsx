// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Paper } from './Paper'
import { QK } from '@/lib/queryKeys'

const calls = vi.hoisted(() => ({
  arena: vi.fn(async (_body: unknown) => ({ created: [{ account: 'new' }] })),
  rule: vi.fn(async (_body: unknown, _account: string) => ({ rule: {} })),
  edit: vi.fn(),
  delete: vi.fn(),
  state: { exists: true, name: '测试账户', cash: 200000, editable: true },
}))
vi.mock('@/lib/api', () => ({
  api: {
    paperCompare: async () => ({ accounts: calls.state.exists ? [{
      account: 'test', name: calls.state.name, status: 'active', initial_cash: calls.state.cash,
      total: calls.state.cash, cash: calls.state.cash, market_value: 0, total_pnl: 0, pnl_pct: 0,
      rounds: 0, win_rate: 0, profit_loss_ratio: null, nav: [],
      auto_rules: calls.rule.mock.calls.map(([body]) => body),
    }] : [] }),
    screenerStrategies: async () => ({ presets: [
      { id: 's-one', name: '策略一' }, { id: 's-two', name: '策略二' },
    ] }),
    monitorRulesList: async () => ({ rules: [{ id: 'r-one', name: '价格规则', enabled: true }] }),
    paperArenaCreate: calls.arena,
    paperAutoRuleCreate: calls.rule,
    paperUpdateAccount: calls.edit,
    paperDeleteAccount: calls.delete,
    paperOverview: async () => ({
      initialized: true, account_name: calls.state.name, status: 'active', initial_cash: calls.state.cash,
      initial_cash_editable: calls.state.editable,
      fees: { commission_pct: .00025, stamp_tax_pct: .001, slippage_bps: 5 },
      cash: calls.state.cash, market_value: 0, total: calls.state.cash, total_pnl: 0, holdings: [],
    }),
    paperOrders: async () => ({ orders: [] }),
    paperTrades: async () => ({ fills: [] }),
    paperNav: async () => ({ nav: [] }),
    paperStats: async () => ({
      rounds: 0, win_rate: 0, profit_loss_ratio: null, avg_holding_days: 0,
      realized_pnl: 0, max_drawdown: null,
    }),
    paperAutoRules: async () => ({ rules: [] }),
    paperAutoSkips: async () => ({ skips: [] }),
    paperCorporateActions: async () => ({ mode: 'events', events: [], blocked: [] }),
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
  Object.assign(calls.state, { exists: true, name: '测试账户', cash: 200000, editable: true })
  calls.edit.mockReset().mockImplementation(async (body: { name: string; initial_cash?: number }) => {
    calls.state.name = body.name
    if (body.initial_cash != null) calls.state.cash = body.initial_cash
    return { account: { id: 'test', name: calls.state.name } }
  })
  calls.delete.mockReset().mockImplementation(async () => {
    calls.state.exists = false
    return { ok: true }
  })
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

async function expand() {
  const row = [...host.querySelectorAll('tbody tr')].find(item => item.textContent?.includes(calls.state.name))!
  await act(async () => (row as HTMLElement).click())
  await settle()
}

it('edits the account and refreshes the comparison and expanded overview', async () => {
  await expand()
  await click('修改账户')
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!
  await input('账户名称', '修改后的账户', dialog)
  await input('初始资金 (元)', '500000', dialog)
  await input('佣金 (万)', '3', dialog)
  await click('保存', dialog)
  expect(calls.edit).toHaveBeenCalledWith(expect.objectContaining({
    name: '修改后的账户', initial_cash: 500000, commission_pct: .0003,
  }), 'test')
  expect(host.textContent).toContain('修改后的账户')
  expect(host.textContent).not.toContain('测试账户')
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})

it('locks initial capital after activity and keeps failed edits open for retry', async () => {
  calls.state.editable = false
  calls.edit.mockRejectedValueOnce(new Error('修改失败'))
  await expand()
  await click('修改账户')
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!
  const capital = [...dialog.querySelectorAll<HTMLInputElement>('input')].find(field => field.value === '200000')!
  expect(capital.disabled).toBe(true)
  await input('账户名称', '重试名称', dialog)
  await click('保存', dialog)
  expect(dialog.textContent).toContain('修改失败')
  expect(calls.edit.mock.calls[0][0]).not.toHaveProperty('initial_cash')
  await click('取消', dialog)
  await click('修改账户')
  expect([...document.querySelectorAll<HTMLInputElement>('[role="dialog"] input')].some(field => field.value === '测试账户')).toBe(true)
})

it('requires confirmation and removes the last account and only its cached details', async () => {
  await expand()
  client.setQueryData(QK.paperNav('other'), { nav: [{ date: 'fixture', nav: 123 }] })
  await click('删除账户')
  expect(calls.delete).not.toHaveBeenCalled()
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!
  expect(dialog.textContent).toContain('成交')
  expect(dialog.textContent).toContain('自动跟单')
  await click('取消', dialog)
  expect(calls.delete).not.toHaveBeenCalled()
  await click('删除账户')
  await click('确认删除')
  expect(calls.delete).toHaveBeenCalledWith('test')
  expect(host.textContent).toContain('还没有对比账户')
  expect(host.textContent).not.toContain('测试账户')
  expect(client.getQueryData(QK.paperOverview('test'))).toBeUndefined()
  expect(client.getQueryData(QK.paperNav('other'))).toEqual({ nav: [{ date: 'fixture', nav: 123 }] })
})

it('retains the account and confirmation when deletion fails', async () => {
  calls.delete.mockRejectedValueOnce(new Error('删除失败，请重试'))
  await expand()
  await click('删除账户')
  await click('确认删除')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('删除失败，请重试')
  expect(host.textContent).toContain('测试账户')
  expect(client.getQueryData(QK.paperOverview('test'))).toBeDefined()
})

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
