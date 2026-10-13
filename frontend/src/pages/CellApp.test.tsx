// @vitest-environment jsdom
import { StrictMode, act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CellApp } from './CellApp'

vi.mock('@/components/Logo', () => ({ Logo: () => <span>TSP</span> }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | null = null
afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  root = null
  sessionStorage.clear()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
}

describe('workspace switching', () => {
  it('isolates private rows across tenants and clears write controls for viewers', async () => {
    sessionStorage.setItem('tsp-cell-session-v1', 'session')
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const body = url.endsWith('/api/tenants') ? { tenants: [
        { id: 'a', name: 'A团队', role: 'member', status: 'active', cell_id: 'a', api_url: 'https://a.test', routing_epoch: 1, policy_version: 1 },
        { id: 'b', name: 'B团队', role: 'viewer', status: 'active', cell_id: 'b', api_url: 'https://b.test', routing_epoch: 1, policy_version: 1 },
      ] } : { symbols: [{ symbol: url.includes('/tenants/a/') ? '000001.SZ' : '600000.SH', note: '工作空间数据' }], groups: [] }
      return new Response(JSON.stringify(body), { status: 200 })
    }))
    const container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => { root?.render(<StrictMode><CellApp /></StrictMode>) })
    await settle()
    await settle()
    expect(document.body.textContent).toContain('000001.SZ')
    expect(document.querySelector('input[aria-label="证券代码"]')).not.toBeNull()
    const selector = document.querySelector<HTMLSelectElement>('select[aria-label="切换工作空间"]')!
    await act(async () => { selector.value = 'b'; selector.dispatchEvent(new Event('change', { bubbles: true })) })
    await settle()
    expect(document.body.textContent).toContain('600000.SH')
    expect(document.body.textContent).not.toContain('000001.SZ')
    expect(document.querySelector('input[aria-label="证券代码"]')).toBeNull()
    expect(document.body.textContent).toContain('只读权限')
  })
  it('shows isolated delivery and automation states and hides viewer writes', async () => {
    sessionStorage.setItem('tsp-cell-session-v1', 'session')
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const workspace = url.includes('/tenants/b/')
      const data = url.endsWith('/api/tenants') ? { tenants: [
        { id: 'a', name: 'A', role: 'owner', status: 'active', cell_id: 'a', api_url: 'https://a.test', routing_epoch: 1, policy_version: 1 },
        { id: 'b', name: 'B', role: 'viewer', status: 'active', cell_id: 'b', api_url: 'https://b.test', routing_epoch: 1, policy_version: 1 },
      ] } : url.endsWith('/watchlist') ? { symbols: [], groups: [] }
        : url.endsWith('/notification-channels') ? { channels: workspace ? [] : [{ id: 'ch', name: '私有邮箱', kind: 'email', destination: 'private@example.test', enabled: true, version: 1 }] }
          : url.endsWith('/notification-deliveries') ? { deliveries: workspace ? [] : [{ id: 'del', channel_id: 'ch', status: 'dead', attempts: 6, error: 'notification_connection_failed' }] }
            : url.endsWith('/monitor-rules') ? { rules: [] }
              : url.endsWith('/alerts') ? { alerts: [] }
                : url.endsWith('/paper/accounts') ? { accounts: [{ id: 'account', name: '模拟', cash: 10000, status: 'active' }] }
                  : url.endsWith('/trade-automations') ? { automations: workspace ? [] : [{ id: 'auto', name: '私有自动交易', account_id: 'account', strategy_id: 's', run_time: '09:35:00', enabled: true, version: 1, max_positions: 2, budget_per_order: 2000, stop_loss_pct: .05, take_profit_pct: .1, max_daily_orders: 4 }] }
                    : url.endsWith('/trade-intents') ? { intents: [] }
                      : url.endsWith('/strategies') ? { strategies: [] }
                        : { account: { cash: 10000 }, holdings: [], orders: [], fills: [] }
      return new Response(JSON.stringify(data), { status: 200 })
    }))
    const container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => root?.render(<CellApp />))
    await settle()
    await settle()
    const click = async (label: string) => {
      const button = Array.from(document.querySelectorAll('button')).find(b => b.textContent === label)!
      await act(async () => button.click())
      await settle()
    }
    await click('告警')
    expect(document.body.textContent).toContain('private@example.test')
    expect(document.body.textContent).toContain('重试投递')
    await click('模拟盘')
    await settle()
    expect(document.body.textContent).toContain('暂停自动交易')
    expect(document.body.textContent).toContain('止损 5.0%')
    const select = document.querySelector<HTMLSelectElement>('select[aria-label="切换工作空间"]')!
    await act(async () => { select.value = 'b'; select.dispatchEvent(new Event('change', { bubbles: true })) })
    await settle()
    await click('告警')
    expect(document.body.textContent).not.toContain('private@example.test')
    expect(document.body.textContent).not.toContain('启用通知')
    await click('模拟盘')
    await settle()
    expect(document.body.textContent).not.toContain('暂停自动交易')
    expect(document.body.textContent).not.toContain('启用模拟自动交易')
  })
})
