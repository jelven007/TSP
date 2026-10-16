import type { AuthStatus, AuthUser } from './api'

let status: AuthStatus | undefined
let confirmed = false

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

export function hasPermission(permission: string): boolean {
  if (!confirmed) return false
  if (!multitenancyEnabled()) return !status?.auth_required || !!status.authenticated
  return status?.user?.permissions?.includes(permission) === true
}

export function confirmAccount(next: AuthStatus): void {
  const changed = confirmed && accountScope() !== identityScope(next.user)
  status = next
  confirmed = true
  if (changed && window.location.pathname !== '/login') window.location.reload()
}

export function verifyResponseScope(scope: string | null): boolean {
  if (!scope || !confirmed || !multitenancyEnabled() || scope === accountScope()) return true
  window.location.reload()
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
  return status?.auth_required ? `${accountScope()}:${key}` : key
}

export const accountStorage = {
  getItem: (key: string) => window.localStorage.getItem(scopedStorageKey(key)),
  setItem: (key: string, value: string) => window.localStorage.setItem(scopedStorageKey(key), value),
  removeItem: (key: string) => window.localStorage.removeItem(scopedStorageKey(key)),
}

export function clearAccountCache(): void {
  const prefix = `${accountScope()}:`
  try {
    for (const key of Object.keys(window.localStorage)) {
      if (key.startsWith(prefix)) window.localStorage.removeItem(key)
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
    if (path.pathname.startsWith('/settings') && (path.pathname !== '/settings' || path.searchParams.get('tab') !== 'account')) return '/'
  }
  return path.pathname + path.search
}
