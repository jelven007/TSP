// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyResultHistoryDialog } from './StrategyResultHistoryDialog'

const { list, detail } = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { screenerResultHistory: list, screenerResultHistoryDetail: detail } }))
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
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <StrategyResultHistoryDialog strategyId="sample" onClose={() => {}} />
    </QueryClientProvider>,
  ))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
}
it('shows saved version and parameters and reads only the chosen record', async () => {
  list.mockResolvedValue({ records: [
    { record_id: 'record', strategy_name: '旧名称', strategy_version: 'old-version',
      as_of: '2026-10-22', params: { threshold: 10 }, total: 1, computed_at: 1000,
      asset_type: 'stock', timeframe: '1d' },
  ], next_cursor: null })
  detail.mockResolvedValue({
    summary: { strategy_name: '旧名称', as_of: '2026-10-22', params: { threshold: 10 }, overrides: {} },
    result: { rows: [{ symbol: 'A', name: '历史命中' }] }, ever_rows: {},
  })
  await render()
  expect(document.body.textContent).toContain('旧名称')
  expect(document.body.textContent).toContain('old-version')
  expect(detail).not.toHaveBeenCalled()
  await act(async () => [...document.querySelectorAll('button')].find(b => b.textContent?.includes('查看'))!.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(detail).toHaveBeenCalledWith('sample', 'record')
  expect(document.body.textContent).toContain('历史命中')
  expect(document.body.textContent).toContain('threshold')
})
it('shows load failure without treating it as an empty history', async () => {
  list.mockRejectedValue(new Error('offline'))
  await render()
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('读取失败')
  expect(document.body.textContent).not.toContain('暂无保存结果')
})
