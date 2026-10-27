// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategySettingsDialog } from './StrategySettingsDialog'
import { WatchlistGroupBar } from '@/components/WatchlistGroups'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { get, groups, save, importCsv } = vi.hoisted(() => ({
  get: vi.fn(), groups: vi.fn(), save: vi.fn(), importCsv: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: {
  strategyGet: get, watchlistGroups: groups, strategySaveConfig: save, watchlistGroupImportCsv: importCsv,
} }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/accountSession', () => ({ hasPermission: () => false }))
vi.mock('@/components/ScoringEditor', () => ({ ScoringEditor: () => null }))
vi.mock('./OverlayFilterEditor', () => ({ OverlayFilterEditor: () => null }))
vi.mock('./SignalPicker', () => ({ SignalPicker: () => null }))
vi.mock('@/components/signals/SignalTriggerActions', () => ({ SignalTriggerActions: () => null }))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  client?.clear()
  vi.clearAllMocks()
})
async function render(node: React.ReactNode) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root!.render(<QueryClientProvider client={client}>{node}</QueryClientProvider>))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)) })
}
function button(text: string) {
  return [...document.querySelectorAll('button')].find(el => el.textContent === text)!
}

it('defaults to all stocks, loads custom groups and saves board and group union independently of numeric filters', async () => {
  get.mockResolvedValue({ id: 'test', name: '测试', source: 'custom', params: [],
    params_defaults: {}, scoring: {}, basic_filter: { enabled: false }, entry_signals: [], exit_signals: [] })
  groups.mockResolvedValue({ groups: [{ id: 'core', name: '核心', color: 'sky' }] })
  save.mockResolvedValue({ ok: true })
  await render(<StrategySettingsDialog strategyId="test" onClose={() => {}} />)
  expect(document.querySelector<HTMLInputElement>('input[aria-label="全部股票"]')?.checked).toBe(true)
  await act(async () => document.querySelector<HTMLInputElement>('input[aria-label="选择股票范围"]')!.click())
  await act(async () => document.querySelector<HTMLInputElement>('input[aria-label="自定义"]')!.click())
  expect(groups).toHaveBeenCalled()
  await act(async () => {
    document.querySelector<HTMLInputElement>('input[aria-label="沪主板"]')!.click()
    document.querySelector<HTMLInputElement>('input[aria-label="核心"]')!.click()
    document.querySelector<HTMLInputElement>('input[aria-label="剔除 *ST 和 ST"]')!.click()
  })
  await act(async () => button('保存设置').click())
  expect(save).toHaveBeenCalledWith('test', expect.objectContaining({ basic_filter: expect.objectContaining({
    enabled: false, target_mode: 'selected', boards: ['沪主板'],
    include_custom: true, watchlist_group_ids: ['core'], exclude_st: true,
  }) }))
})

it('immediately explains why a referenced group cannot be deleted', async () => {
  const onDelete = vi.fn()
  await render(<WatchlistGroupBar groups={[{ id: 'core', name: '核心', color: 'sky', referenced_by: ['test'] }]}
    counts={{ core: 1 }} selected="all" total={1} onSelect={() => {}}
    onCreate={vi.fn()} onRename={vi.fn()} onDelete={onDelete} />)
  await act(async () => document.querySelector<HTMLButtonElement>('button[aria-label="管理自选分组"]')!.click())
  await act(async () => document.querySelector<HTMLButtonElement>('button[title="删除分组"]')!.click())
  expect(document.body.textContent).toContain('被策略引用，无法删除')
  expect(button('确认')).toBeUndefined()
  expect(onDelete).not.toHaveBeenCalled()
})

it('disables clearing a referenced group and leaves an unrelated group available', async () => {
  const onClearGroup = vi.fn().mockResolvedValue(undefined)
  const props = {
    groups: [
      { id: 'core', name: '核心', color: 'sky' as const, referenced_by: ['test'] },
      { id: 'other', name: '观察', color: 'teal' as const },
    ],
    counts: { core: 1, other: 1 }, total: 1, onSelect: vi.fn(),
    onCreate: vi.fn(), onRename: vi.fn(), onDelete: vi.fn(), onClearGroup,
  }
  await render(<WatchlistGroupBar {...props} selected="core" />)
  const clear = () => document.querySelector<HTMLButtonElement>('button[aria-label="清空当前分组"]')!
  expect(clear().disabled).toBe(true)
  expect(clear().title).toContain('被策略引用')
  await act(async () => clear().click())
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(onClearGroup).not.toHaveBeenCalled()
  await act(async () => root!.render(
    <QueryClientProvider client={client}><WatchlistGroupBar {...props} selected="other" /></QueryClientProvider>,
  ))
  expect(clear().disabled).toBe(false)
  await act(async () => clear().click())
  await act(async () => button('确认清空').click())
  expect(onClearGroup).toHaveBeenCalledWith('other')
})

it('creates a group without offering CSV import in the group manager', async () => {
  const onCreate = vi.fn().mockResolvedValue(undefined)
  await render(<WatchlistGroupBar groups={[]} counts={{}} selected="all" total={0}
    onSelect={() => {}} onCreate={onCreate} onRename={vi.fn()} onDelete={vi.fn()} />)
  await act(async () => document.querySelector<HTMLButtonElement>('button[aria-label="管理自选分组"]')!.click())
  const name = document.querySelector<HTMLInputElement>('input[placeholder="新分组名称"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(name, '导入分组')
    name.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(document.querySelector('input[type="file"]')).toBeNull()
  expect(document.body.textContent).not.toContain('导入 CSV 股票列表')
  expect(document.body.textContent).not.toContain('新建并导入')
  await act(async () => button('新建').click())
  expect(onCreate).toHaveBeenCalledWith('导入分组', 'sky')
})
