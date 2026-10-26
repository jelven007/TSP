// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyResultHistoryDialog } from './StrategyResultHistoryDialog'

const { list, detail, remove, permission } = vi.hoisted(() => ({
  list: vi.fn(), detail: vi.fn(), remove: vi.fn(), permission: vi.fn(() => true),
}))
vi.mock('@/lib/api', () => ({ api: {
  screenerResultHistory: list, screenerResultHistoryDetail: detail, screenerResultHistoryDelete: remove,
} }))
vi.mock('@/lib/accountSession', () => ({ hasPermission: permission }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  vi.clearAllMocks()
  permission.mockReturnValue(true)
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

const record = { record_id: 'record', strategy_name: '旧名称', strategy_version: 'old-version',
  as_of: '2026-10-22', params: {}, total: 1, computed_at: 1000, asset_type: 'stock', timeframe: '1d' }
async function click(text: string) {
  await act(async () => [...document.querySelectorAll('button')].find(b => b.textContent === text)!.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
}
async function change(label: string, value: string) {
  const input = document.querySelector(`[aria-label="${label}"]`) as HTMLInputElement | HTMLSelectElement
  const prototype = input.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event(input.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }))
  })
}
it('applies combined filters and resets the cursor and detail when clearing them', async () => {
  list.mockImplementation((_id, cursor, filters) => Promise.resolve({
    records: filters?.q ? [] : [{ ...record, record_id: cursor ? 'page-two' : 'record' }],
    next_cursor: cursor ? null : 'next',
  }))
  detail.mockResolvedValue({ summary: record, result: { rows: [{ symbol: 'A', name: '第二页明细' }] } })
  await render()
  await click('下一页')
  expect(list).toHaveBeenLastCalledWith('sample', 'next', {})
  await click('查看')
  expect(document.body.textContent).toContain('第二页明细')
  await change('名称或版本', 'old')
  await change('开始日期', '2026-10-20')
  await change('结束日期', '2026-10-22')
  await change('资产类型', 'etf')
  await change('周期', '1m')
  const count = list.mock.calls.length
  await click('筛选')
  expect(list.mock.calls.length).toBe(count + 1)
  expect(list).toHaveBeenLastCalledWith('sample', undefined, {
    q: 'old', start_date: '2026-10-20', end_date: '2026-10-22', asset_type: 'etf', timeframe: '1m',
  })
  expect(document.body.textContent).toContain('没有符合筛选条件的结果')
  expect(document.body.textContent).not.toContain('第二页明细')
  await click('重置')
  expect((document.querySelector('[aria-label="名称或版本"]') as HTMLInputElement).value).toBe('')
  expect(document.body.textContent).toContain('旧名称')
  expect([...document.querySelectorAll('button')].find(b => b.textContent === '上一页')!.disabled).toBe(true)
})

it('rejects an inverted date range without sending a filtered request', async () => {
  list.mockResolvedValue({ records: [record], next_cursor: null })
  await render()
  await change('开始日期', '2026-10-23')
  await change('结束日期', '2026-10-22')
  await click('筛选')
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('开始日期不能晚于结束日期')
  expect(list).toHaveBeenCalledTimes(1)
})

it('confirms deletion, allows cancellation and clears the deleted detail and related queries', async () => {
  let deleted = false
  list.mockImplementation(() => Promise.resolve({ records: deleted ? [] : [record], next_cursor: null }))
  detail.mockResolvedValue({ summary: record, result: { rows: [{ symbol: 'A', name: '历史命中' }] } })
  remove.mockImplementation(async () => { deleted = true; return { ok: true } })
  await render()
  client.setQueryData(['screener-cached', 'summary', '2026-10-22', 'stock', '1d'], { saved: true })
  client.setQueryData(['screener-cached', 'summary', '2026-10-22', 'stock', 'all'], { saved: true })
  client.setQueryData(['screener-cached', 'all', '2026-10-22', '', 'stock', 'all'], { saved: true })
  client.setQueryData(['screener-cached', 'summary', '2026-10-22', 'etf', 'all'], { saved: true })
  client.setQueryData(['screener-cached', 'summary', '2026-10-21', 'stock', '1d'], { saved: true })
  await click('查看')
  await click('删除')
  expect(document.body.textContent).toContain('删除历史结果')
  expect(document.body.textContent).toContain('无法恢复')
  await click('取消')
  expect(remove).not.toHaveBeenCalled()
  await click('删除')
  await click('确认删除')
  expect(remove).toHaveBeenCalledWith('sample', 'record')
  expect(document.body.textContent).not.toContain('历史命中')
  expect(document.body.textContent).toContain('暂无保存结果')
  expect(client.getQueryData(['screener-history-detail', 'sample', 'record'])).toBeUndefined()
  expect(client.getQueryState(['screener-cached', 'summary', '2026-10-22', 'stock', '1d'])?.isInvalidated).toBe(true)
  expect(client.getQueryState(['screener-cached', 'summary', '2026-10-22', 'stock', 'all'])?.isInvalidated).toBe(true)
  expect(client.getQueryState(['screener-cached', 'all', '2026-10-22', '', 'stock', 'all'])?.isInvalidated).toBe(true)
  expect(client.getQueryState(['screener-cached', 'summary', '2026-10-22', 'etf', 'all'])?.isInvalidated).toBe(false)
  expect(client.getQueryState(['screener-cached', 'summary', '2026-10-21', 'stock', '1d'])?.isInvalidated).toBe(false)
})

it('retains the selected record after deletion fails and allows retry', async () => {
  list.mockResolvedValue({ records: [record], next_cursor: null })
  remove.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ ok: true })
  await render()
  await click('删除')
  await click('确认删除')
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('删除失败')
  expect(document.body.textContent).toContain('旧名称')
  list.mockResolvedValue({ records: [], next_cursor: null })
  await click('确认删除')
  expect(remove).toHaveBeenCalledTimes(2)
  expect(document.body.textContent).toContain('暂无保存结果')
})

it('disables deletion without business write permission', async () => {
  permission.mockReturnValue(false)
  list.mockResolvedValue({ records: [record], next_cursor: null })
  await render()
  const button = [...document.querySelectorAll('button')].find(b => b.textContent === '删除')!
  expect(button.disabled).toBe(true)
  expect(button.title).toContain('权限')
  expect(remove).not.toHaveBeenCalled()
})

it('keeps the active filter results when an older request resolves late', async () => {
  let resolveOld!: (value: unknown) => void
  list.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    .mockResolvedValue({ records: [], next_cursor: null })
  await render()
  await change('名称或版本', 'missing')
  await click('筛选')
  await act(async () => resolveOld({ records: [record], next_cursor: null }))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(document.body.textContent).toContain('没有符合筛选条件的结果')
  expect(document.body.textContent).not.toContain('旧名称')
})

it('blocks dismissal while deleting and ignores a late detail response', async () => {
  let finishDelete!: (value: unknown) => void
  let finishDetail!: (value: unknown) => void
  list.mockResolvedValue({ records: [record], next_cursor: null })
  detail.mockImplementation(() => new Promise(resolve => { finishDetail = resolve }))
  remove.mockImplementation(() => new Promise(resolve => { finishDelete = resolve }))
  await render()
  await click('查看')
  await click('删除')
  await click('确认删除')
  expect([...document.querySelectorAll('button')].find(b => b.textContent === '取消')!.disabled).toBe(true)
  expect(document.querySelector('[aria-label="关闭删除历史结果"]')?.hasAttribute('disabled')).toBe(true)
  list.mockResolvedValue({ records: [], next_cursor: null })
  await act(async () => finishDelete({ ok: true }))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  await act(async () => finishDetail({ summary: record, result: { rows: [{ symbol: 'A', name: '已删明细' }] } }))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(document.body.textContent).toContain('暂无保存结果')
  expect(document.body.textContent).not.toContain('已删明细')
  expect(client.getQueryData(['screener-history-detail', 'sample', 'record'])).toBeUndefined()
})

it('returns to the first page after deleting the final record on a later page', async () => {
  list.mockImplementation((_id, cursor) => Promise.resolve({
    records: [cursor ? record : { ...record, record_id: 'newer', strategy_name: '最新记录' }],
    next_cursor: cursor ? null : 'next',
  }))
  remove.mockResolvedValue({ ok: true })
  await render()
  await click('下一页')
  await click('删除')
  await click('确认删除')
  expect(document.body.textContent).toContain('最新记录')
  expect([...document.querySelectorAll('button')].find(b => b.textContent === '上一页')!.disabled).toBe(true)
})

it('does not show a deleted record again if refreshing history fails', async () => {
  list.mockResolvedValueOnce({ records: [record], next_cursor: null }).mockRejectedValue(new Error('refresh offline'))
  remove.mockResolvedValue({ ok: true })
  await render()
  await click('删除')
  await click('确认删除')
  expect(document.body.textContent).toContain('历史结果已删除')
  expect(document.body.textContent).toContain('读取失败')
  expect(document.body.textContent).not.toContain('旧名称')
})
