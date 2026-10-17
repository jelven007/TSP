import type { BacktestProvenance } from '@/lib/api'

const percentage = (value?: number | null) =>
  value == null ? '未知' : `${(value * 100).toFixed(1)}%`

export function ResearchInputSummary({ provenance }: { provenance?: BacktestProvenance }) {
  if (!provenance?.historical_inputs && !provenance?.input_snapshot) return null
  const historical = provenance.historical_inputs
  const financial = historical?.financial_publication
  return (
    <div className="space-y-1 rounded-input border border-border p-3 text-xs text-secondary" aria-label="研究输入可信度">
      <p className="break-all">
        固定输入版本：{provenance.input_snapshot?.id ?? '未固定'}
        {provenance.input_snapshot && ` · ${provenance.input_snapshot.file_count} 个文件`}
      </p>
      {historical && <>
        <p>历史证券状态未知比例：{percentage(provenance.research_governance?.status_quality?.unknown_status_ratio)}</p>
        <p>历史退市证券缺失比例：{percentage(historical.universe.missing_delisted_ratio)}（完整股票池分母未验证）</p>
        <p>财务原公告时间未知比例：{percentage(financial?.unknown_ratio)}（保留输入记录数：{financial?.rows ?? '未知'}）</p>
        <p className="text-muted">固定输入支持复测；首次采集时间不能证明原公告时点的值。</p>
      </>}
    </div>
  )
}
