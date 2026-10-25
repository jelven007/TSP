// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import type { CustomSourceConfig } from '@/lib/api'
import { DataSourceEditor } from './DataSourceEditor'

const calls = vi.hoisted(() => ({
  save: vi.fn(async () => ({})),
  test: vi.fn(async () => ({ rows: 1, columns: ['symbol'], preview: [] })),
  read: vi.fn(async () => ({})),
}))
vi.mock('@/lib/api', () => ({ api: {
  saveDataSource: calls.save, testDataSource: calls.test, dataSource: calls.read,
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('framer-motion', () => ({
  motion: { div: ({ children }: { children: React.ReactNode }) => <div>{children}</div> },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let host: HTMLDivElement
let root: Root
let client: QueryClient
const history = (kind: 'daily' | 'minute') => ({
  url: `https://example.test/${kind}`, method: 'POST', response_path: 'data',
  field_map: Object.fromEntries([
    'symbol', kind === 'daily' ? 'date' : 'datetime', 'open', 'high', 'low', 'close', 'volume', 'amount',
  ].map(field => [field, field])),
})

afterEach(() => {
  act(() => root?.unmount())
  client?.clear()
  host?.remove()
  vi.clearAllMocks()
})

function render(initial?: CustomSourceConfig) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  act(() => root.render(
    <QueryClientProvider client={client}>
      <DataSourceEditor initial={initial} onCancel={() => {}} onSaved={() => {}} activeName="rustdx"
        onActivate={() => {}} />
    </QueryClientProvider>,
  ))
}

function button(label: string) {
  return [...host.querySelectorAll('button')].find(element => element.textContent?.trim() === label)!
}

function group(label: string) {
  return host.querySelector<HTMLElement>(`[role="group"][aria-label="数据集 ${label}"]`)!
}

function input(placeholder: string, value: string) {
  const element = host.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('lists realtime, Kline, adjustment, company and financial datasets in the requested order', () => {
  render()
  expect([...host.querySelectorAll('[role="group"]')].map(node => node.getAttribute('aria-label')))
    .toEqual(['数据集 实时', '数据集 K线', '数据集 除权', '数据集 公司', '数据集 财务'])
  expect(host.querySelector('button button')).toBeNull()
  expect(host.textContent).not.toContain('回退 TF')
  expect(host.textContent).not.toContain('回退 TickFlow')
})

it('opens both history configurations through the single Kline group and preserves their values', async () => {
  render({
    name: 'existing', display_name: '已有源', auth: { type: 'none' },
    datasets: { daily: history('daily'), minute: { ...history('minute'), freq_param: 'period' } },
  })
  act(() => group('K线').querySelector('button')!.click())
  expect(host.querySelector<HTMLInputElement>('input[placeholder^="https://my.api"]')?.value)
    .toBe('https://example.test/daily')
  act(() => button('分钟K').click())
  expect(host.querySelector<HTMLInputElement>('input[placeholder^="https://my.api"]')?.value)
    .toBe('https://example.test/minute')
  await act(async () => button('保存').click())
  expect(calls.save).toHaveBeenCalledWith(expect.objectContaining({
    datasets: {
      daily: history('daily'),
      minute: { ...history('minute'), freq_param: 'period' },
    },
  }))
})

it('prefills financial field mappings and retains drafts when switching categories', () => {
  render()
  act(() => group('财务').querySelector('button')!.click())
  act(() => button('启用财务').click())
  const targets = [...host.querySelectorAll<HTMLSelectElement>('select')]
    .filter(select => select.querySelector('option[value="symbol"]'))
  expect(targets.map(select => select.value)).toEqual(expect.arrayContaining([
    'symbol', 'period_end', 'announce_date', 'eps_basic', 'roe', 'revenue_yoy', 'net_income_yoy',
    'net_income', 'total_assets', 'net_operating_cash_flow', 'total_shares', 'float_shares',
  ]))
  input('外部字段名', 'ts_code')
  act(() => group('除权').querySelector('button')!.click())
  act(() => group('财务').querySelector('button')!.click())
  expect(host.querySelector<HTMLInputElement>('input[placeholder="外部字段名"]')?.value).toBe('ts_code')
  expect(host.textContent).toContain('table')
})

it('saves financial mappings without persisting unfinished rows', async () => {
  render()
  input('my_tushare', 'finance_source')
  act(() => group('财务').querySelector('button')!.click())
  act(() => button('启用财务').click())
  input('https://my.api/daily', 'https://example.test/financial')
  input('外部字段名', 'ts_code')
  await act(async () => button('测试').click())
  expect(calls.test).toHaveBeenCalledWith('finance_source', 'financial', ['000001.SZ', '600000.SH'],
    expect.objectContaining({ datasets: { financial: expect.objectContaining({ field_map: { ts_code: 'symbol' } }) } }))
  await act(async () => button('保存').click())
  expect(calls.save).toHaveBeenCalledWith(expect.objectContaining({
    datasets: { financial: expect.objectContaining({ field_map: { ts_code: 'symbol' } }) },
  }))
  expect(calls.read).not.toHaveBeenCalled()
})

it('retains a historical source with only one Kline dataset until the user enables the other', async () => {
  render({
    name: 'daily_only', display_name: '仅日K', auth: { type: 'none' },
    datasets: { daily: history('daily') },
  })
  expect(group('K线').textContent).toContain('部分配置')
  await act(async () => button('保存').click())
  expect(calls.save).toHaveBeenCalledWith(expect.objectContaining({
    datasets: { daily: history('daily') },
  }))
})

it('enables both Kline datasets with the group switch and keeps separate mappings', () => {
  render()
  act(() => group('K线').querySelectorAll('button')[1].click())
  act(() => group('K线').querySelector('button')!.click())
  expect(host.textContent).toContain('K线')
  expect(group('K线').textContent).toContain('已配置')
  expect(host.querySelector<HTMLSelectElement>('select option[value="date"]')).not.toBeNull()
  act(() => button('分钟K').click())
  expect(host.querySelector<HTMLSelectElement>('select option[value="datetime"]')).not.toBeNull()
  act(() => group('K线').querySelectorAll('button')[1].click())
  expect(group('K线').textContent).toContain('未启用')
})

it('edits and preserves the daily period parameter required by weekly Klines', async () => {
  render({
    name: 'weekly_source', display_name: '周K源', auth: { type: 'none' },
    datasets: { daily: history('daily'), minute: history('minute') },
  })
  act(() => group('K线').querySelector('button')!.click())
  act(() => button('请求参数字段映射').click())
  expect(host.textContent).toContain('周期参数')
  input('period', 'frequency')
  await act(async () => button('保存').click())
  expect(calls.save).toHaveBeenCalledWith(expect.objectContaining({
    datasets: expect.objectContaining({
      daily: expect.objectContaining({ freq_param: 'frequency' }),
    }),
  }))
})

it('does not save an enabled financial dataset with an empty URL', async () => {
  render()
  input('my_tushare', 'finance_source')
  act(() => group('财务').querySelectorAll('button')[1].click())
  await act(async () => button('保存').click())
  expect(calls.save).not.toHaveBeenCalled()
})
