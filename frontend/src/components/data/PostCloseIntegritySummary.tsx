import type { PostCloseIntegrityStatus } from '@/lib/api'

const STATUS_LABEL: Record<string, string> = {
  not_run: '当日尚未执行', pending: '等待执行', running: '复核修复中',
  succeeded: '已完成', failed: '仍有异常', cancelled: '已取消', interrupted: '已中断',
}

function beijingClock(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '待确认' : date.toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  })
}

export function PostCloseIntegritySummary({ status, enabled, unavailable = false }: {
  status?: PostCloseIntegrityStatus
  enabled: boolean
  unavailable?: boolean
}) {
  const current = status?.day === status?.today
  const label = !enabled ? '已暂停'
    : unavailable || status?.available === false ? '状态不可用'
    : !status ? '等待状态'
    : status.is_trading_day === false ? '非交易日'
    : !current ? '当日尚未执行'
    : STATUS_LABEL[status.status] ?? '状态不可用'
  const warning = label === '仍有异常' || label === '状态不可用' || label === '已中断'
  const result = status?.result
  const gaps = result?.gap_check
  return (
    <div aria-label="当日遗留复核与修复" className="mt-2 border-t border-border/60 pt-2 text-[10px] leading-relaxed">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium text-foreground">当日遗留复核与修复</span>
        <span className="font-mono text-secondary">交易日 15:45（北京时间）</span>
        <span role="status" className={warning ? 'text-warning' : 'text-secondary'}>{label}</span>
      </div>
      <p className="mt-0.5 text-muted">
        补齐当日日线缺口，复核收盘源端时间、15:00收盘分钟与加工表；异常重新取数并重验，同日失败自动重试。
      </p>
      {status?.day ? <p className="mt-1 font-mono text-muted">
        {current ? '交易日' : '最近记录'} {status.day}
        {status.attempts != null ? ` · 已尝试 ${status.attempts} 次` : ''}
        {enabled && current && status.next_retry_at && status.status !== 'cancelled'
          ? ` · 下次重试 ${beijingClock(status.next_retry_at)}` : ''}
      </p> : null}
      {result ? <p className="mt-0.5 text-secondary">
        已验证结果：修复日线/收盘证据 {result.repaired_symbols?.length ?? 0} 只
        {gaps?.rows != null ? ` · 补缺写入 ${gaps.rows} 行` : ''}
        {gaps?.remaining != null ? ` · 剩余缺口 ${gaps.remaining}` : ''}
        {current && status.status === 'succeeded' ? ' · 收盘证据与加工表已重验' : ''}
      </p> : null}
      {status?.error ? <p className="mt-0.5 break-words text-warning">{status.error}</p> : null}
      {!enabled ? <p className="mt-0.5 text-muted">随自动同步总开关暂停，保留最近结果。</p> : null}
    </div>
  )
}
