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

// 修正入口只获取基础K线; 日K和1分K能力由路由矩阵判定。
export function RepairDailyPanel({ hasCap, hasMinuteCap, isRunning, latestDates, onStart }: {
  hasCap: boolean
  hasMinuteCap: boolean
  isRunning: boolean
  latestDates: Partial<Record<KlinePeriod, string | null>>
  onStart: (jobId: string) => void
}) {
  const qc = useQueryClient()
  const [period, setPeriod] = useState<'1m' | '1d'>('1d')
  const daily = period === '1d'
  const hasBatchCap = daily ? hasCap : hasMinuteCap
  const latestDate = latestDates[period] ?? null

  // 默认起始日期: 最新数据往前推 30 天 (兼顾补缺口 + 复核近期数据, 成本不高)
  const [startDate, setStartDate] = useState(daysAgo(30))
  const [endDate, setEndDate] = useState(todayStr)

  const repair = useMutation({
    mutationFn: (mode: 'full' | 'missing') => api.repairDaily(startDate, period, mode, endDate),
    onSuccess: ({ job_id }) => {
      onStart(job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  const today = todayStr()
  const rangeError = !startDate || !endDate
    ? '请填写起始日期和结束日期'
    : endDate < startDate ? '结束日期不能早于起始日期'
    : endDate > today ? '结束日期不能晚于今天' : null
  const canSubmit = hasBatchCap && !isRunning && !repair.isPending && !rangeError
  const gapDays = latestDate
    ? Math.max(0, Math.round((Date.parse(today) - Date.parse(latestDate)) / 86400000))
    : null

  return (
    <div className="space-y-3">
      <div className="rounded-card bg-accent/8 border border-accent/20 p-3 space-y-1.5">
        <div className="text-xs text-foreground">
          当数据出现缺口时，从这里修正起始日期至结束日期的{KLINE_PERIOD_LABELS[period]}数据。
        </div>
        <div className="text-[10px] text-muted leading-relaxed">
          通过当前K线数据源接口获取{KLINE_PERIOD_LABELS[period]}并入库。
          {' '}获取全量数据会重新获取选定区间并覆盖旧值；获取遗漏数据会先检查缺口，仅补缺少的证券和时间，保留已有数据。
        </div>
      </div>

      <details open className="border-y border-border/60 py-2 text-[10px] leading-relaxed">
        <summary className="cursor-pointer font-medium text-secondary">修正说明</summary>
        <dl className="mt-2 space-y-1.5 text-muted">
          <div>
            <dt className="inline text-secondary">范围：</dt>
            <dd className="inline">从起始日期检查到结束日期，包含结束当天；按所选K线周期分别执行。</dd>
          </div>
          <div>
            <dt className="inline text-secondary">获取全量数据：</dt>
            <dd className="inline">重新获取选定区间，并覆盖同一记录的旧值。</dd>
          </div>
          <div>
            <dt className="inline text-secondary">获取遗漏数据：</dt>
            <dd className="inline">只写入缺失的证券和时间记录，已有数据保持不变。</dd>
          </div>
          <div>
            <dt className="inline text-secondary">数据来源：</dt>
            <dd className="inline">1分K、日K通过当前数据源接口获取。</dd>
          </div>
          <div>
            <dt className="inline text-secondary">后续处理：</dt>
            <dd className="inline">日K修正后会重算受影响证券的复权与技术指标。</dd>
          </div>
        </dl>
      </details>

      <div className="flex items-center justify-between gap-2">
        <label htmlFor="repair-kline-period" className="text-xs text-secondary">K线周期</label>
        <select
          id="repair-kline-period"
          aria-label="K线周期"
          value={period}
          onChange={e => { setPeriod(e.target.value as '1m' | '1d'); repair.reset() }}
          disabled={isRunning || repair.isPending}
          className="rounded-btn border border-border bg-elevated px-2 py-1 text-xs text-foreground"
        >
          {(['1m', '1d'] as const).map(value => (
            <option key={value} value={value}>{KLINE_PERIOD_LABELS[value]}</option>
          ))}
        </select>
      </div>

      {/* 起始日期 — 用户主要操作,放上面 */}
      <div className="flex items-center justify-between gap-2">
        <label htmlFor="repair-start-date" className="text-xs text-secondary">起始日期</label>
        <DatePicker
          id="repair-start-date"
          aria-label="起始日期"
          value={startDate}
          onChange={value => { setStartDate(value); repair.reset() }}
          max={today}
          align="right"
          clearable
          disabled={isRunning || repair.isPending}
        />
      </div>

      <div className="flex items-center justify-between gap-2">
        <label htmlFor="repair-end-date" className="text-xs text-secondary">结束日期</label>
        <DatePicker
          id="repair-end-date"
          aria-label="结束日期"
          value={endDate}
          onChange={value => { setEndDate(value); repair.reset() }}
          min={startDate}
          max={today}
          clearable
          disabled={isRunning || repair.isPending}
          aria-invalid={!!rangeError}
          aria-describedby={rangeError ? 'repair-range-error' : undefined}
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
        {' → '}<span className="font-mono text-secondary">{endDate}</span>{endDate === today ? '(今天)' : ''} 的
        {' '}{KLINE_PERIOD_LABELS[period]}
      </div>

      {rangeError && (
        <div id="repair-range-error" role="alert" className="text-xs text-danger">{rangeError}</div>
      )}

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
          <MissingCapChip capKey={daily ? 'kline.daily.batch' : 'kline.minute.batch'} />
        </span>
      )}
      {repair.isError && (
        <div role="alert" className="text-xs text-danger">{repair.error.message}</div>
      )}
    </div>
  )
}
