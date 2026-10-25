// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { ResearchCandidatesDialog } from './ResearchCandidatesDialog'

const mocks = vi.hoisted(() => ({ remove: vi.fn() }))
const candidate = { id: 'candidate-1', name: '研究示例', kind: 'strategy', status: 'pending', metrics: {}, config: {}, source_id: 'example' }
vi.mock('@/lib/api', () => ({ api: {
  researchCandidates: async () => ({ items: [candidate] }),
  researchCandidateDelete: (id: string) => mocks.remove(id),
  strategyList: async () => ({ strategies: [] }),
  factorColumns: async () => ({ columns: [] }),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let root: Root
let mount: HTMLDivElement
let client: QueryClient
const parentClose = vi.fn()
beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.remove.mockReset()
  parentClose.mockClear()
  mount = document.createElement('div')
  document.body.append(mount)
  root = createRoot(mount)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(QK.researchCandidates, { items: [candidate] })
  client.setQueryData(QK.strategyLinkOptions(), { strategies: [] })
  client.setQueryData(QK.factorColumns, { columns: [] })
  await act(async () => root.render(
    <QueryClientProvider client={client}><ResearchCandidatesDialog onClose={parentClose} /></QueryClientProvider>,
  ))
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  mount.remove()
})

async function click(label: string) {
  const buttons = document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')
  const button = Array.from(buttons).find(el => el.getAttribute('aria-label') === label || el.textContent === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('requires confirmation before deleting and Escape cancels only the nested confirmation', async () => {
  await click('删除候选 研究示例')
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(2)
  expect(mocks.remove).not.toHaveBeenCalled()
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
  expect(parentClose).not.toHaveBeenCalled()
  expect(mocks.remove).not.toHaveBeenCalled()
  await click('删除候选 研究示例')
  await click('取消')
  expect(mocks.remove).not.toHaveBeenCalled()
})

it('deletes the selected candidate once and prevents dismissal while the request is pending', async () => {
  let finish!: (value: unknown) => void
  mocks.remove.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await click('删除候选 研究示例')
  await click('确认删除')
  expect(mocks.remove).toHaveBeenCalledExactlyOnceWith('candidate-1')
  expect(Array.from(document.querySelectorAll('[role="dialog"]')).at(-1)?.textContent).toContain('处理中')
  await click('取消')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(2)
  expect(parentClose).not.toHaveBeenCalled()
  await act(async () => {
    finish({ ok: true })
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
})
