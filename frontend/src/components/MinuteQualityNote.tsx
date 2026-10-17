import type { MinutePriceQuality } from '@/lib/api'

export function MinuteQualityNote({ quality }: { quality?: MinutePriceQuality | null }) {
  if (!quality || (!quality.excluded_rows && !quality.unknown_rule_rows)) return null
  return (
    <span
      className="text-[10px] text-amber-600 dark:text-amber-400"
      title="全市场分钟记录统计。异常记录不参与均值；规则未知表示证券历史、交易日或价格基准不足，保留数据但尚未确认价格限制。"
    >
      已剔除 {quality.excluded_rows} 条异常
      {quality.unknown_rule_rows > 0 && ` · ${quality.unknown_rule_rows} 条规则未知`}
    </span>
  )
}
