// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { SettingsSystemPanel } from './System'

const { read, save, permitted } = vi.hoisted(() => ({
  read: vi.fn(), save: vi.fn(), permitted: vi.fn(() => true),
}))
vi.mock('@/lib/api', () => ({
  api: { strategyConcurrency: read, updateStrategyConcurrency: save },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { screener_auto_run: false } }),
  useVersion: () => ({ data: { version: '0.4.21' } }),
}))
vi.mock('@/lib/updateCheck', () => ({
  useUpdateCheck: () => ({ status: 'idle', check: vi.fn() }),
}))
vi.mock('@/lib/accountSession', () => ({
  accountStorage: window.localStorage,
  multitenancyEnabled: () => true,
  hasPermission: permitted,
}))
vi.mock('@/components/AlertToast', () => ({ refreshAlertToastConfig: vi.fn() }))
vi.mock('@/components/OpenSourceReleaseMonitor', () => ({ OpenSourceReleaseMonitor: () => null }))
vi.mock('@/lib/voiceBroadcast', () => ({
  listZhVoices: () => [], getCurrentVoiceURI: () => '', previewVoice: vi.fn(), activateVoice: vi.fn(),
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient
const status = {
  available: true, pending: false, config_enabled: true, execution_mode: 'sandbox',
  host_memory_mb: 16384, host_cpus: 32, budget_ratio: .25,
  memory_budget_mb: 4096, cpu_budget: 8, recommended_concurrency: 4,
  max_concurrency: 16, strategy_concurrency: null, effective_concurrency: 4,
  task_memory_mb: 1024, task_cpus: 1, active: 0, recommendation_basis: 'task_limits',
}

afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  root = undefined
  vi.clearAllMocks()
  permitted.mockReturnValue(true)
})

async function render() {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root!.render(
      <QueryClientProvider client={client}>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <SettingsSystemPanel />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await flushQueries()
}

async function flushQueries() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
}

function select() {
  return host.querySelector<HTMLSelectElement>('select[aria-label="并发策略数"]')
}

it('puts global concurrency and the quarter budget inside the strategy section', async () => {
  read.mockResolvedValue(status)
  await render()
  const section = [...host.querySelectorAll('section')].find(
    node => node.querySelector('h3')?.textContent === '策略页',
  )!
  expect(section.textContent).toContain('进入策略页自动运行策略')
  expect(section.textContent).toContain('并发策略数')
  expect(section.textContent).toContain('4 GiB')
  expect(section.textContent).toContain('25%')
  expect(section.textContent).toContain('所有租户')
  expect(section.textContent).toContain('推荐')
  expect(select()?.value).toBe('auto')
})

it('persists a manual value and displays the applied global limit', async () => {
  read.mockResolvedValue(status)
  save.mockResolvedValue({
    ...status, strategy_concurrency: 8, effective_concurrency: 8, task_memory_mb: 512,
  })
  await render()
  await act(async () => {
    select()!.value = '8'
    select()!.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await flushQueries()
  expect(save).toHaveBeenCalledWith(8)
  expect(select()!.value).toBe('8')
  expect(host.textContent).toContain('当前生效：8')
  expect(host.textContent).toContain('512 MiB')
})

it('keeps the applied value and shows a save error without false success', async () => {
  read.mockResolvedValue(status)
  save.mockRejectedValue(new Error('计算服务暂不可用'))
  await render()
  await act(async () => {
    select()!.value = '8'
    select()!.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await flushQueries()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('计算服务暂不可用')
  expect(select()!.value).toBe('auto')
  expect(host.textContent).not.toContain('已保存')
})

it('does not invent host resources when the compute service is unavailable', async () => {
  read.mockResolvedValue({
    available: false, strategy_concurrency: 3, error: '计算服务暂不可用',
  })
  await render()
  expect(host.textContent).toContain('计算服务暂不可用')
  expect(select()).toBeNull()
  expect(host.textContent).not.toContain('4 GiB')
})

it('restores automatic mode and uses the updated recommendation', async () => {
  read.mockResolvedValue({ ...status, strategy_concurrency: 8, effective_concurrency: 8 })
  save.mockResolvedValue(status)
  await render()
  await act(async () => {
    select()!.value = 'auto'
    select()!.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await flushQueries()
  expect(save).toHaveBeenCalledWith(null)
  expect(select()!.value).toBe('auto')
  expect(host.textContent).toContain('当前生效：4')
})

it('shows a preserved setting when a smaller host can apply fewer slots', async () => {
  read.mockResolvedValue({
    ...status, strategy_concurrency: 8, max_concurrency: 4, effective_concurrency: 4,
  })
  await render()
  expect(select()!.value).toBe('8')
  expect(host.textContent).toContain('当前生效：4')
  expect(host.textContent).toContain('已按当前资源预算限制')
})

it('does not report a saved configuration as applied when the broker has not read it', async () => {
  read.mockResolvedValue(status)
  save.mockResolvedValue({ ...status, strategy_concurrency: 8, pending: true })
  await render()
  await act(async () => {
    select()!.value = '8'
    select()!.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await flushQueries()
  expect(host.textContent).toContain('计算服务尚未应用')
  expect(host.textContent).not.toContain('新任务按当前配置执行')
})

it('shows a fixed tenant limit without exposing administrator configuration', async () => {
  permitted.mockReturnValue(false)
  read.mockResolvedValue({ available: true, editable: false, strategy_concurrency: 1,
    effective_concurrency: 1, recommended_concurrency: 1 })
  await render()
  expect(select()).toBeNull()
  expect(read).toHaveBeenCalled()
  expect(host.textContent).toContain('租户并发策略数：1')
  expect(host.textContent).not.toContain('整机')
  expect(save).not.toHaveBeenCalled()
})
