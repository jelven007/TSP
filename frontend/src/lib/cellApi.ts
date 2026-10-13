import { request } from './api/client'
import type { WatchlistEntry, WatchlistGroup } from './api'

export type CellRole = 'owner' | 'admin' | 'member' | 'viewer'
export interface Workspace {
  id: string; name: string; cell_id: string; api_url: string
  routing_epoch: number; policy_version: number; status: string; role: CellRole
}
export interface CellStrategy {
  id: string; name: string; template_id: string; version: number
  overrides: Record<string, unknown>
}
export interface CellTemplate {
  id: string; code_hash: string
  definition: {
    name: string; description: string
    params: Array<{ id: string; label: string; type: string; default: unknown; min?: number; max?: number; options?: string[] }>
    basic_filter: Record<string, unknown>
  }
}
export interface CellJob {
  id: string; strategy_id: string; status: string; error: string | null
  result: { total: number; rows: Array<{ symbol: string; name?: string; score?: number }> } | null
}
export interface CellRule {
  id: string; enabled: boolean; version: number
  definition: {
    id?: string; name: string; symbols: string[]; field: 'price' | 'change_pct'
    op: '>' | '>=' | '<' | '<='; value: number; enabled: boolean
    cooldown_seconds: number; severity: 'info' | 'warn' | 'critical'
  }
}
export interface CellAlert {
  id: string; created_at: string; read_at: string | null
  event: { source: string; message: string; symbol?: string; price?: number; severity: string }
}
export interface CellAccount {
  id: string; name: string; initial_cash: number; cash: number; status: string
}
export interface CellOrder {
  id: string; symbol: string; side: string; qty: number; status: string; reason: string | null
}
export interface CellOverview {
  account: CellAccount
  holdings: Array<{ symbol: string; qty: number; available_qty: number; avg_cost: number }>
  orders: CellOrder[]
  fills: Array<{ id: string; symbol: string; side: string; qty: number; price: number; fee: number }>
}
export interface CellChannel {
  id: string; version: number; name: string; kind: 'email' | 'webhook'; destination: string; enabled: boolean
}
export interface CellDelivery {
  id: string; channel_id: string; status: string; attempts: number; error: string | null; created_at: string
}
export interface CellAutomation {
  id: string; version: number; name: string; strategy_id: string; account_id: string; enabled: boolean
  run_time: string; max_positions: number; budget_per_order: number; stop_loss_pct: number
  take_profit_pct: number; max_daily_orders: number; error: string | null; last_run_date: string | null
}
export interface CellIntent {
  id: string; automation_id: string; symbol: string; side: string; status: string; reason: string | null
}
export const CELL_AUTH_ORIGIN = import.meta.env.VITE_CELL_AUTH_ORIGIN || ''
export const CELL_MODE = import.meta.env.VITE_CELL_MODE === 'true'

export function safeCellOrigin(url: string): string {
  const parsed = new URL(url, window.location.origin)
  if (parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error('工作空间地址无效')
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(parsed.hostname))) {
    throw new Error('工作空间地址必须使用 HTTPS')
  }
  return parsed.origin
}

export function cellSession(token: string) {
  const headers = { Authorization: `Bearer ${token}` }
  return {
    directory: (signal?: AbortSignal) => request<{ tenants: Workspace[] }>('/api/tenants', {
      baseUrl: CELL_AUTH_ORIGIN, headers, signal, quiet: true,
    }),
    logout: () => request<void>('/api/auth/logout', { baseUrl: CELL_AUTH_ORIGIN, headers, method: 'POST', quiet: true }),
  }
}

export function loginCell(email: string, password: string) {
  return request<{ token: string; user: { id: string; email: string } }>('/api/auth/login', {
    baseUrl: CELL_AUTH_ORIGIN, method: 'POST', body: JSON.stringify({ email, password }), quiet: true,
  })
}

export function workspaceClient(workspace: Workspace, token: string, lifetime?: AbortSignal) {
  const baseUrl = safeCellOrigin(workspace.api_url)
  const prefix = `/api/tenants/${encodeURIComponent(workspace.id)}`
  const headers = { Authorization: `Bearer ${token}`, 'X-Routing-Epoch': String(workspace.routing_epoch) }
  const call = async <T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) => {
    const activeSignal = lifetime && signal ? AbortSignal.any([lifetime, signal]) : lifetime ?? signal
    activeSignal?.throwIfAborted()
    const result = await request<T>(prefix + path, {
      baseUrl, headers, method, body: body === undefined ? undefined : JSON.stringify(body), signal: activeSignal, quiet: true,
    })
    activeSignal?.throwIfAborted()
    return result
  }
  return {
    watchlist: (signal?: AbortSignal) => call<{ symbols: WatchlistEntry[]; groups: WatchlistGroup[] }>('/watchlist', 'GET', undefined, signal),
    addWatchlist: (symbols: string[]) => call('/watchlist', 'POST', { symbols }),
    removeWatchlist: (symbol: string) => call(`/watchlist/items/${encodeURIComponent(symbol)}`, 'DELETE'),
    templates: (signal?: AbortSignal) => call<{ templates: CellTemplate[] }>('/strategy-templates', 'GET', undefined, signal),
    strategies: (signal?: AbortSignal) => call<{ strategies: CellStrategy[] }>('/strategies', 'GET', undefined, signal),
    saveStrategy: (body: { name: string; template_id: string; overrides: Record<string, unknown>; version?: number }, id?: string) =>
      call(`/strategies${id ? `/${encodeURIComponent(id)}` : ''}`, id ? 'PUT' : 'POST', body),
    runStrategy: (id: string, as_of: string, key: string) => call(`/strategies/${encodeURIComponent(id)}/run`, 'POST', { as_of, idempotency_key: key }),
    jobs: (signal?: AbortSignal) => call<{ jobs: CellJob[] }>('/strategy-jobs', 'GET', undefined, signal),
    cancelJob: (id: string) => call(`/strategy-jobs/${encodeURIComponent(id)}/cancel`, 'POST'),
    rules: (signal?: AbortSignal) => call<{ rules: CellRule[] }>('/monitor-rules', 'GET', undefined, signal),
    saveRule: (rule: CellRule['definition']) => call('/monitor-rules', 'POST', rule),
    deleteRule: (id: string) => call(`/monitor-rules/${encodeURIComponent(id)}`, 'DELETE'),
    alerts: (signal?: AbortSignal) => call<{ alerts: CellAlert[] }>('/alerts', 'GET', undefined, signal),
    readAlert: (id: string) => call(`/alerts/${encodeURIComponent(id)}/read`, 'POST'),
    accounts: (signal?: AbortSignal) => call<{ accounts: CellAccount[] }>('/paper/accounts', 'GET', undefined, signal),
    createAccount: (name: string, initial_cash: number) => call('/paper/accounts', 'POST', { name, initial_cash }),
    overview: (id: string, signal?: AbortSignal) => call<CellOverview>(`/paper/accounts/${encodeURIComponent(id)}`, 'GET', undefined, signal),
    freeze: (id: string, frozen: boolean) => call(`/paper/accounts/${encodeURIComponent(id)}/freeze?frozen=${frozen}`, 'POST'),
    order: (account: string, body: { symbol: string; side: 'buy' | 'sell'; qty: number; idempotency_key: string }) =>
      call(`/paper/accounts/${encodeURIComponent(account)}/orders`, 'POST', body),
    cancelOrder: (account: string, order: string) => call(`/paper/accounts/${encodeURIComponent(account)}/orders/${encodeURIComponent(order)}`, 'DELETE'),
    channels: (signal?: AbortSignal) => call<{ channels: CellChannel[] }>('/notification-channels', 'GET', undefined, signal),
    saveChannel: (body: Omit<CellChannel, 'id' | 'version'> & { id?: string; version?: number }) => call('/notification-channels', 'POST', body),
    deliveries: (signal?: AbortSignal) => call<{ deliveries: CellDelivery[] }>('/notification-deliveries', 'GET', undefined, signal),
    retryDelivery: (id: string) => call(`/notification-deliveries/${encodeURIComponent(id)}/retry`, 'POST'),
    automations: (signal?: AbortSignal) => call<{ automations: CellAutomation[] }>('/trade-automations', 'GET', undefined, signal),
    saveAutomation: (body: Omit<CellAutomation, 'id' | 'version' | 'error' | 'last_run_date'> & { id?: string; version?: number }) =>
      call('/trade-automations', 'POST', body),
    intents: (signal?: AbortSignal) => call<{ intents: CellIntent[] }>('/trade-intents', 'GET', undefined, signal),
  }
}

export type WorkspaceClient = ReturnType<typeof workspaceClient>
