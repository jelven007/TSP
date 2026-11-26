import type { ScreenerResult, StrategyEntryProgressStep } from '@/lib/api'

const labels = { breadth: '上涨占比', change: '涨跌幅', ma1: '1分钟MA5', ma5: '5分钟MA5' } as const
const states = { waiting: '等待', success: '完成', rejected: '未达下限', skipped: '跳过', failed: '失败' }
const count = (value: number | null | undefined) => value == null ? '—' : String(value)
const percent = (value: number | null | undefined, digits?: number) =>
  value == null || !Number.isFinite(value) ? '—' : digits == null ? String(value) : value.toFixed(digits)
// 回执为北京时间墙钟；只截取保存时钟，不由浏览器时区推算完成时间。
const at = (value?: string | null) => value?.slice(11, 19) ?? '—'
const timing = (step: StrategyEntryProgressStep) =>
  `计划 ${at(step.scheduled_at)} · ${step.first_receipt
    ? `首次计算 ${at(step.first_receipt.started_at)}～${at(step.first_receipt.finished_at)}`
    : step.started_at ? '首次计算未保存' : '等待首次计算'} · 最近结果 ${at(step.started_at)}～${at(step.finished_at)}`

const issueLabels: Record<string, string> = {
  missed_trigger: '到点未触发', delayed_input: '输入延迟', delayed_compute: '计算延迟',
  stuck_compute: '计算超过预算', invalid_input: '输入证据异常', expired_unfinished: '截止前未完成',
  worker_unavailable: 'Worker不可用',
}

/** 分开显示同日实时证据、独立运行异常和诊断复核，摘要一致不代表按时完成。 */
function RunEvidence({ result }: { result: ScreenerResult }) {
  const evidence = result.run_evidence?.as_of === result.as_of ? result.run_evidence : null
  const issues = result.operational_state?.as_of === result.as_of ? result.operational_state.issues : []
  const review = result.repair_provenance?.kind === 'post_close_signal_review_v1'
    || result.repair_provenance?.diagnostic_only
    || result.diagnostic_review?.diagnostic_only
  if (!evidence && !issues.length && !review) return null
  return <div aria-label="输入与结果来源" className="strategy-result-note flex basis-full flex-wrap gap-x-3 gap-y-1">
    {review && <span>诊断复核结果 · 不代表原盘中按时完成</span>}
    {review && result.original_realtime && <span>
      原盘中状态 {result.original_realtime.buy_status ?? '未保存'}
      {result.original_realtime.buy_error && ` · ${result.original_realtime.buy_error}`}
    </span>}
    {Object.entries(evidence?.phases ?? {}).map(([phase, value]) => value && <details key={phase}>
      <summary className="strategy-result-disclosure">
        {phase === 'stocks' ? '个股' : '广度'} · {value.provenance === 'diagnostic_only' ? '诊断复核输入' : '实时冻结输入'}
        {' · '}{value.state === 'complete' ? '已完成' : '未完成'}
      </summary>
      <p>输入 {value.input_sha256.slice(0, 12)} · 结果 {value.result_sha256?.slice(0, 12) ?? '尚未完成'}</p>
      <p>输入可用 {at(value.timing.publication_available_at)} · 派发 {at(value.timing.dispatched_at)}
        {' · '}返回 {at(value.timing.received_at)} · 事务记录 {at(result.run_commit?.phases[phase as 'stocks' | 'breadth']?.commit_recorded_at)}</p>
      {value.timing.compute?.queue_ms != null && <p>
        排队 {value.timing.compute.queue_ms.toFixed(1)}ms · 计算与回收 {value.timing.compute.sandbox_ms?.toFixed(1) ?? '—'}ms
      </p>}
    </details>)}
    {issues.map(issue => <span key={`${issue.phase}:${issue.kind}`} className="text-warning">
      {issue.phase === 'stocks' ? '个股' : '广度'} · {issueLabels[issue.kind] ?? '运行异常'}
      {issue.reason && ` · ${issue.reason}`}
    </span>)}
  </div>
}

/** 四项始终占位；只有同日真实回执填状态，缺输入与成功零命中分别展示。 */
export function StrategyEntryProgress({ result, today }: { result: ScreenerResult; today: string }) {
  const receipts = Object.entries(result.entry_data_quality ?? {})
    .filter(([, quality]) => quality.progress?.schema === 'late_session_progress_v1'
      && quality.progress.as_of === result.as_of)
  const historical = result.as_of < today
  const error = result.section_errors?.buy ?? result.execution_status?.reason
  const buyTask = result.monitor_tasks?.buy
  const signals = buyTask?.signals as Record<string, {
    stocks?: { input_state?: string; state?: string }
  }> | undefined
  const waitingInput = Object.values(signals ?? {}).some(signal =>
    signal?.stocks?.state === 'waiting_input' || signal?.stocks?.input_state === 'waiting_input')
  const missingSelection = result.section_statuses?.select === 'unavailable'
  const emptyNote = <span className="strategy-result-note">
    {result.as_of} · {historical ? (missingSelection ? '历史归档待查询' : '历史保存结果 · 未保存阶段进展')
      : result.as_of > today ? (missingSelection ? '盘后预选尚未归档' : '已保存预选 · 等待该交易日执行')
      : buyTask?.published === true && !['success', 'rejected'].includes(String(buyTask.state))
        ? `部分入场确认完成 · ${result.sections?.buy.length ?? 0}只`
      : buyTask?.terminal === true && buyTask.state === 'rejected'
        ? '入场任务结束 · 条件未满足'
        : result.section_statuses?.buy === 'rejected' ? '入场任务结束 · 条件未满足'
        : result.section_statuses?.buy === 'success' && !result.selection_only
        ? `买入确认完成 · ${result.sections?.buy.length ?? 0}只`
        : result.section_statuses?.buy === 'expired' ? '今日买入未确认'
        : waitingInput ? '等待输入同步' : '未执行 / 等待状态回执'}
    {error && ` · ${error}`}
  </span>
  if (!receipts.length) return <div aria-label="核心任务进展" className="strategy-result-note flex flex-1 basis-[560px] flex-wrap items-center gap-x-3 gap-y-1">
    {(Object.keys(labels) as (keyof typeof labels)[]).map(key => <details key={key} data-progress-step={key}>
      <summary className="strategy-result-disclosure">{labels[key]} · 未知</summary>
      <p className="py-1">{historical ? '未保存阶段进展' : '等待计算回执'}</p>
    </details>)}
    {emptyNote}
    <RunEvidence result={result} />
  </div>
  return <div aria-label="核心任务进展" className="strategy-result-note flex flex-1 basis-[560px] flex-wrap items-center gap-x-3 gap-y-1">
    {receipts.map(([signal, quality]) => {
      const progress = quality.progress!
      return <div key={signal} className="flex min-w-0 flex-1 basis-full flex-wrap items-center gap-x-3 gap-y-1">
        {(Object.keys(labels) as (keyof typeof labels)[]).map(key => {
          const step: StrategyEntryProgressStep = progress.steps[key] ?? {
            state: 'waiting', reason: '等待计算回执',
          }
          const p = progress.params
          const known = step.processed != null
          const detail = !known ? step.reason ?? '等待计算回执'
            : key === 'breadth' ? `上涨 ${count(step.passed)} / 有效 ${count(step.usable)} / 完整池 ${count(step.total)}`
            : key === 'change' ? `完整池符合 ${count(step.pool_passed)} · ${progress.cohort === 'frozen_selection' ? '预选' : '池内'} ${count(step.passed)}/${count(step.total)}`
            : `检查 ${count(step.processed)}/${count(step.total)} · 通过 ${count(step.passed)} · 缺数据 ${count(step.missing)}`
          const title = key === 'breadth' && known
            ? `上涨占比 ${percent(step.up_pct, 2)}% / ${p.pool_up_min_pct == null
              ? '仅展示' : `下限 ${percent(p.pool_up_min_pct)}%`}`
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
      : result.showing_last_success ? '上次成功回执' : waitingInput ? '等待输入同步'
      : buyTask && buyTask.terminal !== true ? '阶段处理中' : '完成结果固定'}{error && ` · ${error}`}</span>
    <RunEvidence result={result} />
  </div>
}
