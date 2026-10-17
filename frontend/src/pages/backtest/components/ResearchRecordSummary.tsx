import type { ResearchRecord } from '@/lib/researchRecord'

const REASONS: Record<string, string> = {
  incomplete_search: '存在未完成或失败的试验',
  objective_not_sharpe: '当前优化目标不是夏普',
  insufficient_trials: '参数试验少于两组',
  insufficient_return_samples: '缺少足够的每日组合收益样本',
  inconsistent_return_samples: '各组收益样本长度不同',
  degenerate_trial_variance: '参数组的夏普没有有效差异',
  degenerate_return_moments: '收益分布无法计算校正值',
}

export function ResearchRecordSummary({ research, result }: {
  research?: ResearchRecord
  result: unknown
}) {
  if (!research) return null
  const correction = research.selection_correction
  const exportRecord = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `research-${research.family_id?.slice(0, 12) ?? research.provenance?.job_id ?? 'record'}.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }
  return (
    <div className="space-y-1.5 rounded-input border border-border p-3 text-xs text-secondary">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>研究配置已冻结 · 已记录 {research.n_attempted ?? 0} 次参数试验</span>
        <button type="button" onClick={exportRecord} className="text-accent hover:underline">下载配置与试验记录</button>
      </div>
      {research.n_failed != null && research.n_failed > 0 && <p>{research.n_failed} 组失败或目标无有效值，记录中保留原因。</p>}
      {research.provenance?.public_release_id && <p>行情版本：{research.provenance.public_release_id}</p>}
      {research.data_snapshot && (
        <p className="break-all">固定输入版本：{research.data_snapshot.id} · {research.data_snapshot.file_count} 个文件</p>
      )}
      {correction && (
        <p>
          {correction.status === 'available' && correction.probability != null
            ? `DSR 概率 ${(correction.probability * 100).toFixed(1)}% · ${correction.n_trials} 组 · ${correction.n_obs} 个每日收益样本`
            : `DSR 未计算：${REASONS[correction.status] ?? '所需统计信息不足'}。`}
        </p>
      )}
      {correction?.status === 'available' && (
        <p className="text-[11px] text-muted">仅校正本次参数网格，采用独立试验近似；未估计参数组相关性，也未覆盖历史调参。</p>
      )}
      {!correction && research.family_id && (
        <p className="text-[11px] text-muted">每折使用冻结条件和训练区间选出的参数；记录包含训练试验、测试配置及失败原因。</p>
      )}
    </div>
  )
}
