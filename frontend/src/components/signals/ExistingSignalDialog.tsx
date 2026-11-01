import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, Loader2, Search, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { customSignalColumn, lateSessionSummary, morningLiquidationSummary } from '@/lib/customSignals'

interface Props {
  kind: 'entry' | 'exit'
  signals: string[]
  allowIntraday: boolean
  onChange: (signals: string[]) => void
  onClose: () => void
}

export function ExistingSignalDialog({ kind, signals, allowIntraday, onChange, onClose }: Props) {
  const [draft, setDraft] = useState(() => [...signals])
  const [search, setSearch] = useState('')
  const query = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  const label = kind === 'entry' ? '入场' : '出场'
  const keyword = search.trim().toLowerCase()
  const list = (query.data?.signals ?? []).filter(signal =>
    (signal.kind === kind || signal.kind === 'both')
    && (!keyword || `${signal.name} ${signal.id}`.toLowerCase().includes(keyword)))
  const toggle = (id: string) => setDraft(previous =>
    previous.includes(id) ? previous.filter(value => value !== id) : [...previous, id])
  return (
    <Modal onClose={onClose} ariaLabel={`选择已有${label}信号`} size="md">
      <div className="modal-header flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">选择已有{label}信号</h3>
          <p className="mt-1 text-[11px] text-muted">来自信号指标，可多选；应用后点击“保存设置”生效。</p>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭信号选择" className="modal-close"><X className="h-4 w-4" /></button>
      </div>
      <div className="modal-body space-y-3 overflow-y-auto px-5 py-4">
        <label className="flex items-center gap-2 rounded-btn border border-border bg-base px-3">
          <Search className="h-3.5 w-3.5 shrink-0 text-muted" />
          <input aria-label="搜索已有信号" value={search} onChange={event => setSearch(event.target.value)}
            placeholder="搜索信号名称或标识" className="h-9 min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none" />
        </label>
        {query.isPending && <p className="flex items-center gap-2 text-xs text-muted"><Loader2 className="h-3 w-3 animate-spin" />加载信号…</p>}
        {query.isError && <div role="alert" className="text-xs text-danger">
          信号加载失败。<button type="button" onClick={() => query.refetch()} className="ml-2 underline">重试</button>
        </div>}
        {list.map(signal => {
          const id = customSignalColumn(signal)
          const selected = draft.includes(id)
          const reason = !signal.enabled ? '信号已停用，请先在信号指标中启用'
            : signal.timeframe === 'intraday' && !allowIntraday ? '仅支持通用分钟策略，当前策略不支持' : ''
          const summary = signal.template === 'late_session_strength' ? lateSessionSummary(signal).slice(0, 2)
            : signal.template === 'morning_liquidation' ? morningLiquidationSummary(signal).slice(0, 2)
              : [`${signal.conditions.length} 个条件同时满足`]
          return <label key={id} className={`flex items-start gap-3 rounded-btn border px-3 py-3 ${selected
            ? 'border-accent/40 bg-accent/5' : 'border-border bg-base'} ${reason && !selected ? 'opacity-60' : 'cursor-pointer'}`}>
            <input type="checkbox" aria-label={signal.name} checked={selected} disabled={!!reason && !selected}
              onChange={() => toggle(id)} className="mt-0.5 shrink-0 accent-accent" />
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-foreground">{signal.name}</span>
                <span className="text-[10px] text-muted">{signal.timeframe === 'intraday' ? '盘中' : '日线'} · {signal.kind === 'both' ? '出入通用' : label}</span>
              </div>
              <p className="break-all font-mono text-[10px] text-muted">{signal.id}</p>
              {summary.map(text => <p key={text} className="text-[11px] leading-5 text-secondary">{text}</p>)}
              {reason && <p className="text-[11px] leading-5 text-warning">{reason}{selected ? '；可取消选择' : ''}</p>}
            </div>
          </label>
        })}
        {!query.isPending && !query.isError && list.length === 0 && <p className="py-5 text-center text-xs text-muted">
          {keyword ? '没有匹配的信号' : `暂无已有${label}信号，可先在信号指标中创建`}
        </p>}
      </div>
      <div className="modal-footer flex items-center justify-between gap-3">
        <span className="text-[11px] text-muted">已选 {draft.filter(id => id.startsWith('csg_') || id.startsWith('csgi_')).length} 个自定义信号</span>
        <div className="flex items-center gap-2">
          <button type="button" onClick={onClose} className="btn-secondary h-8 px-3 text-xs">取消</button>
          <button type="button" disabled={query.isPending || query.isError}
            onClick={() => { onChange(draft); onClose() }} className="btn-primary flex h-8 items-center gap-1.5 px-3 text-xs disabled:opacity-50">
            <Check className="h-3.5 w-3.5" />应用选择
          </button>
        </div>
      </div>
    </Modal>
  )
}
