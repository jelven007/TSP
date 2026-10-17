// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsAIPanel } from './AI'

const mocks = vi.hoisted(() => ({
  save: vi.fn(), clear: vi.fn(), test: vi.fn(),
  settings: { data: {
    ai_provider: 'openai_compat', ai_protocol: 'chat_completions',
    ai_base_url: 'https://ai.test/v1', ai_model: 'test-model',
    has_ai_key: true, ai_configured: true, ai_api_key_masked: 'test-key',
  } },
}))
vi.mock('@/lib/api', () => ({ api: {
  saveAiSettings: mocks.save, clearAiSettings: mocks.clear, strategyAiTest: mocks.test,
} }))
vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => mocks.settings }))
vi.mock('@/lib/useCardFlash', () => ({
  useCardFlash: () => ({ ref: null, flash: false }), cardFlashCls: () => '',
}))

let root: Root
let node: HTMLDivElement
let queryClient: QueryClient

beforeEach(async () => {
  vi.clearAllMocks()
  mocks.settings.data.ai_protocol = 'chat_completions'
  mocks.save.mockResolvedValue({ ok: true })
  mocks.clear.mockResolvedValue({ ok: true })
  mocks.test.mockResolvedValue({ ok: true, model: 'test-model' })
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  node = document.createElement('div')
  document.body.append(node)
  root = createRoot(node)
  queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  await render()
})
afterEach(() => {
  act(() => root.unmount())
  queryClient.clear()
  node.remove()
})

async function render() {
  await act(async () => root.render(
    <QueryClientProvider client={queryClient}><SettingsAIPanel /></QueryClientProvider>,
  ))
}

async function choose(protocol: string) {
  await act(async () => {
    const field = node.querySelector<HTMLSelectElement>('[aria-label="接口协议"]')!
    field.value = protocol
    field.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

async function click(text: string) {
  const button = [...node.querySelectorAll('button')].find(item => item.textContent?.trim() === text)!
  await act(async () => {
    button.click()
    await new Promise(resolve => setTimeout(resolve, 10))
  })
}

it('keeps Chat as default and saves the explicitly selected Responses protocol', async () => {
  expect(node.querySelector<HTMLSelectElement>('[aria-label="接口协议"]')?.value).toBe('chat_completions')
  await choose('responses')
  await click('保存配置')
  expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({ protocol: 'responses' }))
  expect(node.textContent).toContain('接口失败时不会自动切换协议')
})

it('tests the selected protocol through the existing save-then-test flow', async () => {
  await choose('responses')
  await click('测试')
  expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({ protocol: 'responses' }))
  expect(mocks.save.mock.invocationCallOrder[0]).toBeLessThan(mocks.test.mock.invocationCallOrder[0])
  expect(node.textContent).toContain('连通成功')
})

it('hides protocol for Codex and retains the API draft when switching back', async () => {
  await choose('responses')
  await click('Codex CLI')
  expect(node.querySelector('[aria-label="接口协议"]')).toBeNull()
  await click('保存配置')
  expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({ provider: 'codex_cli', protocol: undefined }))
  await click('自定义')
  expect(node.querySelector<HTMLSelectElement>('[aria-label="接口协议"]')?.value).toBe('responses')
})

it('clears the selected protocol with the rest of AI configuration', async () => {
  await choose('responses')
  await click('清空')
  await click('确认')
  expect(mocks.clear).toHaveBeenCalledOnce()
  expect(node.querySelector<HTMLSelectElement>('[aria-label="接口协议"]')?.value).toBe('chat_completions')
})
