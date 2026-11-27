import type { StrategyMonitoring } from '@/lib/api'
import { S05_BRANCH_LABELS } from '@/lib/customSignals'

const labels: Record<string, string> = {
  disabled: '未开启', unconfigured: '未配置', waiting: '待执行',
  running: '执行中', success: '已计算', rejected: '已计算 · 未通过',
  failed: '计算失败', expired: '已结束', cancelled: '已取消',
  blocked: '等待兼容归档',
  non_trading_day: '非交易日', calendar_unknown: '交易日历未确认',
  waiting_input: '等待输入同步',
  no_receipt: '无定时回执',
}
const windowsText = (windows: { start: string; end: string }[]) =>
  windows.map(w => `${w.start}～${w.end}`).join(' / ')
const descriptions = { select: '保存至下个交易日选', buy: '同步至下个交易日卖', sell: '触发时提示卖出' }
const issues: Record<string, string> = {
  missed_trigger: '到点未触发', delayed_input: '输入延迟', delayed_compute: '计算延迟',
  stuck_compute: '计算超过预算', invalid_input: '输入证据异常',
  expired_unfinished: '截止前未完成', worker_unavailable: 'Worker不可用',
}

export function StrategyMonitorTasks({ value, pending = false, onToggle }: {
  value: StrategyMonitoring
  pending?: boolean
  onToggle?: () => void
}) {
  return (
    <div aria-label="策略定时任务" className="mt-3 border-t border-border pt-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="text-muted">{value.historical ? '历史定时任务'
          : value.historical === false ? '定时任务' : '当前监控任务'} · 北京时间 · {value.as_of}
          {value.is_trading_day === false ? ' · 非交易日' : value.is_trading_day === null ? ' · 交易日历未确认' : ''}
          {value.strategy_id === 'custom_mvxxcs05' && value.branch_code != null
            ? ` · 买入分支参数：${value.branch_code} · ${S05_BRANCH_LABELS[value.branch_code]}` : ''}
        </span>
        {onToggle && <button disabled={pending} onClick={onToggle}
          className="shrink-0 text-accent hover:underline disabled:opacity-50">
          {pending ? '保存中…' : value.enabled ? '取消监控' : '开启监控'}
        </button>}
      </div>
      <div className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2">
        {(['select', 'buy', 'sell'] as const).map(kind => {
          const task = value.tasks[kind]
          const noPlan = task.branch_code === 4 || (kind === 'buy' && value.branch_code === 4)
          const plannedTime = windowsText(task.windows)
          const receiptTime = windowsText(task.receipt_windows ?? [])
          return (
            <div key={kind} title={task.errors?.join('；')} className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <strong>{{ select: '选', buy: '买', sell: '卖' }[kind]}</strong>
                <span className="font-mono text-muted">
                  {plannedTime || (noPlan ? (kind === 'buy' ? '不安排买入' : '不安排卖出')
                    : task.plan_issue ? '计划时段未确认' : value.historical ? '无时段归档' : '未配置时段')}
                </span>
                <span className={task.state === 'failed' ? 'text-danger' : 'text-muted'}>
                  {task.receipt_source ? '定时回执：' : ''}
                  {noPlan && !task.receipt_source && !value.historical ? ''
                    : labels[task.state] ?? (value.historical == null ? '待执行' : '状态未确认')}
                </span>
              </div>
              <p className="mt-0.5 text-[10px] text-muted">
                {kind === 'select' ? `${value.strategy_id === 'custom_mvxxcs03' ? 'S03盘后“选”任务'
                  : value.strategy_id === 'custom_mvxxcs05' ? 'S05盘后“选”任务' : '自定义策略盘后“选”任务'} · ` : ''}
                {task.branch_date
                  ? `${kind === 'sell' ? '前一交易日' : '所选交易日'} ${task.branch_date} · ${task.branch_code != null
                      ? `分支参数 ${task.branch_code} · ${S05_BRANCH_LABELS[task.branch_code]}` : '分支参数未确认'}`
                  : kind === 'sell' && value.strategy_id === 'custom_mvxxcs05'
                    ? '按前一交易日买入分支匹配出场信号' : descriptions[kind]}
              </p>
              {task.plan_issue && <p className="mt-0.5 text-[10px] text-warning">{task.plan_issue}</p>}
              {receiptTime && receiptTime !== plannedTime && <details className="mt-0.5 text-[10px] text-muted">
                <summary className="cursor-pointer">查看原定时回执时段</summary>
                <p>{receiptTime}</p>
              </details>}
              {task.archived_result && <p className="mt-0.5 text-[10px] text-muted"
                title={task.archived_result.errors?.join('；')}>
                {task.archived_result.source === 'replay' ? '历史补算' : '归档结果'}：
                {labels[task.archived_result.state] ?? '状态未确认'}
                {task.archived_result.count != null ? ` · ${task.archived_result.count} 条` : ''}
                {task.archived_result.completed_at ? ` · ${task.archived_result.completed_at}` : ''}
              </p>}
              {kind !== 'select' && task.windows.some(window => window.signal_name)
                && <p className="mt-0.5 text-[10px] text-muted">
                  信号：{task.windows.map(window => window.signal_name).filter(Boolean).join(' / ')}
                </p>}
              {kind === 'buy' && Object.entries(task.signals ?? {})
                .filter(([, signal]) => signal.phase_policy === 'independent_v1')
                .map(([id, signal]) => (
                  <div key={id} className="mt-1 text-[10px] text-muted" aria-label="独立入场计算">
                    <p title={signal.breadth?.errors?.join('；')}>
                      {`广度：${labels[signal.breadth?.state ?? 'waiting'] ?? '待执行'}`}
                    </p>
                    <p title={signal.stocks?.errors?.join('；')}>
                      {`预选股：${labels[signal.stocks?.state ?? 'waiting'] ?? '待执行'}`}
                    </p>
                    <p>{`内部候选 ${signal.stocks?.count ?? '未知'} · 最终买 ${task.published === true ? task.count ?? 0 : '未确认'}`}</p>
                    <p>{signal.breadth_required === false
                      ? '广度仅展示，个股条件完成后生成买信息'
                      : '两类条件均满足后生成买信息'}</p>
                  </div>
                ))}
              {task.execution_deadline && <p className="text-[10px] text-muted">
                实时入场截止：{task.execution_deadline}
              </p>}
              {task.target_date && <p className="text-[10px] text-muted">
                {kind === 'buy' && task.state === 'blocked' ? '最早完整周期入场日' : '目标交易日'}：{task.target_date}
              </p>}
              {task.completed_at && <p className="text-[10px] text-muted">完成：{task.completed_at}</p>}
              {task.source_date && <p className="text-[10px] text-muted">输入日：{task.source_date}</p>}
              {task.pool_total != null && <p className="text-[10px] text-muted">
                完整范围 {task.pool_total} · 有效日线 {task.pool_usable ?? '未知'}
                {' · 缺失 '}{task.missing_count ?? '未知'}
                {' · 异常 '}{Object.keys(task.invalid_symbols ?? {}).length}
                {task.pool_usable != null && task.pool_total > 0
                  ? ` · 日线可用 ${(100 * task.pool_usable / task.pool_total).toFixed(1)}%` : ''}
              </p>}
              {task.candidate_total != null && <p className="text-[10px] text-muted">
                候选 {task.candidate_total} · 可评分 {task.scorable_total ?? '未知'} · 选中 {task.count ?? '未知'}
              </p>}
              {!!Object.keys(task.invalid_symbols ?? {}).length && <details className="mt-1 text-warning">
                <summary className="cursor-pointer">查看单股异常</summary>
                {Object.entries(task.invalid_symbols ?? {}).map(([symbol, reason]) =>
                  <p key={symbol} className="break-words">{symbol}：{reason}</p>)}
              </details>}
              {!!task.warnings?.length && <p className="mt-1 break-words text-warning">{task.warnings.join('；')}</p>}
              {!!task.errors?.length && <p className="mt-1 break-words text-danger">{task.errors.join('；')}</p>}
            </div>
          )
        })}
      </div>
      {!!value.errors?.length && <p role="alert" className="mt-1 text-danger">{value.errors.join('；')}</p>}
      {value.operational_state?.as_of === value.as_of && !!value.operational_state.issues.length
        && <p role="alert" className="mt-1 text-warning">
          {value.operational_state.issues.map(issue =>
            `${issue.phase === 'stocks' ? '个股' : '广度'}：${issues[issue.kind] ?? '运行异常'}${issue.reason ? ` · ${issue.reason}` : ''}`
          ).join('；')}
        </p>}
    </div>
  )
}
