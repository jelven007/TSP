import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link2, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api, type WatchlistEntry, type WatchlistGroup } from '@/lib/api'
import { accountScope } from '@/lib/accountSession'
import { QK } from '@/lib/queryKeys'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'

export type WatchlistStrategyOption = { id: string; name: string }

export const watchlistStrategyQuery = () => ({
  queryKey: QK.watchlistStrategyOptions(),
  queryFn: () => api.strategyList(undefined, 'all'),
  staleTime: 30_000,
})

export function WatchlistRelations({ entry, groups, strategies, onEdit }: {
  entry: WatchlistEntry
  groups: WatchlistGroup[]
  strategies: WatchlistStrategyOption[]
  onEdit?: () => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-1 mt-1 text-[10px] leading-normal">
      {(entry.group_ids ?? []).map(id => {
        const group = groups.find(item => item.id === id)
        const color = resolveWatchlistGroupColor(group?.color)
        return <span key={id} className={`max-w-full break-words px-1.5 py-0.5 rounded ${color.background} ${color.text}`}>{group?.name ?? '分组不可用'}</span>
      })}
      {(entry.strategy_ids ?? []).map(id => (
        <span key={id} className="max-w-full break-words px-1.5 py-0.5 rounded bg-accent/10 text-accent">
          {strategies.find(item => item.id === id)?.name ?? `策略不可用（${id}）`}
        </span>
      ))}
      {onEdit && <button type="button" onClick={event => { event.stopPropagation(); onEdit() }}
        className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-muted hover:text-accent hover:bg-accent/10"
        aria-label={`关联策略 ${entry.symbol}`} title="设置这只股票关联的策略">
        <Link2 className="h-3 w-3" />关联策略
      </button>}
    </div>
  )
}

export function WatchlistStrategyDialog({ entry, onClose, onSaved }: {
  entry: WatchlistEntry
  onClose: () => void
  onSaved: (symbols: WatchlistEntry[]) => void
}) {
  const catalog = useQuery(watchlistStrategyQuery())
  const [selected, setSelected] = useState(() => [...new Set(entry.strategy_ids ?? [])])
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const options = catalog.data?.strategies ?? []
  const missing = selected.filter(id => !options.some(option => option.id === id))
  const toggle = (id: string) => setSelected(prev => prev.includes(id) ? prev.filter(value => value !== id) : [...prev, id])

  const save = async () => {
    if (pending) return
    const scope = accountScope()
    setPending(true)
    setError('')
    try {
      const { symbols } = await api.watchlistSetStrategies(entry.symbol, selected)
      if (mounted.current && scope === accountScope()) onSaved(symbols)
    } catch (err) {
      if (mounted.current && scope === accountScope()) setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      if (mounted.current && scope === accountScope()) setPending(false)
    }
  }

  return (
    <Modal onClose={onClose} ariaLabel="关联策略" panelClassName="p-5" closeDisabled={pending}>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-foreground">关联策略 · {entry.symbol}</h3>
        <button onClick={onClose} disabled={pending} aria-label="关闭关联策略" className="text-muted hover:text-foreground"><X className="h-4 w-4" /></button>
      </div>
      <p className="text-xs text-secondary mb-3">可为这只股票选择多个策略，作为自选标记。</p>
      {catalog.isLoading ? <p className="text-xs text-muted">加载策略中…</p>
        : catalog.isError ? <div className="text-xs text-danger">读取策略失败 <button onClick={() => catalog.refetch()} className="text-accent">重试</button></div>
        : <div className="max-h-72 overflow-y-auto space-y-1">
          {[...options, ...missing.map(id => ({ id, name: `策略不可用（${id}）` }))].map(option => (
            <label key={option.id} className="flex items-center gap-2 rounded p-2 text-xs hover:bg-elevated cursor-pointer">
              <input type="checkbox" checked={selected.includes(option.id)} disabled={pending} onChange={() => toggle(option.id)} />
              <span className="break-words">{option.name}</span>
            </label>
          ))}
          {!options.length && !missing.length && <p className="text-xs text-muted">暂无可关联的策略</p>}
        </div>}
      {error && <p role="alert" className="mt-3 text-xs text-danger">{error}</p>}
      <div className="flex items-center justify-end gap-2 mt-4">
        <button onClick={() => setSelected([])} disabled={pending || !selected.length}
          className="mr-auto text-xs text-muted hover:text-foreground disabled:opacity-40">解除全部关联</button>
        <button onClick={onClose} disabled={pending} className="px-3 py-1.5 rounded-btn bg-elevated text-xs">取消</button>
        <button onClick={save} disabled={pending || (!catalog.isSuccess && selected.length > 0)}
          className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs disabled:opacity-50">{pending ? '保存中…' : '保存'}</button>
      </div>
    </Modal>
  )
}
