// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategySettingsDialog } from './StrategySettingsDialog'
import { QK } from '@/lib/queryKeys'

const { get, remove } = vi.hoisted(() => ({ get: vi.fn(), remove: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { strategyGet: get, strategyDelete: remove,
  watchlistGroups: vi.fn(async () => ({ groups: [] })) } }))
vi.mock('@/lib/accountSession', () => ({ hasPermission: () => false }))
vi.mock('@/components/ScoringEditor', () => ({ ScoringEditor: () => null }))
vi.mock('./OverlayFilterEditor', () => ({ OverlayFilterEditor: () => null }))
vi.mock('./SignalPicker', () => ({ SignalPicker: () => null }))
vi.mock('@/components/signals/SignalTriggerActions', () => ({ SignalTriggerActions: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

let root: Root
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  vi.clearAllMocks()
})
async function render() {
  get.mockResolvedValue({ id: 'target', name: '测试策略', source: 'custom', params: [],
    params_defaults: {}, scoring: {}, basic_filter: {}, entry_signals: [], exit_signals: [] })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(QK.screenerHistory('target'), { records: ['old'] })
  client.setQueryData(QK.screenerHistoryDetail('target', 'record'), { old: true })
  client.setQueryData(QK.screenerHistory('other'), { records: ['kept'] })
  client.setQueryData(QK.screenerCachedSummary(), { results: { target: {}, other: {} } })
  client.setQueryData(['research-jobs'], { items: [] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  const close = vi.fn(), deleted = vi.fn()
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategySettingsDialog strategyId="target" onClose={close} onDeleted={deleted} />
  </QueryClientProvider>))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
  return { close, deleted }
}
async function click(text: string) {
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent === text)!.click())
}
it('explains historical cleanup and removes only the deleted strategy history cache', async () => {
  remove.mockResolvedValue({ ok: true, warnings: [] })
  const { close, deleted } = await render()
  await click('删除策略')
  expect(document.body.textContent).toContain('历史选股结果')
  expect(document.body.textContent).toContain('回测')
  await click('确认删除')
  expect(remove).toHaveBeenCalledWith('target')
  expect(close).toHaveBeenCalledOnce()
  expect(deleted).toHaveBeenCalledOnce()
  expect(client.getQueryData(QK.screenerHistory('target'))).toBeUndefined()
  expect(client.getQueryData(QK.screenerHistoryDetail('target', 'record'))).toBeUndefined()
  expect(client.getQueryData(QK.screenerHistory('other'))).toEqual({ records: ['kept'] })
  expect(client.getQueryState(QK.screenerCachedSummary())?.isInvalidated).toBe(true)
  expect(client.getQueryState(['research-jobs'])?.isInvalidated).toBe(true)
})
it('keeps the dialog and history when cleanup fails', async () => {
  remove.mockRejectedValue(new Error('历史清理失败'))
  const { close, deleted } = await render()
  await click('删除策略')
  await click('确认删除')
  expect(document.body.textContent).toContain('历史清理失败')
  expect(close).not.toHaveBeenCalled()
  expect(deleted).not.toHaveBeenCalled()
  expect(client.getQueryData(QK.screenerHistory('target'))).toEqual({ records: ['old'] })
})
