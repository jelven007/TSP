import type { BacktestProvenance } from '@/lib/api'

const percentage = (value?: number | null) =>
  value == null ? '未知' : `${(value * 100).toFixed(1)}%`

export function ResearchInputSummary({ provenance, onReplay, onNewInputs, disabled, runtime }: {
  provenance?: BacktestProvenance
  onReplay?: () => void
  onNewInputs?: () => void
  disabled?: boolean
  runtime?: { app_sources_sha256: string }
}) {
  if (!provenance?.historical_inputs && !provenance?.input_snapshot) return null
  const historical = provenance.historical_inputs
  const financial = historical?.financial_publication
  return (
    <div className="space-y-1 rounded-input border border-border p-3 text-xs text-secondary" aria-label="研究输入可信度">
      <p className="break-all">
        固定输入版本：{provenance.input_snapshot?.id ?? '未固定'}
        {provenance.input_snapshot && ` · ${provenance.input_snapshot.file_count} 个文件`}
      </p>
      <details>
        <summary className="cursor-pointer">查看策略、参数与运行版本</summary>
        <div className="mt-2 space-y-1 break-all">
          <p>策略指纹：{provenance.strategy_hash ?? '未知'}</p>
          <p>配置指纹：{provenance.config_hash ?? '未知'}</p>
          <p>数据发布：{provenance.dataset_release_id ?? '未知'}</p>
          <p>记录时间：{provenance.captured_at ?? '未知'}</p>
          <p>运行构建：{runtime?.app_sources_sha256 ?? provenance.input_snapshot?.runtime?.app_sources_sha256 ?? '未知'}</p>
        </div>
      </details>
      {historical && <>
        <p>历史证券状态未知比例：{percentage(provenance.research_governance?.status_quality?.unknown_status_ratio)}</p>
        <p>历史退市证券缺失比例：{percentage(historical.universe.missing_delisted_ratio)}（完整股票池分母未验证）</p>
        <p>财务原公告时间未知比例：{percentage(financial?.unknown_ratio)}（保留输入记录数：{financial?.rows ?? '未知'}）</p>
        <p className="text-muted">固定输入支持复测；首次采集时间不能证明原公告时点的值。</p>
      </>}
      {(onReplay || onNewInputs) && <div className="flex flex-wrap gap-3 pt-2">
        {onReplay && <button type="button" disabled={disabled} onClick={onReplay}
          className="text-accent hover:underline disabled:opacity-40">原输入复测</button>}
        {onNewInputs && <button type="button" disabled={disabled} onClick={onNewInputs}
          className="text-accent hover:underline disabled:opacity-40">用新数据建立试验</button>}
        <span className="text-muted">使用原结果的参数；原输入复测仍需匹配原运行构建。</span>
      </div>}
    </div>
  )
}
