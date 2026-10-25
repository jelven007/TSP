import type { DataQuality, InitializationStatus } from '@/lib/api/dataQuality'

const STAGE_NAMES: Record<string, string> = {
  instruments: '股票目录', minute: '分钟K', daily: '日K', enriched: '指标',
  financials: '财务', ai: 'AI', notifications: '通知',
}
const STATE_NAMES: Record<string, string> = {
  pending: '等待首次同步', queued: '已排队', running: '执行中', cancelling: '取消中',
  completed: '首次同步已完成', retry_required: '同步未完成，可重试', manual: '手动模式',
  waiting: '等待数据', available: '有数据', warming: '预热中', unavailable: '无可用能力',
  unknown: '需单独检查配置',
}
const ratio = (value: number | null | undefined) =>
  value == null ? '未知' : `${(value * 100).toFixed(1)}%`
const count = (value: number | null | undefined) => value == null ? '未知' : String(value)

export function DataReadiness({ quality, initialization }: {
  quality?: DataQuality
  initialization?: InitializationStatus
}) {
  if (!quality && !initialization) return null
  return (
    <section aria-label="数据就绪与质量" className="rounded-card border border-border bg-surface p-4 space-y-3">
      {initialization && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <h3 className="font-medium">数据就绪与质量</h3>
            <span role="status" className="text-secondary">
              {STATE_NAMES[initialization.status] ?? initialization.status}
              {initialization.progress != null ? ` · ${initialization.progress}%` : ''}
            </span>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-secondary">
            {Object.entries({ ...STAGE_NAMES, ...initialization.stages })
              .filter(([name]) => name in initialization.stages)
              .map(([name, state]) => (
                <span key={name}>{STAGE_NAMES[name] ?? name}：{STATE_NAMES[state] ?? state}</span>
              ))}
          </div>
          {initialization.marker_error && <p className="text-xs text-danger">初始化记录不可读，请检查存储并重试同步。</p>}
          <p className="text-xs text-muted">
            最近成功同步：{initialization.last_successful_pipeline_at ?? '暂无成功记录'}
          </p>
        </>
      )}
      {quality && (
        <details className="text-xs">
          <summary className="cursor-pointer text-secondary">查看数据质量与历史研究限制</summary>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-3 text-secondary">
            <div>日K检查分区：{quality.latest_daily.partition ?? '未知'}</div>
            <div>相对当前目录覆盖：{ratio(quality.latest_daily.coverage_ratio)}</div>
            <div>异常价格行：{count(quality.latest_daily.invalid_price_rows)}</div>
            <div>异常成交量行：{count(quality.latest_daily.invalid_volume_rows)}</div>
            <div>财务原公告时间未知比例：{ratio(quality.historical_inputs.financial_publication.unknown_ratio)}</div>
            <div>历史退市池缺失比例：{ratio(quality.historical_inputs.universe.missing_delisted_ratio)}</div>
          </div>
          <p className="mt-3 text-muted leading-relaxed">
            仅检查最新日K分区；当前目录覆盖不代表历史股票池完整。源时间尚未认证，
            历史PIT尚未验证，严格研究仍须通过原有门槛。
          </p>
          {quality.latest_daily.error && <p className="mt-2 text-danger">最新分区质量未能完成检查，数值保持未知。</p>}
        </details>
      )}
    </section>
  )
}
