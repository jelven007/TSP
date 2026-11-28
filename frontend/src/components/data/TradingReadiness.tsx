import { Link } from 'react-router-dom'
import type { ScheduleReadiness } from '@/lib/api'

const stateLabel: Record<string, string> = {
  not_run: '无本期回执', queued: '排队中', pending: '等待执行', running: '执行中',
  succeeded: '完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
  unavailable: '回执不可用', calendar_unknown: '交易日历未覆盖',
}
const coverageLabel = {
  complete: '覆盖已核验', partial: '部分覆盖', pending_publication: '等待发布', unverified: '覆盖未核实',
}
const checkLabel = {
  morning_status: '盘前状态核验', close_sync: '盘后数据补缺',
  close_review: '收盘证据复核', evening_status: '下一交易日公告核验',
}
const numberFormat = new Intl.NumberFormat('zh-CN')
const beijing = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium',
})
const dateTime = (value: string) => beijing.format(new Date(value))
const state = (value: string) => stateLabel[value] ?? '状态未知'

function TaskLink({ kind, day }: { kind: 'stock-status' | 'post-close'; day: string }) {
  const search = new URLSearchParams({ task_context: kind, data_date: day })
  return <Link className="inline-block font-medium text-accent hover:underline"
    to={`/monitor?${search}#strategy-tasks`}>查看相关策略任务</Link>
}

export function TradingReadiness({ value, loading, unavailable }: {
  value?: ScheduleReadiness
  loading: boolean
  unavailable: boolean
}) {
  return (
    <section aria-label="交易数据就绪" className="space-y-3 rounded-card border border-border bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-sm font-medium text-foreground">交易数据就绪</h2>
        <p className="text-xs text-muted">
          {unavailable ? '最近刷新失败' : value ? `北京时间 · ${dateTime(value.observed_at)}` : '北京时间'}
          {value?.automatic_enabled === false ? ' · 自动同步暂停，保留回执' : ''}
        </p>
      </div>
      {value ? <ReadinessCards value={value} /> : <p role="status" className="text-xs text-muted">
        {loading ? '读取交易数据回执…' : '当前无法读取交易数据回执'}
      </p>}
    </section>
  )
}

function ReadinessCards({ value }: { value: ScheduleReadiness }) {
  const { stock_status: stock, post_close: close, next_check: next } = value
  const warning = stock.coverage !== 'complete'
  return (
    <div className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2 xl:grid-cols-3">
      <article className="min-w-0 space-y-2 rounded-md border border-border/60 p-3">
        <h3 className="font-medium text-foreground">目标交易日风险状态</h3>
        <p className="font-mono text-muted">{stock.target_date ?? '交易日待确认'} · 停复牌 / ST</p>
        <p className={warning ? 'text-warning' : 'text-secondary'}>
          {coverageLabel[stock.coverage]} · {state(stock.status)}
          {warning && stock.deadline_elapsed ? ' · 盘前窗口已结束' : ''}
        </p>
        {stock.counts && <p className="text-secondary">
          已核 {stock.counts.covered == null ? '未知' : numberFormat.format(stock.counts.covered)} 只
          {' · 待核 '}{stock.counts.pending == null ? '未知' : numberFormat.format(stock.counts.pending)} 只
          {stock.counts.universe != null ? ` · 应核 ${numberFormat.format(stock.counts.universe)} 只` : ''}
        </p>}
        {(stock.counts?.unscanned != null || stock.counts?.unresolved != null) && <p className="text-muted">
          {stock.counts.unscanned != null ? `目录待扫 ${numberFormat.format(stock.counts.unscanned)} 只` : ''}
          {stock.counts.unscanned != null && stock.counts.unresolved != null ? ' · ' : ''}
          {stock.counts.unresolved != null ? `公告待核 ${numberFormat.format(stock.counts.unresolved)} 只` : ''}
        </p>}
        {stock.scheduled_for && <p className="text-muted">计划 {dateTime(stock.scheduled_for)}</p>}
        {stock.observed_at && <p className="text-muted">实际核验 {dateTime(stock.observed_at)}</p>}
        {stock.next_retry_at && <p className="text-muted">回执计划重试 {dateTime(stock.next_retry_at)}</p>}
        {stock.target_date && <TaskLink kind="stock-status" day={stock.target_date} />}
      </article>
      <article className="min-w-0 space-y-2 rounded-md border border-border/60 p-3">
        <h3 className="font-medium text-foreground">最近交易日行情定版</h3>
        <p className="font-mono text-muted">{close.trade_date ?? '交易日待确认'}</p>
        <p className="text-secondary">
          <span className={close.sync.status === 'failed' ? 'text-warning' : undefined}>
            15:35 补缺{state(close.sync.status)}
          </span>
          {' · 日线'}{close.daily.status === 'succeeded' ? '已定版' : state(close.daily.status)}
          {' · 1分K'}{close.minute.status === 'succeeded' ? '已定版' : state(close.minute.status)}
        </p>
        <p className="text-secondary">
          15:45 复核{state(close.review.status)}
          {close.review.coverage === 'partial' ? ' · 验证部分有效' : ''}
          {close.review.status === 'succeeded' && close.review.coverage === 'unverified' ? ' · 覆盖未核实' : ''}
          {close.review.remaining != null ? ` · 复核剩余 ${numberFormat.format(close.review.remaining)} 条` : ''}
        </p>
        {close.review.valid != null && <p className="text-muted">
          有效 {numberFormat.format(close.review.valid)} 只
          {close.review.suspended != null ? ` · 按停牌证据跳过 ${numberFormat.format(close.review.suspended)} 只` : ''}
        </p>}
        {close.review.finished_at && <p className="text-muted">复核完成 {dateTime(close.review.finished_at)}</p>}
        {close.review.next_retry_at && <p className="text-muted">复核重试 {dateTime(close.review.next_retry_at)}</p>}
        {close.trade_date && <TaskLink kind="post-close" day={close.trade_date} />}
      </article>
      <article className="min-w-0 space-y-2 rounded-md border border-border/60 p-3">
        <h3 className="font-medium text-foreground">下一次关键检查</h3>
        {next ? <>
          <p className="text-secondary">{checkLabel[next.kind]}</p>
          <p className="font-mono text-muted">{dateTime(next.due_at)}</p>
          <p className="text-muted">计划时点，数据是否可用以当期回执为准。</p>
        </> : <p className="text-muted">{value.automatic_enabled ? '暂无可确认的计划时点' : '随自动同步总开关暂停'}</p>}
      </article>
    </div>
  )
}
