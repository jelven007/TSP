// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsDataSourcesPanel } from './DataSources'
import { QK } from '@/lib/queryKeys'
import type { CapabilityMatrix, DataSourcesResponse, ProviderField } from '@/lib/api'

const fixtures = vi.hoisted(() => ({
  update: vi.fn(async (_changes: object) => ({})),
  available: false,
}))
vi.mock('@/lib/api', () => ({
  api: {
    dataSources: async () => ({
      builtin: [{ name: 'tickflow', display_name: 'TickFlow', datasets: ['daily'] }],
      custom: [{ name: 'local', display_name: '本地数据源', datasets: ['daily'] }],
      errors: [],
      plugins: [
        { name: 'other', display_name: '其他插件', runtime: 'none', datasets: ['daily'], available: true },
        { name: 'fuyao', display_name: 'fuyao', runtime: 'none', datasets: ['realtime'], available: true },
        {
          name: 'rustdx', display_name: 'rustdx', runtime: 'none',
          datasets: ['daily'], available: fixtures.available,
          status: '缺少 rustdx 原生桥接',
          install_hint: '源码环境执行 uv sync --extra rustdx --frozen',
        },
      ],
    }),
    capabilityMatrix: async () => ({
      capabilities: [{
        id: 'daily', label: '日K', field: 'daily_data_provider',
        current: 'tickflow', effective: 'tickflow', effective_display: 'TickFlow',
        usable: false,
        candidates: [
          { name: 'tickflow', display: 'TickFlow', available: true },
          { name: 'fuyao', display: 'fuyao', available: true },
          { name: 'other', display: '其他插件', available: true },
        ],
        pending: [{ name: 'rustdx', display: 'rustdx', available: false, status: '缺少原生桥接' }],
      }],
    }),
    updateDataProviders: fixtures.update,
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { daily_data_provider: 'tickflow' } }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('@/lib/useCardFlash', () => ({
  AnchorWrap: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

function sourceSection() {
  return [...host.querySelectorAll('section')].find(section =>
    section.querySelector('h2')?.textContent === '数据源',
  )!
}

function sourceCards() {
  return [...sourceSection().querySelectorAll<HTMLDivElement>('.grid > div')]
}

function cardNamed(name: string) {
  return sourceCards().find(card => card.querySelector('span.text-sm')?.textContent === name)!
}
beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.update.mockClear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(
    <QueryClientProvider client={client}><SettingsDataSourcesPanel /></QueryClientProvider>,
  ))
  await settle()
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

it('can select the default rustdx route before its native dependency is ready', async () => {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('rustdx'))
  expect(button).toBeDefined()
  expect(button!.disabled).toBe(false)
  await act(async () => button!.click())
  await settle()
  expect(fixtures.update).toHaveBeenCalledWith({ daily_data_provider: 'rustdx' })
})

it('shows dependency instructions and allows applying rustdx without reporting it as serving', async () => {
  expect(host.textContent).toContain('uv sync --extra rustdx --frozen')
  const apply = [...cardNamed('rustdx').querySelectorAll('button')].find(b => b.textContent === '套用')
  expect(apply).toBeDefined()
  await act(async () => apply!.click())
  await settle()
  expect(fixtures.update).toHaveBeenCalledWith(expect.objectContaining({ daily_data_provider: 'rustdx' }))
  expect(host.textContent).not.toContain('服务中 7 项能力')
})

it('removes TickFlow cards and keeps rustdx first and fuyao second without mutating cached sources', () => {
  expect(sourceCards().map(card => card.querySelector('span.text-sm')?.textContent))
    .toEqual(['rustdx', 'fuyao', '其他插件', '本地数据源'])
  expect(sourceSection().textContent).not.toContain('TickFlow')
  const cached = client.getQueryData<{ builtin: { name: string }[]; plugins: { name: string }[] }>(QK.dataSources)
  expect(cached?.builtin[0].name).toBe('tickflow')
  expect(cached?.plugins.map(plugin => plugin.name)).toEqual(['other', 'fuyao', 'rustdx'])
})

it('orders pending rustdx before available fuyao in routing choices and preserves the saved current source', () => {
  const routing = [...host.querySelectorAll('section')].find(section =>
    section.querySelector('h2')?.textContent === '能力路由',
  )!
  expect([...routing.querySelectorAll('button')].map(button => button.textContent))
    .toEqual(['rustdx · 未就绪', 'fuyao', '其他插件'])
  expect(routing.textContent).toContain('TickFlow · 未就绪')
  expect(fixtures.update).not.toHaveBeenCalled()
})

it('opens fuyao details and applies only its declared datasets with the existing default fallback', async () => {
  await act(async () => cardNamed('fuyao').click())
  await settle()
  expect([...host.querySelectorAll('h3')].some(heading => heading.textContent === 'fuyao')).toBe(true)
  const apply = [...cardNamed('fuyao').querySelectorAll('button')].find(button => button.textContent === '套用')!
  await act(async () => apply.click())
  await settle()
  expect(fixtures.update).toHaveBeenCalledWith({
    daily_data_provider: 'rustdx',
    adj_factor_provider: 'rustdx',
    corporate_actions_provider: 'rustdx',
    realtime_data_provider: 'fuyao',
    minute_data_provider: 'rustdx',
    full_minute_data_provider: 'rustdx',
    depth5_data_provider: 'rustdx',
    financial_data_provider: 'rustdx',
  })
})

it('uses matrix order for routing cards, source capability chips and the detail table', async () => {
  const order: [string, string, string, ProviderField][] = [
    ['realtime', '实时行情', '实时', 'realtime_data_provider'],
    ['depth5', '五档盘口', '五档', 'depth5_data_provider'],
    ['minute', '分钟K', '分钟', 'minute_data_provider'],
    ['daily', '日K', '日K', 'daily_data_provider'],
    ['adj_factor', '除权因子', '除权', 'adj_factor_provider'],
    ['corporate_actions', '公司行为', '公司行为', 'corporate_actions_provider'],
    ['financial', '财务数据', '财务', 'financial_data_provider'],
    ['full_minute', '全量分钟', '全量分钟', 'full_minute_data_provider'],
  ]
  const matrix = client.getQueryData<CapabilityMatrix>(QK.capabilityMatrix)!
  const sources = client.getQueryData<DataSourcesResponse>(QK.dataSources)!
  await act(async () => {
    client.setQueryData(QK.capabilityMatrix, {
      ...matrix,
      capabilities: order.map(([id, label, , field]) => ({
        ...matrix.capabilities[0], id, label, field,
      })),
    })
    client.setQueryData(QK.dataSources, {
      ...sources,
      plugins: sources.plugins.map(plugin => plugin.name === 'rustdx'
        ? { ...plugin, datasets: order.map(([id]) => id).reverse() }
        : plugin),
    })
  })
  await settle()

  const routing = [...host.querySelectorAll('section')].find(section =>
    section.querySelector('h2')?.textContent === '能力路由',
  )!
  expect([...routing.querySelectorAll('.grid > div')].map(card =>
    card.querySelector('div.text-xs.font-medium')?.textContent,
  )).toEqual(order.map(([, label]) => label))
  expect([...cardNamed('rustdx').querySelectorAll('span[title^="已适配"], span[title^="正在提供"]')]
    .map(chip => chip.textContent)).toEqual(order.map(([, , chip]) => chip))

  const detail = [...host.querySelectorAll('section')].find(section =>
    section.querySelector('h3')?.textContent === 'rustdx',
  )!
  expect([...detail.querySelectorAll('.divide-y > div span.text-xs.text-foreground')]
    .map(label => label.textContent)).toEqual(order.map(([, label]) => label))
})
