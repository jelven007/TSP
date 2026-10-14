import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type NotificationChannel, type NotificationPreferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

export const CHANNELS_KEY = ['notification-channels'] as const
export const NOTIFICATION_PREFS_KEY = ['notification-preferences'] as const
const DELIVERY_KEY = ['notification-deliveries'] as const
const INPUT = 'h-9 rounded-btn border border-border bg-base px-3 text-xs text-foreground'
const STATUS_LABELS = { queued: '等待投递', sending: '投递中', sent: '已发送', dead: '投递失败', cancelled: '已取消' }

export function NotificationsPanel() {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<NotificationChannel | null>(null)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'email' | 'webhook'>('email')
  const [destination, setDestination] = useState('')
  const [enabled, setEnabled] = useState(true)
  const channels = useQuery({ queryKey: CHANNELS_KEY, queryFn: api.notificationChannels })
  const prefs = useQuery({ queryKey: NOTIFICATION_PREFS_KEY, queryFn: api.notificationPreferences })
  const deliveries = useQuery({ queryKey: DELIVERY_KEY, queryFn: api.notificationDeliveries, refetchInterval: 15_000 })
  const status = useQuery({ queryKey: QK.authStatus, queryFn: api.authStatus })
  const tenantEmail = status.data?.user?.role === 'tenant' ? status.data.user.email : null
  const reset = () => { setEditing(null); setName(''); setDestination(''); setKind('email'); setEnabled(true) }
  const save = useMutation({
    mutationFn: () => api.notificationSaveChannel({
      ...(editing ? { id: editing.id, version: editing.version } : {}),
      name, kind, destination: kind === 'email' && tenantEmail ? tenantEmail : destination, enabled,
    }),
    onSuccess: response => {
      qc.setQueryData(CHANNELS_KEY, response)
      void qc.invalidateQueries({ queryKey: DELIVERY_KEY })
      reset()
      toast('通知渠道已保存', 'success')
    },
  })
  const savePrefs = useMutation({
    mutationFn: api.notificationSavePreferences,
    onSuccess: value => qc.setQueryData(NOTIFICATION_PREFS_KEY, value),
  })
  const retry = useMutation({
    mutationFn: api.notificationRetry,
    onSuccess: () => { void qc.invalidateQueries({ queryKey: DELIVERY_KEY }) },
  })
  const edit = (channel: NotificationChannel) => {
    setEditing(channel); setName(channel.name); setKind(channel.kind)
    setDestination(channel.destination); setEnabled(channel.enabled)
  }
  const toggle = (key: 'default_channels' | 'review_channels', id: string) => {
    if (!prefs.data) return
    const values = prefs.data[key]
    savePrefs.mutate({ ...prefs.data, [key]: values.includes(id) ? values.filter(value => value !== id) : [...values, id] })
  }
  const submit = (event: FormEvent) => { event.preventDefault(); save.mutate() }
  const error = channels.error || prefs.error || deliveries.error || save.error || savePrefs.error || retry.error
  return (
    <section aria-label="通知配置与投递历史" className="mx-5 my-3 max-h-[70vh] shrink-0 space-y-4 overflow-y-auto rounded-card border border-border bg-surface p-4">
      <h2 className="text-sm font-medium text-foreground">通知配置与投递历史</h2>
      <p className="text-xs text-muted">渠道与投递记录属于当前租户。可为监控规则和复盘分别选择默认渠道。</p>
      {error && <p role="alert" className="text-xs text-danger">{error.message}</p>}
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
        <label className="space-y-1 text-xs text-secondary"><span className="block">渠道名称</span><input aria-label="渠道名称" className={INPUT} required maxLength={100} value={name} onChange={event => setName(event.target.value)} /></label>
        <label className="space-y-1 text-xs text-secondary"><span className="block">类型</span><select aria-label="通知类型" className={INPUT} value={kind} onChange={event => setKind(event.target.value as 'email' | 'webhook')}><option value="email">邮件</option><option value="webhook">Webhook</option></select></label>
        <label className="min-w-48 flex-1 space-y-1 text-xs text-secondary"><span className="block">{kind === 'email' ? '收件邮箱' : 'HTTPS Webhook 地址'}</span><input aria-label="通知目的地" required type={kind === 'email' ? 'email' : 'url'} className={`${INPUT} w-full`} value={kind === 'email' && tenantEmail ? tenantEmail : destination} readOnly={kind === 'email' && !!tenantEmail} onChange={event => setDestination(event.target.value)} /></label>
        <label className="flex h-9 items-center gap-2 text-xs"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />启用</label>
        <button type="submit" disabled={save.isPending || channels.isLoading} className="h-9 rounded-btn bg-accent px-3 text-xs text-white disabled:opacity-50">{editing ? '保存修改' : '添加渠道'}</button>
        {editing && <button type="button" className="h-9 px-2 text-xs text-secondary" onClick={reset}>取消编辑</button>}
      </form>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs"><thead className="border-b border-border text-muted"><tr><th className="py-2">渠道</th><th>状态</th><th>监控默认</th><th>复盘推送</th><th>操作</th></tr></thead>
          <tbody>{channels.data?.channels.map(channel => (
            <tr key={channel.id} className="border-b border-border/50">
              <td className="py-3 pr-4"><span className="block text-foreground">{channel.name}</span><span className="text-[10px] text-muted">{channel.destination}</span></td>
              <td>{channel.enabled ? '启用' : '停用'}</td>
              <td><input aria-label={`${channel.name} 监控默认`} type="checkbox" checked={prefs.data?.default_channels.includes(channel.id) ?? false} disabled={savePrefs.isPending || !prefs.data} onChange={() => toggle('default_channels', channel.id)} /></td>
              <td><input aria-label={`${channel.name} 复盘推送`} type="checkbox" checked={prefs.data?.review_channels.includes(channel.id) ?? false} disabled={savePrefs.isPending || !prefs.data} onChange={() => toggle('review_channels', channel.id)} /></td>
              <td><button type="button" className="text-accent" onClick={() => edit(channel)}>编辑</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <label className="flex items-center gap-2 text-xs text-secondary">
        复盘归档后
        <select aria-label="复盘推送触发方式" className={INPUT} value={prefs.data?.review_push_mode ?? 'manual'} disabled={!prefs.data || savePrefs.isPending} onChange={event => { if (prefs.data) savePrefs.mutate({ ...prefs.data, review_push_mode: event.target.value as NotificationPreferences['review_push_mode'] }) }}>
          <option value="manual">仅保存报告</option><option value="auto">自动推送</option>
        </select>
      </label>
      <h3 className="border-t border-border pt-4 text-xs font-medium">最近投递</h3>
      {!deliveries.data?.deliveries.length && <p className="text-xs text-muted">{deliveries.isLoading ? '加载中…' : '暂无投递记录'}</p>}
      <div className="space-y-2">
        {deliveries.data?.deliveries.map(delivery => (
          <div key={delivery.id} className="flex items-center gap-3 border-b border-border/40 pb-2 text-xs">
            <span className="min-w-0 flex-1">{channels.data?.channels.find(channel => channel.id === delivery.channel_id)?.name || '通知渠道'} · {STATUS_LABELS[delivery.status]}{delivery.error ? ` · ${delivery.error}` : ''}<span className="mt-0.5 block text-[10px] text-muted">{delivery.created_at.slice(0, 19).replace('T', ' ')} · 尝试 {delivery.attempts} 次</span></span>
            {delivery.status === 'dead' && <button type="button" disabled={retry.isPending} className="text-accent disabled:opacity-50" onClick={() => retry.mutate(delivery.id)}>重试</button>}
          </div>
        ))}
      </div>
    </section>
  )
}
