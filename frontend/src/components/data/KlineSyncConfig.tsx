import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type KlinePeriod, type KlineSyncConfig as Config } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { isLocalDerivedPeriod } from '@/lib/klinePeriods'
import { TimePicker } from '@/components/TimePicker'
import { DatePicker } from '@/components/DatePicker'

const inputClass = 'rounded-btn border border-border bg-base px-2 py-1.5 text-xs text-foreground w-full'
const buttonClass = 'rounded-btn border border-accent/30 bg-accent/10 text-accent px-3 py-2 text-xs disabled:opacity-40'

function pad(value: number) {
  return String(value).padStart(2, '0')
}

function dateString(value: Date) {
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
}

function daysAgo(days: number) {
  const value = new Date()
  value.setDate(value.getDate() - days)
  return dateString(value)
}

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
  const [startDate, setStartDate] = useState(() => daysAgo(30))
  const [endDate, setEndDate] = useState(() => dateString(new Date()))
  const generate = isLocalDerivedPeriod(period)
  const today = dateString(new Date())
  const rangeError = !startDate || !endDate
    ? '请选择起始日期和结束日期'
    : endDate < startDate
      ? '结束日期不能早于起始日期'
      : endDate > today
        ? '结束日期不能晚于今天'
        : null
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
    mutationFn: (mode?: 'full' | 'missing') => {
      if (generate) return api.syncKlinePeriod(period)
      if (!mode) throw new Error('请选择数据获取方式')
      return api.syncKlinePeriod(period, { startDate, endDate, mode })
    },
    onSuccess: ({ job_id }) => {
      void qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      onJobStart(job_id)
    },
  })
  if (query.isError) return <p role="alert" className="text-xs text-danger">{query.error.message}</p>
  if (!draft) return <p role="status" className="text-xs text-secondary">正在加载同步设置…</p>
  const intraday = period.endsWith('m')
  const local = isLocalDerivedPeriod(period)
  const localBase = period === '1w' || period === '1d' ? '本地日K' : '本地1分K'
  const canSync = generate || (hasCap && !rangeError)
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
        <TimePicker aria-label={period === '1w' ? '周六同步时间' : '交易日同步时间'}
          value={draft.time} onChange={time => setDraft({ ...draft, time })}
          className="w-full" buttonClassName="w-full justify-start" align="left" />
      </label>}
      <button className={buttonClass} disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存周期设置'}</button>
      {save.isSuccess && <p role="status" className="text-xs text-accent">周期设置已保存</p>}
      {save.isError && <p role="alert" className="text-xs text-danger">{save.error.message}</p>}
    </section>
    <section className="rounded-card border border-border p-4 space-y-3">
      <h3 className="text-sm font-medium">离线补齐</h3>
        <p className="text-xs text-muted">{generate
          ? `由${localBase}计算后入库，覆盖本地基础数据已有日期，不请求上游。`
          : '按所选起始日期和结束日期获取闭区间数据。全量模式覆盖同一记录，遗漏模式先检查本地缺口再请求上游。'}</p>
        {period === '1d' && <p className="text-xs text-muted">获取日K后继续同步除权因子并重算指标。</p>}
        {generate && <p className="text-xs text-muted">缺少可计算的{localBase}时，任务会提示先补齐基础数据。</p>}
        {!generate && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-xs text-secondary">
            <span>起始日期</span>
            <DatePicker
              aria-label="起始日期"
              value={startDate}
              onChange={value => { setStartDate(value); sync.reset() }}
              max={endDate || today}
              align="left"
              clearable
              disabled={isRunning || sync.isPending}
              className="w-full"
              buttonClassName="w-full justify-start"
            />
          </label>
          <label className="space-y-1 text-xs text-secondary">
            <span>结束日期</span>
            <DatePicker
              aria-label="结束日期"
              value={endDate}
              onChange={value => { setEndDate(value); sync.reset() }}
              min={startDate || undefined}
              max={today}
              clearable
              disabled={isRunning || sync.isPending}
              aria-invalid={!!rangeError}
              aria-describedby={rangeError ? `kline-${period}-range-error` : undefined}
              className="w-full"
              buttonClassName="w-full justify-start"
            />
          </label>
        </div>}
        {!generate && rangeError && <p id={`kline-${period}-range-error`} role="alert" className="text-xs text-danger">{rangeError}</p>}
        <p className="text-xs text-muted">当前最早：{earliestDate ?? '暂无数据'}</p>
        {generate
          ? <button className={`${buttonClass} w-full`} onClick={() => sync.mutate(undefined)} disabled={!canSync || isRunning || sync.isPending}>
              {sync.isPending ? '请求中…' : '生成数据'}
            </button>
          : <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {(['full', 'missing'] as const).map(mode => <button
                key={mode}
                className={`${buttonClass} w-full`}
                onClick={() => sync.mutate(mode)}
                disabled={!canSync || isRunning || sync.isPending}
              >
                {sync.isPending && sync.variables === mode
                  ? '请求中…'
                  : mode === 'full' ? '获取全量数据' : '获取遗漏数据'}
              </button>)}
            </div>}
        {!generate && !hasCap && <p role="alert" className="text-xs text-muted">当前数据源缺少K线能力，请检查数据源设置。</p>}
        {sync.isError && <p role="alert" className="text-xs text-danger">{sync.error.message}</p>}
    </section>
  </div>
}
