import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api, type KlinePeriod } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { KLINE_PERIOD_LABELS } from '@/lib/klinePeriods'
import { MissingCapChip } from '@/lib/capability-labels'
import { DatePicker } from '@/components/DatePicker'

function pad(n: number) { return String(n).padStart(2, '0') }
function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
/** 往前推 N 天 */
function daysAgo(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// 日K和分钟K能力均由路由矩阵判定; 10分K仅依赖本地数据。
export function RepairDailyPanel({ hasCap, hasMinuteCap, isRunning, latestDates, onStart }: {
  hasCap: boolean
  hasMinuteCap: boolean
  isRunning: boolean
  latestDates: Partial<Record<KlinePeriod, string | null>>
  onStart: (jobId: string) => void
}) {
  const qc = useQueryClient()
  const [period, setPeriod] = useState<KlinePeriod>('1d')
  const daily = period === '1d'
  const local = period === '10m'
  const hasBatchCap = local || (daily || period === '1w' ? hasCap : hasMinuteCap)
  const latestDate = latestDates[period] ?? null

  // 默认起始日期: 最新数据往前推 30 天 (兼顾补缺口 + 复核近期数据, 成本不高)
  const [startDate, setStartDate] = useState(daysAgo(30))

  const repair = useMutation({
    mutationFn: (mode: 'full' | 'missing') => api.repairDaily(startDate, period, mode),
    onSuccess: ({ job_id }) => {
      onStart(job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  const today = todayStr()
  const canSubmit = hasBatchCap && !isRunning && !repair.isPending && startDate <= today
  const gapDays = latestDate
    ? Math.max(0, Math.round((Date.parse(today) - Date.parse(latestDate)) / 86400000))
    : null

  return (
    <div className="space-y-3">
      <div className="rounded-card bg-accent/8 border border-accent/20 p-3 space-y-1.5">
        <div className="text-xs text-foreground">
          当数据出现缺口时，从这里修正选定区间到今天的{KLINE_PERIOD_LABELS[period]}数据。
        </div>
        <div className="text-[10px] text-muted leading-relaxed">
          {local
            ? '由本地1分K自动计算10分K并入库；基础1分K不完整时，请先获取遗漏的1分K。'
            : `通过当前K线数据源接口获取${KLINE_PERIOD_LABELS[period]}并入库。`}
          {' '}获取全量数据会重新获取选定区间并覆盖旧值；获取遗漏数据会先检查缺口，仅补缺少的证券和时间，保留已有数据。
        </div>
      </div>

      <div className="flex items-center justify-between gap-2">
        <label htmlFor="repair-kline-period" className="text-xs text-secondary">K线周期</label>
        <select
          id="repair-kline-period"
          aria-label="K线周期"
          value={period}
          onChange={e => { setPeriod(e.target.value as KlinePeriod); repair.reset() }}
          disabled={isRunning || repair.isPending}
          className="rounded-btn border border-border bg-elevated px-2 py-1 text-xs text-foreground"
        >
          {(Object.entries(KLINE_PERIOD_LABELS) as [KlinePeriod, string][]).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </div>

      {/* 起始日期 — 用户主要操作,放上面 */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-secondary">起始日期</span>
        <DatePicker
          value={startDate}
          onChange={setStartDate}
          max={today}
          align="right"
          buttonClassName="font-mono text-xs"
        />
      </div>

      {/* 本地最新数据 — 参考信息,放下面 */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted">本地最新数据</span>
        <span className="font-mono text-xs text-secondary">
          {latestDate ?? '—'}
          {gapDays !== null && gapDays > 0 && (
            <span className="ml-2 text-warning/90">已落后 {gapDays} 天</span>
          )}
        </span>
      </div>

      <div className="text-[10px] text-muted -mt-1">
        检查区间 <span className="font-mono text-secondary">{startDate}</span>
        {' → '}<span className="font-mono text-secondary">{today}</span>(今天) 的
        {' '}{KLINE_PERIOD_LABELS[period]}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {(['full', 'missing'] as const).map(mode => (
          <button
            key={mode}
            onClick={() => repair.mutate(mode)}
            disabled={!canSubmit}
            className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
          >
            {repair.isPending && repair.variables === mode
              ? <><Loader2 className="h-3 w-3 animate-spin" />请求中…</>
              : mode === 'full' ? '获取全量数据' : '获取遗漏数据'}
          </button>
        ))}
      </div>

      {!hasBatchCap && (
        <span className="block text-center">
          <MissingCapChip capKey={daily || period === '1w' ? 'kline.daily.batch' : 'kline.minute.batch'} />
        </span>
      )}
      {repair.isError && (
        <div role="alert" className="text-xs text-danger">{repair.error.message}</div>
      )}
    </div>
  )
}
