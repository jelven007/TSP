// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Onboarding } from './Onboarding'

vi.mock('framer-motion', async () => {
  const { createElement } = await import('react')
  return {
    motion: {
      div: ({ children, className }: { children?: React.ReactNode; className?: string }) =>
        createElement('div', { className }, children),
    },
    AnimatePresence: ({ children }: { children: React.ReactNode }) => children,
  }
})

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

let root: Root
let node: HTMLDivElement
let queryClient: QueryClient

function Panel() {
  const client = useQueryClient()
  const settings = client.getQueryData<{ onboarding_completed: boolean }>(QK.settings)
  return <div data-completed={String(settings?.onboarding_completed)}>业务面板</div>
}

beforeEach(async () => {
  vi.spyOn(api, 'completeOnboarding').mockResolvedValue({ ok: true, onboarding_completed: true })
  vi.spyOn(api, 'dataSources')
  vi.spyOn(api, 'capabilityMatrix')
  vi.spyOn(api, 'updateDataProviders')
  node = document.createElement('div')
  document.body.append(node)
  root = createRoot(node)
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  queryClient.setQueryData(QK.settings, { onboarding_completed: false, mode: 'free' })
  await act(async () => root.render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={['/onboarding']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/onboarding" element={<Onboarding />} />
          <Route path="/" element={<Panel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  ))
})

afterEach(async () => {
  await act(async () => root.unmount())
  queryClient.clear()
  node.remove()
  vi.restoreAllMocks()
})

async function click(text: string) {
  const button = [...node.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  expect(button, `missing button: ${text}`).toBeDefined()
  await act(async () => {
    button!.click()
    await new Promise(resolve => setTimeout(resolve, 10))
  })
}

describe('two-step onboarding', () => {
  it('keeps the disclaimer and welcome as the only two steps', async () => {
    expect(node.textContent).toContain('1 / 2')
    expect(node.textContent).toContain('使用前请知悉')
    expect(api.completeOnboarding).not.toHaveBeenCalled()

    await click('我已了解,继续')

    expect(node.textContent).toContain('2 / 2')
    expect(node.textContent).toContain('欢迎使用 TSP')
    expect(node.textContent).not.toContain('开始配置')
    expect(node.textContent).not.toContain('配置数据源')
    expect(node.textContent).not.toContain('能力路由检测')
    expect(node.textContent).not.toContain('一切就绪')
    expect(api.dataSources).not.toHaveBeenCalled()
    expect(api.capabilityMatrix).not.toHaveBeenCalled()
    expect(api.updateDataProviders).not.toHaveBeenCalled()
  })

  it('finishes from the welcome page, prevents duplicate submissions, and updates settings before navigation', async () => {
    let finish!: (result: { ok: boolean; onboarding_completed: boolean }) => void
    vi.mocked(api.completeOnboarding).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    await click('我已了解,继续')
    await click('进入面板')

    const pending = [...node.querySelectorAll('button')]
      .find(button => button.textContent?.trim() === '正在进入…')!
    expect(pending.disabled).toBe(true)
    await act(async () => pending.click())
    expect(api.completeOnboarding).toHaveBeenCalledOnce()
    expect(node.textContent).not.toContain('业务面板')
    expect(queryClient.getQueryData(QK.settings)).toMatchObject({ onboarding_completed: false })

    await act(async () => {
      finish({ ok: true, onboarding_completed: true })
      await new Promise(resolve => setTimeout(resolve, 10))
    })

    expect(node.textContent).toContain('业务面板')
    expect(node.querySelector('[data-completed]')?.getAttribute('data-completed')).toBe('true')
    expect(queryClient.getQueryData(QK.settings)).toEqual({ onboarding_completed: true, mode: 'free' })
    expect(api.dataSources).not.toHaveBeenCalled()
    expect(api.capabilityMatrix).not.toHaveBeenCalled()
    expect(api.updateDataProviders).not.toHaveBeenCalled()
  })

  it('stays on the welcome page when completion fails and allows retry', async () => {
    vi.mocked(api.completeOnboarding).mockRejectedValueOnce(new Error('服务暂不可用'))
    await click('我已了解,继续')
    await click('进入面板')

    expect(node.querySelector('[role="alert"]')?.textContent).toContain('保存引导状态失败，请重试')
    expect(node.textContent).toContain('欢迎使用 TSP')
    expect(node.textContent).not.toContain('业务面板')
    expect(queryClient.getQueryData(QK.settings)).toMatchObject({ onboarding_completed: false })

    await click('进入面板')

    expect(api.completeOnboarding).toHaveBeenCalledTimes(2)
    expect(node.textContent).toContain('业务面板')
  })
})
