import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type KlinePeriod, type KlineSyncConfig as Config } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { isLocalDerivedPeriod } from '@/lib/klinePeriods'

const inputClass = 'rounded-btn border border-border bg-base px-2 py-1.5 text-xs text-foreground w-full'
const buttonClass = 'rounded-btn border border-accent/30 bg-accent/10 text-accent px-3 py-2 text-xs disabled:opacity-40'

export function KlineSyncConfig({ period, hasCap, isRunning, earliestDate, onJobStart }: {
  period: KlinePeriod
  hasCap: boolean
  isRunning: boolean
  earliestDate: string | null
  onJobStart: (id: string) => void
}) {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: QK.klineSyncConfigs, queryFn: api.klineSyncConfigs })
  const [draft, setDraft] = useState<Config | null>(null)
  const [value, setValue] = useState(6)
  const [unit, setUnit] = useState<'month' | 'year'>('month')
  useEffect(() => { if (query.data) setDraft(query.data[period]) }, [query.data, period])
  const save = useMutation({
    mutationFn: () => api.saveKlineSyncConfig(period, draft!),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.klineSyncConfigs })
      void qc.invalidateQueries({ queryKey: QK.preferences })
      void qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
  })
  const sync = useMutation({
    mutationFn: () => api.syncKlinePeriod(period, value, unit),
    onSuccess: ({ job_id }) => {
      void qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      onJobStart(job_id)
    },
  })
  if (query.isError) return <p role="alert" className="text-xs text-danger">{query.error.message}</p>
  if (!draft) return <p role="status" className="text-xs text-secondary">正在加载同步设置…</p>
  const intraday = period.endsWith('m')
  const local = isLocalDerivedPeriod(period)
  const localBase = period === '1w' ? '本地日K' : '本地1分K'
  const canSync = local || hasCap
  return <div className="space-y-5">
    <section className="rounded-card border border-border p-4 space-y-3">
      <h3 className="text-sm font-medium">周期同步</h3>
      <p className="text-xs text-muted">{period === '1w' ? '每周六按北京时间运行，自动同步总开关开启后生效。' : '按北京时间交易日运行，自动同步总开关开启后生效。'}</p>
      {local && <p className="text-xs text-muted">由{localBase}计算并入库，不向上游获取该周期数据。</p>}
      <label className="flex items-center gap-2 text-xs">
        <input type="checkbox" role="switch" aria-label="开启周期同步" checked={draft.enabled}
          onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />开启周期同步
      </label>
      {intraday ? <>
        <p className="text-xs text-secondary">统一采集窗口：09:15–11:45 / 13:00–16:00</p>
        <label className="block text-xs text-secondary space-y-1"><span>周期间隔（秒）</span>
          <input type="number" min={1} max={86400} className={inputClass} value={draft.interval_seconds}
            onChange={e => setDraft({ ...draft, interval_seconds: Number(e.target.value) })} />
        </label>
      </> : <label className="block text-xs text-secondary space-y-1"><span>{period === '1w' ? '周六同步时间' : '交易日同步时间'}</span>
        <input type="time" className={inputClass} value={draft.time} onChange={e => setDraft({ ...draft, time: e.target.value })} />
      </label>}
      <button className={buttonClass} disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存周期设置'}</button>
      {save.isSuccess && <p role="status" className="text-xs text-accent">周期设置已保存</p>}
      {save.isError && <p role="alert" className="text-xs text-danger">{save.error.message}</p>}
    </section>
    <section className="rounded-card border border-border p-4 space-y-3">
      <h3 className="text-sm font-medium">离线补齐</h3>
        <p className="text-xs text-muted">{local ? `由${localBase}计算后入库。请先补齐所需范围的${localBase}；缺失日期不会生成数据。` : '点击获取数据后，从本地最早日期向前扩展历史；空库从今天开始计算范围。'}</p>
        {period === '1d' && <p className="text-xs text-muted">获取日K后继续同步除权因子并重算指标。</p>}
        <div className="flex items-center gap-2 text-xs">
          <span className="shrink-0">向前扩展</span>
          <input aria-label="历史扩展数量" type="number" min={1} max={unit === 'year' ? 10 : 120}
            className={inputClass} value={value} onChange={e => setValue(Number(e.target.value))} />
          <select aria-label="历史扩展单位" className={inputClass} value={unit}
            onChange={e => { setUnit(e.target.value as 'month' | 'year'); setValue(1) }}><option value="month">月</option><option value="year">年</option></select>
        </div>
        <p className="text-xs text-muted">当前最早：{earliestDate ?? '暂无数据'}</p>
        <button className={`${buttonClass} w-full`} onClick={() => sync.mutate()} disabled={!canSync || isRunning || sync.isPending || !Number.isInteger(value) || value < 1}>
          {sync.isPending ? '请求中…' : '获取数据'}
        </button>
        {!canSync && <p role="alert" className="text-xs text-muted">当前数据源缺少K线能力，请检查数据源设置。</p>}
        {sync.isError && <p role="alert" className="text-xs text-danger">{sync.error.message}</p>}
    </section>
  </div>
}
