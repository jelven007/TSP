// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PaperAutoRule } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Paper } from './Paper'

const calls = vi.hoisted(() => ({
  arena: vi.fn(async (_body: unknown) => ({ created: [{ account: 'new' }] })),
  rule: vi.fn(async (_body: unknown, _account: string) => ({ rule: {} })),
  edit: vi.fn(),
  delete: vi.fn(),
  state: { exists: true, name: '测试账户', cash: 200000, editable: true },
  update: vi.fn(async (_id: string, _body: unknown, _account: string) => ({ rule: {} })),
  settings: vi.fn(async (_body: unknown, _account: string) => ({ account: {} })),
  rules: [] as PaperAutoRule[],
}))
vi.mock('@/lib/api', () => ({
  api: {
    paperCompare: async () => ({ accounts: calls.state.exists ? [{
      account: 'test', name: calls.state.name, status: 'active', initial_cash: calls.state.cash,
      total: calls.state.cash, cash: calls.state.cash, market_value: 0, total_pnl: 0, pnl_pct: 0,
      rounds: 0, win_rate: 0, profit_loss_ratio: null, nav: [],
      auto_rules: [...calls.rules, ...calls.rule.mock.calls.map(([body]) => body)],
    }] : [] }),
    screenerStrategies: async () => ({ presets: [
      { id: 's-one', name: '策略一' }, { id: 's-two', name: '策略二' },
    ] }),
    monitorRulesList: async () => ({ rules: [{ id: 'r-one', name: '价格规则', enabled: true }] }),
    strategyGet: async (id: string) => ({
      id, name: id === 's-one' ? '策略一' : '策略二', execution_backend: 'polars_expr',
      stop_loss: 0.05, take_profit: 0.1, max_hold_days: 10,
      entry_risk: { max_entry_premium_pct: 0.02 }, basic_filter: {}, scoring: {},
    }),
    paperSettings: calls.settings,
    paperArenaCreate: calls.arena,
    paperAutoRuleCreate: calls.rule,
    paperAutoRuleUpdate: calls.update,
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
    paperAutoRules: async () => ({ rules: calls.rules }),
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
  calls.update.mockReset()
  calls.settings.mockClear()
  calls.rules = []
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
  await input('佣金（万分比，双边）', '3', dialog)
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
  const risk = document.querySelector<HTMLElement>('[role="dialog"]')!.querySelector<HTMLInputElement>('input[type="checkbox"]')!
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

const savedRule: PaperAutoRule = {
  id: 'arule_saved', name: '已停用规则', match_kind: 'strategy', match_id: 's-two',
  side: 'auto', size_mode: 'pct_equity', size_value: 15, order_type: 'close',
  cooldown_days: 7, max_positions: 4, candidate_priority: 'symbol',
  max_symbol_exposure_pct: 20, max_total_exposure_pct: 70, risk_follow: true,
  enabled: false, created_at: '2026-11-01T10:00:00+08:00',
}

async function openRule(rule = savedRule) {
  calls.rules = [{ ...rule }]
  const row = [...host.querySelectorAll('tbody tr')].find(item => item.textContent?.includes('测试账户'))!
  await act(async () => (row as HTMLElement).click())
  await settle()
  await click('修改')
}

function fieldValue(label: string) {
  const found = [...document.querySelectorAll('label')].find(item => item.textContent === label)!
  return found.parentElement!.querySelector('input, select') as HTMLInputElement | HTMLSelectElement
}

it('prefills saved rule fields and updates the same account rule without creating a new one', async () => {
  const invalidated = vi.spyOn(client, 'invalidateQueries')
  await openRule()
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('修改自动跟单规则')
  for (const [label, value] of [
    ['规则名称', '已停用规则'], ['策略 (输入中文名 / ID 联想, 留空聚焦看全部)', 's-two'],
    ['买入占权益 %', '15'], ['买入冷却天数 (同标的)', '7'],
    ['最大持仓标的数 (0 不限)', '4'], ['候选买入顺序', 'symbol'],
    ['单股仓位上限 %（0 不限）', '20'], ['账户总仓位上限 %（0 不限）', '70'],
  ]) expect(fieldValue(label).value, label).toBe(value)
  expect(button('自动').getAttribute('aria-pressed')).toBe('true')
  expect(document.querySelector<HTMLElement>('[role="dialog"]')!.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(true)
  await input('规则名称', '修改后的规则')
  await input('买入占权益 %', '25')
  calls.update.mockImplementationOnce(async (id, body) => {
    calls.rules = [{ ...savedRule, ...body as Partial<PaperAutoRule>, id }]
    return { rule: calls.rules[0] }
  })
  await click('保存修改')
  expect(calls.update).toHaveBeenCalledWith('arule_saved', {
    name: '修改后的规则', match_kind: 'strategy', match_id: 's-two', side: 'auto',
    size_mode: 'pct_equity', size_value: 25, order_type: 'close', buy_order_type: 'close',
    sell_order_type: 'close', cooldown_days: 7,
    max_positions: 4, candidate_priority: 'symbol', max_symbol_exposure_pct: 20,
    max_total_exposure_pct: 70, risk_follow: true,
  }, 'test')
  expect(calls.rule).not.toHaveBeenCalled()
  expect(host.textContent).toContain('修改后的规则')
  expect(button('启用')).toBeDefined() // Saving a disabled rule does not enable it.
  expect(invalidated).toHaveBeenCalledWith(expect.objectContaining({ queryKey: QK.paperAutoRules('test') }))
})

it('keeps S02 minute execution and submits score ordering without extra cooldown', async () => {
  await openRule({
    ...savedRule, match_id: 'custom_mvxxcp88', execution_mode: 's02_minute',
    candidate_priority: 'score', cooldown_days: 0,
  })
  expect(fieldValue('策略成交方式').value).toBe('s02_minute')
  expect(fieldValue('买入冷却天数 (同标的)').disabled).toBe(true)
  expect(fieldValue('候选买入顺序').disabled).toBe(true)
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('五个交易分钟')
  expect(document.querySelector('#auto-rule-buy-type')).toBeNull()
  calls.update.mockResolvedValueOnce({ rule: {} })
  await click('保存修改')
  expect(calls.update).toHaveBeenCalledWith('arule_saved', expect.objectContaining({
    execution_mode: 's02_minute', cooldown_days: 0, candidate_priority: 'score',
    match_id: 'custom_mvxxcp88', side: 'auto',
  }), 'test')
})

it('cancels editing without saving and resets the draft when editing another rule', async () => {
  await openRule()
  await input('规则名称', '未保存的改名')
  await click('取消')
  expect(calls.update).not.toHaveBeenCalled()
  expect(host.textContent).toContain(savedRule.name)
  calls.rules = [{ ...savedRule, id: 'arule_other', name: '另一条规则', size_value: 9 }]
  await act(async () => { await client.invalidateQueries({ queryKey: ['paper'] }) })
  await settle()
  await click('修改')
  expect(fieldValue('规则名称').value).toBe('另一条规则')
  expect(fieldValue('买入占权益 %').value).toBe('9')
})

it('retains the edit draft on save failure and permits retry', async () => {
  await openRule()
  await input('规则名称', '重试规则')
  calls.update.mockRejectedValueOnce(new Error('保存失败，请重试'))
  await click('保存修改')
  expect(document.body.textContent).toContain('保存失败，请重试')
  expect(fieldValue('规则名称').value).toBe('重试规则')
  expect(button('保存修改').disabled).toBe(false)
  calls.update.mockResolvedValueOnce({ rule: {} })
  await click('保存修改')
  expect(calls.update).toHaveBeenCalledTimes(2)
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})

it('keeps legacy defaults and validates changed source, direction and percentage', async () => {
  const legacy = { ...savedRule }
  delete legacy.candidate_priority
  delete legacy.risk_follow
  delete legacy.max_positions
  delete legacy.max_symbol_exposure_pct
  delete legacy.max_total_exposure_pct
  await openRule(legacy)
  expect(fieldValue('候选买入顺序').value).toBe('event')
  expect(fieldValue('最大持仓标的数 (0 不限)').value).toBe('0')
  await input('买入占权益 %', '101')
  expect(button('保存修改').disabled).toBe(true)
  await input('买入占权益 %', '20')
  await click('跟监控规则')
  expect(button('自动').disabled).toBe(true)
  expect(button('买入', document.querySelector('fieldset')!).getAttribute('aria-pressed')).toBe('true')
  expect(button('保存修改').disabled).toBe(true) // Source cleared until explicitly selected.
  await input('监控规则 (输入名称 / ID 联想)', 'r-one')
  calls.update.mockResolvedValueOnce({ rule: {} })
  await click('保存修改')
  expect(calls.update).toHaveBeenCalledWith('arule_saved', expect.objectContaining({
    match_kind: 'rule', match_id: 'r-one', side: 'buy', risk_follow: false, candidate_priority: 'event',
  }), 'test')
})

it('disables form controls and duplicate submits while the edit is being saved', async () => {
  await openRule()
  let finish!: (value: { rule: Record<string, unknown> }) => void
  calls.update.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await click('保存修改')
  expect(button('保存中…').disabled).toBe(true)
  expect(button('取消').disabled).toBe(true)
  expect(fieldValue('规则名称').matches(':disabled')).toBe(true)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  await act(async () => finish({ rule: {} }))
  await settle()
  expect(calls.update).toHaveBeenCalledTimes(1)
})

it('keeps amount and percentage drafts separate and limits pool order to strategy sources', async () => {
  await openRule({ ...savedRule, size_mode: 'fixed_amount', size_value: 10000 })
  for (const group of ['跟单对象', '买入与仓位', '成交方式', '策略风控', '账户费用']) {
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain(group)
  }
  await click('权益 %')
  expect(fieldValue('买入占权益 %').value).toBe('')
  expect(button('保存修改').disabled).toBe(true)
  await input('买入占权益 %', '16')
  await click('固定金额')
  expect(fieldValue('买入金额 (元)').value).toBe('10000')
  await click('权益 %')
  expect(fieldValue('买入占权益 %').value).toBe('16')
  const priority = fieldValue('候选买入顺序') as HTMLSelectElement
  const pool = [...priority.options].find(option => option.value === 'pool')!
  expect(pool?.textContent).toBe('候选股票池默认顺序')
  expect(pool.disabled).toBe(false)
  await act(async () => { priority.value = 'pool'; priority.dispatchEvent(new Event('change', { bubbles: true })) })
  await click('跟监控规则')
  expect(pool.disabled).toBe(true)
  expect(priority.value).toBe('event')
})

it('previews stored backtest differences, imports drafts and saves fees independently', async () => {
  window.localStorage.setItem('strategy-backtest-last', JSON.stringify({
    defaultsVersion: 2, selectedStrategy: 's-one', maxPositions: '5', maxExposure: '80',
    positionSizing: 'equal', mode: 'position', entryFill: 'open_t+1', exitFill: 'close_t',
    fees: '2.5', stampTax: '', minimumCommission: '3', transferFee: '0.1', slippage: '7',
  }))
  await openRule()
  await click('参考回测参数')
  expect(document.body.textContent).toContain('5分钟容量')
  expect(document.body.textContent).toContain('策略一')
  expect(calls.update).not.toHaveBeenCalled()
  await click('填入草稿')
  expect(fieldValue('买入占权益 %').value).toBe('16')
  expect(fieldValue('最大持仓标的数 (0 不限)').value).toBe('5')
  expect(calls.settings).not.toHaveBeenCalled()
  expect(calls.update).not.toHaveBeenCalled()
  await click('修改账户费用')
  await click('保存账户费用')
  expect(calls.settings).toHaveBeenCalledWith(expect.objectContaining({
    commission_pct: 0.00025, min_commission: 3, transfer_fee_pct: 0.00001,
    stamp_tax_mode: 'historical', slippage_bps: 7,
  }), 'test')
  expect(calls.update).not.toHaveBeenCalled()
  const buy = fieldValue('建仓订单类型') as HTMLSelectElement
  await act(async () => { buy.value = 'market'; buy.dispatchEvent(new Event('change', { bubbles: true })) })
  calls.update.mockResolvedValueOnce({ rule: {} })
  await click('保存修改')
  expect(calls.update).toHaveBeenCalledWith(savedRule.id, expect.objectContaining({
    buy_order_type: 'market', sell_order_type: 'close', max_positions: 5, size_value: 16,
  }), 'test')
})
