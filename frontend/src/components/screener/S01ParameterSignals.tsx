import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type CustomSignal, type StrategyDetail } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { s01SignalSummary } from '@/lib/customSignals'
import { CustomSignalDialog } from '@/components/signals/CustomSignalDialog'

export function S01ParameterSignals({ detail, onSaved }: {
  detail: StrategyDetail
  onSaved?: (signal: CustomSignal) => void
}) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<CustomSignal | null>(null)
  const list = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  if (!detail.parameter_signals) return null
  return <div className="space-y-3">
    {(['entry', 'exit'] as const).map(kind => {
      const bound = detail.parameter_signals?.[kind]
      if (!bound) return null
      const signal = list.data?.signals.find(value => value.id === bound.id) ?? bound
      return <div key={kind} className="rounded-xl border border-border bg-surface/20 p-3">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-medium">{kind === 'entry' ? '入场触发器' : '出场触发器'} · {signal.name}</span>
          <button type="button" disabled={!!signal.error} onClick={() => setEditing(signal)}
            className="shrink-0 rounded-btn border border-border px-2 py-1 text-xs text-accent disabled:opacity-50">编辑信号</button>
        </div>
        {s01SignalSummary(signal).map(text => <p key={text} className="mt-1 text-[11px] leading-5 text-secondary">{text}</p>)}
        {!signal.enabled && <p role="alert" className="text-xs text-danger">信号已停用，策略不能执行。</p>}
      </div>
    })}
    <p className="text-[10px] leading-5 text-muted">以上参数保存到当前信号，s01选股、持仓判断与新回测统一读取。修改即时生效；历史冻结任务保留原定义。</p>
    <CustomSignalDialog open={!!editing} signal={editing} onClose={() => setEditing(null)}
      onSaved={signal => {
        qc.invalidateQueries({ queryKey: QK.strategyDetail(detail.id) })
        onSaved?.(signal)
      }} />
  </div>
}
