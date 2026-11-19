import type { StrategyMonitoring } from '@/lib/api'

const labels: Record<string, string> = {
  disabled: '未开启', unconfigured: '未配置', waiting: '待执行',
  running: '执行中', success: '已计算', rejected: '已计算 · 未通过',
  failed: '计算失败', expired: '已结束', cancelled: '已取消',
}
const descriptions = { select: '保存至下个交易日选', buy: '同步至下个交易日卖', sell: '触发时提示卖出' }

export function StrategyMonitorTasks({ value, pending = false, onToggle }: {
  value: StrategyMonitoring
  pending?: boolean
  onToggle?: () => void
}) {
  return (
    <div aria-label="策略定时任务" className="mt-3 border-t border-border pt-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="text-muted">每交易日任务 · 北京时间 · {value.as_of}</span>
        {onToggle && <button disabled={pending} onClick={onToggle}
          className="shrink-0 text-accent hover:underline disabled:opacity-50">
          {pending ? '保存中…' : value.enabled ? '取消监控' : '开启监控'}
        </button>}
      </div>
      <div className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2">
        {(['select', 'buy', 'sell'] as const).map(kind => {
          const task = value.tasks[kind]
          return (
            <div key={kind} title={task.errors?.join('；')} className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <strong>{{ select: '选', buy: '买', sell: '卖' }[kind]}</strong>
                <span className="font-mono text-muted">
                  {task.windows.map(w => `${w.start}～${w.end}`).join(' / ') || '未配置时段'}
                </span>
                <span className={task.state === 'failed' ? 'text-danger' : 'text-muted'}>
                  {labels[task.state] ?? '待执行'}
                </span>
              </div>
              <p className="mt-0.5 text-[10px] text-muted">{descriptions[kind]}</p>
              {task.completed_at && <p className="text-[10px] text-muted">完成：{task.completed_at}</p>}
              {!!task.errors?.length && <p className="mt-1 break-words text-danger">{task.errors.join('；')}</p>}
            </div>
          )
        })}
      </div>
      {!!value.errors?.length && <p role="alert" className="mt-1 text-danger">{value.errors.join('；')}</p>}
    </div>
  )
}
