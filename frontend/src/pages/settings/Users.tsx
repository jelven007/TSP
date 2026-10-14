import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ManagedUser } from '@/lib/api'
import { hasPermission, multitenancyEnabled } from '@/lib/accountSession'
import { toast } from '@/components/Toast'

export function UsersPanel() {
  const qc = useQueryClient()
  const [secret, setSecret] = useState('')
  const [pending, setPending] = useState<{ user: ManagedUser; role: 'admin' | 'tenant'; active: boolean } | null>(null)
  const users = useQuery({ queryKey: ['admin-users'], queryFn: api.adminUsers })
  const update = useMutation({
    mutationFn: (change: NonNullable<typeof pending>) => api.adminUpdateUser(change.user.id, change.role, change.active),
    onSuccess: () => { setPending(null); void qc.invalidateQueries({ queryKey: ['admin-users'] }) },
  })
  const changeSecret = useMutation({
    mutationFn: api.adminRegistrationSecret,
    onSuccess: () => { setSecret(''); toast('注册口令已更新', 'success') },
  })
  const submitSecret = (event: FormEvent) => {
    event.preventDefault()
    if (secret.trim()) changeSecret.mutate(secret)
  }
  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <h3 className="text-sm font-medium text-foreground">用户与权限</h3>
      <p className="mt-1 text-xs text-muted">角色或状态变更后，该用户的登录会话与 API Token 会失效。</p>
      {users.error && <p role="alert" className="mt-3 text-xs text-danger">{users.error.message}</p>}
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-border text-muted"><tr><th className="py-2">账户</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {users.data?.users.map(user => (
              <tr key={user.id} className="border-b border-border/50">
                <td className="py-3 pr-3 text-foreground">{user.username || user.email}</td>
                <td>{user.roles.includes('admin') ? '管理员' : '租户'}</td>
                <td>{user.account_state === 'pending' ? '初始化中' : user.active ? '正常' : '停用'}</td>
                <td className="space-x-3 whitespace-nowrap">
                  <button type="button" disabled={user.account_state === 'pending'} className="text-accent disabled:opacity-40" onClick={() => setPending({ user, role: user.roles.includes('admin') ? 'tenant' : 'admin', active: user.active })}>
                    {user.roles.includes('admin') ? '设为租户' : '设为管理员'}
                  </button>
                  <button type="button" disabled={user.account_state === 'pending'} className="text-accent disabled:opacity-40" onClick={() => setPending({ user, role: user.roles.includes('admin') ? 'admin' : 'tenant', active: !user.active })}>{user.active ? '停用' : '启用'}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {users.isLoading && <p className="py-4 text-xs text-muted">加载中…</p>}
      {pending && (
        <div role="alertdialog" aria-label="确认权限变更" className="mt-4 rounded-btn border border-warning/30 p-3 text-xs">
          <p>将 {pending.user.username || pending.user.email} 设为{pending.role === 'admin' ? '管理员' : '租户'}，状态为{pending.active ? '启用' : '停用'}。</p>
          <div className="mt-3 flex gap-3">
            <button type="button" disabled={update.isPending} className="rounded-btn bg-accent px-3 py-2 text-white disabled:opacity-50" onClick={() => update.mutate(pending)}>确认变更</button>
            <button type="button" disabled={update.isPending} onClick={() => setPending(null)}>取消</button>
          </div>
          {update.error && <p role="alert" className="mt-2 text-danger">{update.error.message}</p>}
        </div>
      )}
      <form className="mt-5 border-t border-border pt-4" onSubmit={submitSecret}>
        <label className="block text-xs text-secondary" htmlFor="registration-secret">中文注册口令</label>
        <div className="mt-2 flex max-w-md gap-2">
          <input id="registration-secret" type="password" autoComplete="new-password" maxLength={128} value={secret} onChange={event => setSecret(event.target.value)} className="min-w-0 flex-1 rounded-btn border border-border bg-base px-3 text-sm" placeholder="输入新的注册口令" />
          <button type="submit" disabled={!secret.trim() || changeSecret.isPending} className="rounded-btn bg-accent px-3 py-2 text-xs text-white disabled:opacity-50">更新口令</button>
        </div>
        {changeSecret.error && <p role="alert" className="mt-2 text-xs text-danger">{changeSecret.error.message}</p>}
      </form>
    </section>
  )
}

export function AdminUsersPanel() {
  return multitenancyEnabled() && hasPermission('users:manage') ? <UsersPanel /> : null
}
