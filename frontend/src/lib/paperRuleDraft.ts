import type { PaperAutoRule, PaperFees, StrategyDetail } from './api'
import { BACKTEST_DEFAULTS_VERSION } from '@/pages/backtest/defaults'

function numberIn(value: unknown, max: number) {
  if ((typeof value !== 'string' && typeof value !== 'number') || String(value).trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) && n >= 0 && n <= max ? n : null
}

export function equalEquityPct(exposure: string, slots: string): number | null {
  const total = numberIn(exposure, 100)
  const count = numberIn(slots, 50)
  return total && count && Number.isInteger(count) ? total / count : null
}

export function backtestRuleDraft(
  saved: Record<string, unknown> | null,
  strategy?: Pick<StrategyDetail, 'id' | 'execution_backend' | 'minute_execution'>,
) {
  const rule: Partial<PaperAutoRule> = {}
  const fees: Partial<PaperFees> = {}
  const notes: string[] = []
  if (!saved || saved.defaultsVersion !== BACKTEST_DEFAULTS_VERSION) {
    return { available: false, rule, fees, notes: ['没有当前版本的已保存回测设置；不使用页面默认值代替。'] }
  }
  if (!strategy || strategy.id !== saved.selectedStrategy) {
    return { available: false, rule, fees, notes: ['来源策略尚未加载或已不可用，无法确认成交口径。'] }
  }
  rule.match_kind = 'strategy'
  rule.match_id = strategy.id
  const slots = numberIn(saved.maxPositions, 50)
  const exposure = numberIn(saved.maxExposure, 100)
  if (saved.mode === 'position') {
    if (slots && Number.isInteger(slots)) rule.max_positions = slots
    else notes.push('未同步最大持仓数：需要1～50的整数。')
    if (exposure) rule.max_total_exposure_pct = exposure
    else notes.push('未同步总仓位：回测0表示零投入，跟单0表示不限。')
    if (saved.positionSizing === 'equal') {
      const pct = equalEquityPct(String(saved.maxExposure ?? ''), String(saved.maxPositions ?? ''))
      if (pct != null) { rule.size_mode = 'pct_equity'; rule.size_value = pct }
      else notes.push('未同步等权规模：需正总仓位和有效持仓数。')
    } else notes.push('未同步评分加权：候选评分排序不等于加权分配金额。')
  } else notes.push('未同步全量模拟：实时跟单受账户资金和仓位约束。')
  const minute = strategy.execution_backend === 'minute_filter' || !!strategy.minute_execution
  if (minute || saved.entryFill === 'open_t+1') {
    notes.push(`未同步建仓：${minute ? '分钟容量' : '开盘起5分钟容量'}撮合不等于单一开盘价或即时单。`)
  } else if (saved.entryFill === 'close_t') rule.buy_order_type = 'close'
  else notes.push('未同步建仓：缺少可识别的已保存口径。')
  if (strategy.minute_execution || saved.params && typeof saved.params === 'object'
    && (saved.params as Record<string, unknown>).exit_mode === 'next_morning') {
    notes.push('未同步清仓：专用分钟退出不等于普通开盘或收盘单。')
  } else if (saved.exitFill === 'close_t') rule.sell_order_type = 'close'
  else if (saved.exitFill === 'open_t+1') rule.sell_order_type = 'next_open'
  else notes.push('未同步清仓：下一分钟或未知口径不等于即时单。')
  for (const [source, target, max, divisor] of [
    ['fees', 'commission_pct', 100, 10000],
    ['minimumCommission', 'min_commission', 10000, 1],
    ['transferFee', 'transfer_fee_pct', 100, 10000],
    ['slippage', 'slippage_bps', 200, 1],
  ] as const) {
    const value = numberIn(saved[source], max)
    if (value != null) fees[target] = value / divisor
    else notes.push(`未同步费用 ${source}：缺失或超出范围，保留账户保存值。`)
  }
  if (saved.stampTax === '') fees.stamp_tax_mode = 'historical'
  else {
    const tax = numberIn(saved.stampTax, 50)
    if (tax != null) { fees.stamp_tax_mode = 'fixed'; fees.stamp_tax_pct = tax / 1000 }
    else notes.push('未同步印花税：缺失或无效。')
  }
  notes.push('费用仅填独立草稿，需要另点“保存账户费用”；涨跌停排队沿账户设置。')
  if (saved.overrides && Object.keys(saved.overrides).length) notes.push('未同步策略覆盖：基础过滤、日线叠加、评分和风控覆盖仍在策略页维护。')
  notes.push('初始资金、区间、研究校验不导入；策略定义不修改，已成交订单不重算。')
  return { available: true, rule, fees, notes }
}
