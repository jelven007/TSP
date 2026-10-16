// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsDataSourcesPanel } from './DataSources'

const fixtures = vi.hoisted(() => ({
  update: vi.fn(async (_changes: object) => ({})),
  available: false,
}))
vi.mock('@/lib/api', () => ({
  api: {
    dataSources: async () => ({
      builtin: [], custom: [], errors: [],
      plugins: [{
        name: 'rustdx', display_name: 'rustdx', runtime: 'none',
        datasets: ['daily'], available: fixtures.available,
        status: '缺少 rustdx 原生桥接',
        install_hint: '源码环境执行 uv sync --extra rustdx --frozen',
      }],
    }),
    capabilityMatrix: async () => ({
      capabilities: [{
        id: 'daily', label: '日K', field: 'daily_data_provider',
        current: 'tickflow', effective: 'tickflow', effective_display: 'TickFlow',
        usable: false, candidates: [],
        pending: [{ name: 'rustdx', display: 'rustdx', status: '缺少原生桥接' }],
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
  const apply = [...host.querySelectorAll('button')].find(b => b.textContent === '套用')
  expect(apply).toBeDefined()
  await act(async () => apply!.click())
  await settle()
  expect(fixtures.update).toHaveBeenCalledWith(expect.objectContaining({ daily_data_provider: 'rustdx' }))
  expect(host.textContent).not.toContain('服务中 7 项能力')
})
