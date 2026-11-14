import type { ScreenerResult, StrategyEntryProgressStep } from '@/lib/api'

const labels = { breadth: '上涨占比', change: '涨跌幅', ma1: '1分钟MA5', ma5: '5分钟MA5' } as const
const states = { waiting: '等待', success: '完成', rejected: '未达下限', skipped: '跳过', failed: '失败' }
const count = (value: number | null | undefined) => value == null ? '—' : String(value)
const percent = (value: number | null | undefined, digits?: number) =>
  value == null || !Number.isFinite(value) ? '—' : digits == null ? String(value) : value.toFixed(digits)
// These timestamps are Beijing wall-clock values; avoid browser timezone conversion.
const at = (value?: string | null) => value?.slice(11, 19) ?? '—'
const timing = (step: StrategyEntryProgressStep) =>
  `计划 ${at(step.scheduled_at)} · ${step.first_receipt
    ? `首次计算 ${at(step.first_receipt.started_at)}～${at(step.first_receipt.finished_at)}`
    : step.started_at ? '首次计算未保存' : '等待首次计算'} · 最近结果 ${at(step.started_at)}～${at(step.finished_at)}`

export function StrategyEntryProgress({ result, today }: { result: ScreenerResult; today: string }) {
  const receipts = Object.entries(result.entry_data_quality ?? {})
    .filter(([, quality]) => quality.progress?.schema === 'late_session_progress_v1'
      && quality.progress.as_of === result.as_of)
  const historical = result.as_of < today
  const error = result.section_errors?.buy ?? result.execution_status?.reason
  if (!receipts.length) return <span className="strategy-result-note">
    {result.as_of} · {historical ? '历史保存结果 · 未保存阶段进展'
      : result.as_of > today ? '已保存预选 · 等待该交易日执行' : '未执行 / 等待状态回执'}
    {error && ` · ${error}`}
  </span>
  return <div aria-label="核心任务进展" className="strategy-result-note flex flex-1 basis-[560px] flex-wrap items-center gap-x-3 gap-y-1">
    {receipts.map(([signal, quality]) => {
      const progress = quality.progress!
      return <div key={signal} className="flex min-w-0 flex-1 basis-full flex-wrap items-center gap-x-3 gap-y-1">
        {(Object.keys(labels) as (keyof typeof labels)[]).map(key => {
          const step = progress.steps[key]
          if (!step) return null
          const p = progress.params
          const known = step.processed != null
          const detail = !known ? step.reason ?? '等待计算回执'
            : key === 'breadth' ? `上涨 ${count(step.passed)} / 有效 ${count(step.usable)} / 完整池 ${count(step.total)}`
            : key === 'change' ? `完整池符合 ${count(step.pool_passed)} · ${progress.cohort === 'frozen_selection' ? '预选' : '池内'} ${count(step.passed)}/${count(step.total)}`
            : `检查 ${count(step.processed)}/${count(step.total)} · 通过 ${count(step.passed)} · 缺数据 ${count(step.missing)}`
          const title = key === 'breadth' && known
            ? `上涨占比 ${percent(step.up_pct, 2)}% / 下限 ${percent(p.pool_up_min_pct)}%`
            : key === 'change' ? `涨跌幅 ${percent(p.change_min_pct)}～${percent(p.change_max_pct)}%` : labels[key]
          return <details key={key} data-progress-step={key} className="min-w-0 max-w-full">
            <summary title={`${timing(step)} · ${detail}`}
              className={`strategy-result-disclosure ${step.state === 'failed' ? 'text-danger' : step.state === 'rejected' ? 'text-warning' : ''}`}>
              {title} · {states[step.state] ?? '未知'}{known && ` · ${key === 'breadth' ? `${count(step.passed)}/${count(step.usable)}` : key === 'change' ? `${progress.cohort === 'frozen_selection' ? '预选' : '池内'} ${count(step.passed)}/${count(step.total)}` : detail}`}
            </summary>
            <div className="max-w-full space-y-1 py-1">
              <p>{detail}</p>
              <p>{timing(step)}{step.elapsed_ms != null && ` · ${step.elapsed_ms.toFixed(1)}ms`}</p>
              {step.first_receipt && <p>
                {key === 'breadth'
                  ? `首次上涨 ${count(step.first_receipt.passed)} / 有效 ${count(step.first_receipt.usable)} / 完整池 ${count(step.first_receipt.total)}`
                  : `首次检查 ${count(step.first_receipt.processed)}/${count(step.first_receipt.total)} · 通过 ${count(step.first_receipt.passed)}`}
                {step.first_receipt.elapsed_ms != null && ` · 首次耗时 ${step.first_receipt.elapsed_ms.toFixed(1)}ms`}
              </p>}
              {step.reason && known && <p>{step.reason}</p>}
              {key === 'ma1' || key === 'ma5' ? <p>连续 {key === 'ma1' ? p.ma5_1m_count : p.ma5_5m_count} 个MA5值严格上升</p> : null}
            </div>
          </details>
        })}
      </div>
    })}
    <span className="strategy-result-note">{result.as_of} · {historical ? '历史保存回执'
      : result.showing_last_success ? '上次成功回执' : '当前页每秒刷新'}{error && ` · ${error}`}</span>
  </div>
}
