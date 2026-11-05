import type { AuthStatus, AuthUser } from './api'

let status: AuthStatus | undefined
let confirmed = false
let scopeRevision = 0

export type AccountScopeChange = {
  previousScope: string
  nextScope: string
  status: AuthStatus | undefined
  revision: number
}

const scopeListeners = new Set<(change: AccountScopeChange) => void>()
let scopeSnapshot: AccountScopeChange = {
  previousScope: 'tsp-v1:signed-out',
  nextScope: 'tsp-v1:signed-out',
  status: undefined,
  revision: scopeRevision,
}

export function identityScope(account: AuthUser | null): string {
  return account
    ? `tsp-v1:${account.id}:${account.default_tenant_id || 'market'}:${account.auth_version ?? 0}`
    : 'tsp-v1:signed-out'
}

export function accountScope(): string {
  return identityScope(status?.user ?? null)
}

export function multitenancyEnabled(): boolean {
  return status?.multitenancy_enabled === true
}

export function localAdminMode(): boolean {
  return status?.local_admin_mode === true
}

export function hasPermission(permission: string): boolean {
  if (!confirmed) return false
  if (!multitenancyEnabled()) return !status?.auth_required || !!status.authenticated
  return status?.user?.permissions?.includes(permission) === true
}

export function confirmAccount(next: AuthStatus): void {
  const previousScope = accountScope()
  const nextScope = identityScope(next.user)
  const changed = confirmed && previousScope !== nextScope
  status = next
  confirmed = true
  if (!changed) {
    scopeSnapshot = { previousScope: nextScope, nextScope, status: next, revision: scopeRevision }
    return
  }
  scopeRevision += 1
  scopeSnapshot = { previousScope, nextScope, status: next, revision: scopeRevision }
  scopeListeners.forEach(listener => listener(scopeSnapshot))
}

export function subscribeAccountScopeChanges(listener: (change: AccountScopeChange) => void): () => void {
  scopeListeners.add(listener)
  return () => scopeListeners.delete(listener)
}

export function getAccountScopeSnapshot(): AccountScopeChange {
  return scopeSnapshot
}

export function verifyResponseScope(scope: string | null): boolean {
  if (!scope || !confirmed || !multitenancyEnabled() || scope === accountScope()) return true
  return false
}

export async function accountFetch(path: string, init?: RequestInit): Promise<Response> {
  const startedScope = accountScope()
  const headers = new Headers(init?.headers)
  if (multitenancyEnabled() && path.startsWith('/api/') && !path.startsWith('/api/auth/')) {
    headers.set('X-TSP-Account-Scope', startedScope)
  }
  const response = await globalThis.fetch(path, { ...init, headers })
  if (startedScope !== accountScope() || !verifyResponseScope(response.headers.get('X-TSP-Account-Scope'))) {
    throw new DOMException('账户已切换', 'AbortError')
  }
  return response
}

export function accountStreamUrl(path: string): string {
  if (!multitenancyEnabled()) return path
  const url = new URL(path, window.location.origin)
  url.searchParams.set('account_scope', accountScope())
  return url.pathname + url.search
}

export async function accountEventStreamUrl(lastEventId = ''): Promise<string> {
  if (!multitenancyEnabled()) return accountStreamUrl('/api/intraday/stream')
  const response = await accountFetch('/api/events/ticket', { method: 'POST' })
  if (!response.ok) throw new Error(`事件流授权失败 (${response.status})`)
  const result = await response.json() as { stream?: string }
  const url = new URL(result.stream || '', window.location.origin)
  if (url.origin !== window.location.origin || url.pathname !== '/api/events' || !url.searchParams.get('ticket')) {
    throw new Error('事件流授权响应无效')
  }
  if (lastEventId) url.searchParams.set('last_event_id', lastEventId)
  return url.pathname + url.search
}

export function scopedStorageKey(key: string): string {
  if (!confirmed) throw new Error('请先确认登录身份')
  if (!status?.auth_required) return key
  return PERSISTENT_PREFERENCES.has(key)
    ? preferenceKey(accountScope(), key)
    : `${accountScope()}:${key}`
}

// Only durable UI configuration is retained. Drafts, task IDs, query caches and
// private result snapshots keep the auth-version scope and are cleared.
const PERSISTENT_PREFERENCES = new Set([
  'strategy-pool', 'strategy-pool-1m', 'tf-stocks-query-config',
  'watchlist_columns', 'stock_info_bar_fields', 'stock_volume_compare',
  'stock_preview_intraday_days', 'stock_external_template', 'screener_result_columns',
  'strategy_signal_columns',
  'watchlist_view', 'watchlist_showCandle', 'watchlist_showIntraday',
  'screener_showCandle', 'screener_showIntraday', 'screener_strategyTagsExpanded',
  'watchlist_boardFilter', 'watchlist_excludeST', 'watchlist_groupStats',
  'abnormal_enabled', 'screener-card-size', 'tf-page-zoom',
  'limit-ladder-board-filter', 'limit-ladder-ext-fields', 'limit-ladder-show-ext',
  'limit-ladder-direction', 'limit-ladder-seal-mode', 'default-strategy-basic-filter',
  'strategy-backtest-quick-ranges', 'concept-analysis-config', 'industry-analysis-config',
  'data-card-visible', 'data-card-order', 'tf-nav-state', 'tf-nav-collapsed',
  'tf-settings-nav-collapsed', 'monitor_badge_enabled', 'alert_toast_enabled',
  'alert_toast_max', 'alert_sound_enabled', 'alert_sound', 'voice_broadcast_enabled',
  'voice_broadcast_voice', 'voice_broadcast_rate', 'tsp_adj_factor_no_remind',
  'ai_bubble_pos', 'sa_bubble_pos', 'assistant.width.v1', 'assistant.fab.pos.v1',
  'paper.newAccountDefaults',
])

function preferenceKey(scope: string, key: string): string {
  return `${scope.replace(/:\d+$/, ':preferences')}:${key}`
}

function legacyPreferenceKeys(scope: string, key: string): string[] {
  const prefix = `${scope.replace(/:\d+$/, '')}:`
  const suffix = `:${key}`
  const version = (candidate: string) => candidate.slice(prefix.length, -suffix.length)
  return Object.keys(window.localStorage)
    .filter(candidate => candidate.startsWith(prefix) && candidate.endsWith(suffix)
      && /^\d+$/.test(version(candidate)))
    .sort((left, right) => Number(version(right)) - Number(version(left)))
}

export const accountStorage = {
  getItem: (key: string) => {
    const target = scopedStorageKey(key)
    const current = window.localStorage.getItem(target)
    if (current !== null || !status?.auth_required || !PERSISTENT_PREFERENCES.has(key)) return current
    const legacy = legacyPreferenceKeys(accountScope(), key)[0]
    if (!legacy) return null
    const value = window.localStorage.getItem(legacy)
    if (value !== null) {
      try {
        window.localStorage.setItem(target, value)
        window.localStorage.removeItem(legacy)
      } catch { /* Keep readable legacy preferences if storage is full. */ }
    }
    return value
  },
  setItem: (key: string, value: string) => window.localStorage.setItem(scopedStorageKey(key), value),
  removeItem: (key: string) => {
    window.localStorage.removeItem(scopedStorageKey(key))
    if (status?.auth_required && PERSISTENT_PREFERENCES.has(key)) {
      for (const legacy of legacyPreferenceKeys(accountScope(), key)) window.localStorage.removeItem(legacy)
    }
  },
}

export function clearAccountCache(scope = accountScope()): void {
  const prefix = `${scope}:`
  try {
    for (const key of Object.keys(window.localStorage)) {
      if (!key.startsWith(prefix)) continue
      const name = key.slice(prefix.length)
      if (PERSISTENT_PREFERENCES.has(name)) {
        const target = preferenceKey(scope, name)
        if (window.localStorage.getItem(target) === null) {
          try {
            window.localStorage.setItem(target, window.localStorage.getItem(key)!)
          } catch { continue /* Retain the legacy preference; still clear private caches. */ }
        }
      }
      window.localStorage.removeItem(key)
    }
    window.localStorage.removeItem('tf-query-cache')
  } catch { /* Browser storage may be unavailable. */ }
}

export function safeLoginRedirect(value: string | null): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/'
  const path = new URL(value, window.location.origin)
  if (path.origin !== window.location.origin || path.pathname === '/login') return '/'
  if (multitenancyEnabled() && !hasPermission('settings:manage')) {
    if (['/data', '/dev', '/onboarding'].some(prefix => path.pathname === prefix || path.pathname.startsWith(prefix + '/'))) return '/'
    if (path.pathname.startsWith('/settings') && (path.pathname !== '/settings' || !['account', 'system'].includes(path.searchParams.get('tab') ?? ''))) return '/'
  }
  return path.pathname + path.search
}
