import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { clearAccountCache, hasPermission, multitenancyEnabled } from '@/lib/accountSession'
import { QK } from '@/lib/queryKeys'

export function TenantSelector({ compact = false }: { compact?: boolean }) {
  const qc = useQueryClient()
  const enabled = multitenancyEnabled() && hasPermission('users:manage')
  const status = useQuery({ queryKey: QK.authStatus, queryFn: api.authStatus, enabled })
  const tenants = useQuery({ queryKey: ['account-tenants'], queryFn: api.accountTenants, enabled, staleTime: 30_000 })
  const select = useMutation({
    mutationFn: api.accountSelectTenant,
    onSuccess: async () => {
      await qc.cancelQueries()
      clearAccountCache()
      qc.clear()
      window.location.replace('/')
    },
  })
  if (!enabled) return null
  return (
    <label className="block border-b border-border px-3 py-2">
      {!compact && <span className="mb-1 block text-[10px] text-muted">当前业务租户</span>}
      <select
        aria-label="当前业务租户"
        title="选择管理的业务租户"
        className="h-8 w-full rounded-btn border border-border bg-base px-1 text-xs text-foreground disabled:opacity-50"
        value={status.data?.user?.default_tenant_id ?? ''}
        disabled={tenants.isLoading || select.isPending}
        onChange={event => { if (event.target.value) select.mutate(event.target.value) }}
      >
        <option value="" disabled>选择租户</option>
        {tenants.data?.tenants.map(tenant => (
          <option key={tenant.id} value={tenant.id} disabled={tenant.status !== 'active'}>
            {tenant.display_name ?? tenant.name}{tenant.status !== 'active' ? '（不可用）' : ''}
          </option>
        ))}
      </select>
      {select.error && <span role="alert" className="mt-1 block text-[10px] text-danger">{select.error.message}</span>}
    </label>
  )
}
