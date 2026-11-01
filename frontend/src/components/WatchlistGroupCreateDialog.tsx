import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type WatchlistGroupColor } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { DEFAULT_WATCHLIST_GROUP_COLOR } from '@/lib/watchlist-group-colors'
import { Modal } from './Modal'
import { GroupColorPicker } from './WatchlistGroups'

export function WatchlistGroupCreateDialog({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (groupId: string) => void
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [color, setColor] = useState<WatchlistGroupColor>(DEFAULT_WATCHLIST_GROUP_COLOR)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const create = useMutation({
    mutationFn: () => api.watchlistGroupCreate(name.trim(), color),
    onSuccess: result => {
      qc.setQueryData(QK.watchlistGroups, { groups: result.groups })
      void qc.invalidateQueries({ queryKey: QK.watchlistGroups })
      if (mounted.current) onCreated(result.group.id)
    },
    onError: err => setError(err instanceof Error ? err.message : '创建分组失败'),
  })
  const submit = () => {
    if (create.isPending) return
    if (!name.trim()) return setError('请输入分组名称')
    if (name.trim().length > 24) return setError('分组名称不能超过 24 个字符')
    setError('')
    create.mutate()
  }
  return <Modal size="sm" ariaLabel="新建自选分组" onClose={onClose} closeDisabled={create.isPending}>
    <div className="modal-header flex items-center justify-between">
      <span className="text-sm font-medium">新建自选分组</span>
      <button onClick={onClose} disabled={create.isPending} aria-label="关闭新建自选分组" className="modal-close">✕</button>
    </div>
    <div className="space-y-3 p-4">
      <label className="block text-xs text-muted">分组名称
        <input aria-label="新自选分组名称" maxLength={24} value={name} disabled={create.isPending}
          onChange={e => setName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') submit() }}
          autoFocus placeholder="输入分组名称"
          className="mt-1.5 h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
      </label>
      <div className="space-y-2"><span className="text-xs text-muted">分组颜色</span>
        <GroupColorPicker value={color} onChange={setColor} /></div>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </div>
    <div className="modal-footer flex justify-end gap-2">
      <button onClick={onClose} disabled={create.isPending} className="h-9 rounded-btn border border-border px-3 text-xs">取消</button>
      <button onClick={submit} disabled={create.isPending} className="h-9 rounded-btn bg-accent px-4 text-xs text-white disabled:opacity-50">
        {create.isPending ? '创建中…' : '创建并加入'}
      </button>
    </div>
  </Modal>
}
