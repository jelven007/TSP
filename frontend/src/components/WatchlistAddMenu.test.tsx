// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { WatchlistAddMenu } from './WatchlistAddMenu'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let client: QueryClient
beforeEach(() => {
  vi.spyOn(api, 'watchlistGroups').mockResolvedValue({ groups: [] })
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(document.body)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { queueMicrotask(() => cb(0)); return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => {})
})
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})
async function settle() { await act(async () => { await new Promise(r => setTimeout(r, 10)) }) }
async function render(allowCreate = true) {
  const host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const onSelect = vi.fn()
  await act(async () => root.render(<QueryClientProvider client={client}>
    <WatchlistAddMenu allowCreate={allowCreate} onSelect={onSelect}>批量加自选</WatchlistAddMenu>
  </QueryClientProvider>))
  await click('批量加自选')
  return onSelect
}
async function click(text: string) {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.trim() === text)!
  expect(button, text).toBeDefined()
  await act(async () => button.click()); await settle()
}
async function name(value: string) {
  const field = document.querySelector<HTMLInputElement>('[aria-label="新自选分组名称"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('creates a group and adds the current stocks into it', async () => {
  const group = { id: 'new', name: '今日买入', color: 'sky' as const }
  const create = vi.spyOn(api, 'watchlistGroupCreate').mockResolvedValue({ group, groups: [group] })
  const onSelect = await render()
  await click('新建自选分组')
  await name('  今日买入  ')
  await click('创建并加入')
  expect(create).toHaveBeenCalledWith('今日买入', 'sky')
  expect(onSelect).toHaveBeenCalledWith('new')
  expect(client.getQueryData(QK.watchlistGroups)).toEqual({ groups: [group] })
  expect(document.querySelector('[aria-label="新建自选分组"][role="dialog"]')).toBeNull()
})

it('keeps the input and selection dialog open after group creation fails', async () => {
  vi.spyOn(api, 'watchlistGroupCreate').mockRejectedValue(new Error('同名分组已存在'))
  const onSelect = await render()
  await click('新建自选分组')
  await name('重复')
  await click('创建并加入')
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('同名分组')
  expect(document.querySelector<HTMLInputElement>('[aria-label="新自选分组名称"]')!.value).toBe('重复')
  expect(onSelect).not.toHaveBeenCalled()
})

it('does not offer creation in group filter menus unless enabled', async () => {
  await render(false)
  expect(document.body.textContent).not.toContain('新建自选分组')
})

it('does not add stocks from a closed result view when group creation finishes late', async () => {
  let finish!: (value: any) => void
  vi.spyOn(api, 'watchlistGroupCreate').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const onSelect = await render()
  await click('新建自选分组')
  await name('稍后完成')
  await click('创建并加入')
  await act(async () => root.unmount())
  await act(async () => finish({ group: { id: 'late', name: '稍后完成', color: 'sky' }, groups: [] }))
  await settle()
  expect(onSelect).not.toHaveBeenCalled()
})
