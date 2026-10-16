// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { accountScope, confirmAccount } from '@/lib/accountSession'
import { OPEN_SOURCE_REPO, type OpenSourceRelease } from '@/lib/api/openSourceRelease'
import { OpenSourceReleaseMonitor, OpenSourceReleaseNotice } from '@/components/OpenSourceReleaseMonitor'
import { QK } from '@/lib/queryKeys'
import { useOpenSourceMonitor } from './openSourceMonitor'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

const CACHE_KEY = 'open_source_release_monitor'
const first: OpenSourceRelease = { id: 1, tag: 'v0.3.7', publishedAt: '2026-10-14T07:19:36Z' }
const next: OpenSourceRelease = { id: 2, tag: 'v0.3.8', publishedAt: '2026-10-16T07:19:36Z' }
let root: Root | undefined
let qc: QueryClient
let monitor: ReturnType<typeof useOpenSourceMonitor>
let container: HTMLDivElement

function Harness({ enabled = true, panel = true }: { enabled?: boolean; panel?: boolean }) {
  monitor = useOpenSourceMonitor({ enabled, poll: true })
  const location = useLocation()
  return <>
    <span hidden data-route={location.pathname + location.search} />
    <OpenSourceReleaseNotice monitor={monitor} />
    {panel && <OpenSourceReleaseMonitor />}
  </>
}

async function render({ enabled = true, panel = true, newClient = false, entry = '/' } = {}) {
  if (newClient) {
    await act(async () => root?.unmount())
    qc.clear()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    root = createRoot(container)
  }
  await act(async () => {
    root?.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[entry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Harness enabled={enabled} panel={panel} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await settle()
}

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)) })
}

async function check() {
  await act(async () => {
    await qc.refetchQueries({ queryKey: QK.openSourceRelease(accountScope()), type: 'active' })
  })
  await settle()
}

function seed(seen: OpenSourceRelease | null, latest = seen) {
  localStorage.setItem(CACHE_KEY, JSON.stringify({
    schema: 1, repository: OPEN_SOURCE_REPO, seen, latest, checkedAt: Date.now() - 1000,
  }))
}

beforeEach(() => {
  localStorage.clear()
  confirmAccount({
    auth_required: false, configured: false, has_users: false, authenticated: true,
    legacy_migration_required: false, registration_enabled: false,
    email_verification_required: false, user: null,
  })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root?.unmount())
  root = undefined
  qc.clear()
  container.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('independent open-source monitor', () => {
  it('首次建立基准，共享请求，与应用更新缓存互不影响', async () => {
    const appCache = JSON.stringify({ current: '0.4.0', latest: '0.4.1', found: true })
    localStorage.setItem('update_check_cache', appCache)
    const fetch = vi.spyOn(api, 'openSourceRelease').mockResolvedValue(first)
    await render()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(monitor.hasNewRelease).toBe(false)
    expect(container.textContent).toContain('开源版本监控')
    expect(container.textContent).toContain('暂无新的开源发布')
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)!)).toMatchObject({ seen: first, latest: first })
    expect(localStorage.getItem('update_check_cache')).toBe(appCache)
  })

  it('新 Release 提示直至标记已读，刷新后不会重复提示', async () => {
    seed(first)
    vi.spyOn(api, 'openSourceRelease').mockResolvedValue(next)
    await render()
    expect(monitor.hasNewRelease).toBe(true)
    expect(container.textContent).toContain('开源仓库有新发布')
    const detail = container.querySelector<HTMLAnchorElement>('a[href="/settings?tab=system&opensource=1"]')
    expect(detail).not.toBeNull()
    expect(container.querySelector(`a[href="https://github.com/${OPEN_SOURCE_REPO}/releases/tag/v0.3.8"]`)).not.toBeNull()

    const markRead = Array.from(container.querySelectorAll('button')).find(b => b.textContent === '标记已读')
    await act(async () => markRead?.click())
    await settle()
    expect(monitor.hasNewRelease).toBe(false)
    expect(container.textContent).not.toContain('开源仓库有新发布')
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)!)).toMatchObject({ seen: next })
    await render({ newClient: true })
    expect(monitor.hasNewRelease).toBe(false)
  })

  it('识别后续发布而不比较本地或开源语义版本，旧发布回退不提示', async () => {
    seed(first)
    const fetch = vi.spyOn(api, 'openSourceRelease')
      .mockResolvedValue({ ...next, tag: 'v0.2.0' })
    await render()
    expect(monitor.hasNewRelease).toBe(true)
    fetch.mockResolvedValue({ ...next, publishedAt: '2026-10-13T07:19:36Z' })
    await check()
    expect(monitor.hasNewRelease).toBe(false)
  })

  it('同一 Release 的标题或标签编辑不作为新发布', async () => {
    seed(first)
    vi.spyOn(api, 'openSourceRelease').mockResolvedValue({ ...first, tag: 'v0.3.7-edited' })
    await render()
    expect(monitor.hasNewRelease).toBe(false)
  })

  it('首次尚无 Release，随后第一次正式发布会提示', async () => {
    const fetch = vi.spyOn(api, 'openSourceRelease').mockResolvedValue(null)
    await render()
    expect(container.textContent).toContain('尚无正式 Release')
    expect(monitor.hasNewRelease).toBe(false)
    fetch.mockResolvedValue(first)
    await check()
    expect(monitor.hasNewRelease).toBe(true)
  })

  it('检查失败及离线重开保留未读提醒与上次成功检查时间', async () => {
    seed(first, next)
    const saved = localStorage.getItem(CACHE_KEY)
    const fetch = vi.spyOn(api, 'openSourceRelease').mockRejectedValue(new Error('无法连接 GitHub'))
    await render()
    expect(monitor.hasNewRelease).toBe(true)
    expect(container.querySelector('[role="alert"]')?.textContent).toBe('无法连接 GitHub')
    expect(localStorage.getItem(CACHE_KEY)).toBe(saved)
    await render({ newClient: true })
    expect(monitor.hasNewRelease).toBe(true)
    fetch.mockResolvedValue(next)
    const button = Array.from(container.querySelectorAll('button')).find(b => b.textContent === '检查开源版本')
    await act(async () => button?.click())
    await settle()
    expect(monitor.error).toBeNull()
    expect(monitor.hasNewRelease).toBe(true)
  })

  it('首次失败不记录基准，损坏缓存会重新建立基准', async () => {
    localStorage.setItem(CACHE_KEY, '{broken')
    const fetch = vi.spyOn(api, 'openSourceRelease').mockRejectedValue(new Error('网络失败'))
    await render()
    expect(monitor.hasNewRelease).toBe(false)
    expect(monitor.checkedAt).toBeUndefined()
    expect(localStorage.getItem(CACHE_KEY)).toBe('{broken')
    fetch.mockResolvedValue(first)
    await check()
    expect(monitor.hasNewRelease).toBe(false)
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)!)).toMatchObject({ seen: first })
  })

  it('无设置权限时不检查、不显示提醒', async () => {
    seed(first, next)
    const fetch = vi.spyOn(api, 'openSourceRelease').mockResolvedValue(next)
    await render({ enabled: false, panel: false })
    expect(fetch).not.toHaveBeenCalled()
    expect(monitor.hasNewRelease).toBe(false)
    expect(container.textContent).toBe('')
  })

  it('提醒入口在页面布局后滚动到开源监控，保留系统设置 tab', async () => {
    const scroll = vi.fn()
    const previousScroll = HTMLElement.prototype.scrollIntoView
    HTMLElement.prototype.scrollIntoView = scroll
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(0), 0))
    vi.stubGlobal('cancelAnimationFrame', clearTimeout)
    vi.spyOn(api, 'openSourceRelease').mockResolvedValue(first)
    try {
      await render({ entry: '/settings?tab=system&opensource=1' })
      expect(scroll).toHaveBeenCalledWith({ block: 'center', behavior: 'smooth' })
      expect(container.textContent).toContain('开源版本监控')
      expect(container.querySelector('[data-route]')?.getAttribute('data-route')).toBe('/settings?tab=system')
    } finally {
      HTMLElement.prototype.scrollIntoView = previousScroll
    }
  })

  it('打开应用期间每小时自动检查', async () => {
    vi.useFakeTimers()
    const fetch = vi.spyOn(api, 'openSourceRelease').mockResolvedValue(first)
    await act(async () => {
      root?.render(
        <QueryClientProvider client={qc}>
          <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <Harness panel={false} />
          </MemoryRouter>
        </QueryClientProvider>,
      )
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(fetch).toHaveBeenCalledTimes(1)
    fetch.mockResolvedValue(next)
    await act(async () => { await vi.advanceTimersByTimeAsync(60 * 60 * 1000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(monitor.hasNewRelease).toBe(true)
  })
})
