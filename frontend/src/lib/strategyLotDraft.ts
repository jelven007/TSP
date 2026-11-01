import type { StrategyDetail, StrategyResultHistorySummary } from './api'

export interface LotBatchStockDraft {
  symbol: string
  name: string
  qty: string
  cost: string
}

/** 只取本页成交字段；缺失成交数据交给用户补填，不用现价或卖出计划替代。 */
export function strategyLotStocks(rows: Record<string, unknown>[]): LotBatchStockDraft[] {
  const seen = new Set<string>()
  return rows.flatMap(row => {
    if (typeof row.symbol !== 'string' || !row.symbol.trim() || seen.has(row.symbol)) return []
    seen.add(row.symbol)
    const qty = row.filled_shares, cost = row.buy_price
    return [{
      symbol: row.symbol, name: typeof row.name === 'string' ? row.name : '',
      qty: typeof qty === 'number' && Number.isFinite(qty) && qty >= 0 ? String(qty) : '',
      cost: typeof cost === 'number' && Number.isFinite(cost) && cost > 0 ? String(cost) : '',
    }]
  })
}

type RiskSource = Pick<StrategyDetail, 'params_defaults' | 'take_profit' | 'stop_loss'>
  | Pick<StrategyResultHistorySummary, 'params' | 'overrides'>

/** 分钟策略用专用参数，普通策略用通用退出设置；归档仅使用其自己的参数及覆盖。 */
export function strategyLotRisk(source: RiskSource) {
  const saved = 'overrides' in source
  const overrides = saved ? source.overrides : {}
  const params = saved
    ? { ...source.params, ...(overrides.params as Record<string, unknown> | undefined) }
    : source.params_defaults
  const percent = (param: string, field: 'take_profit' | 'stop_loss') => {
    const value = Object.prototype.hasOwnProperty.call(params, param) ? params[param]
      : saved ? overrides[field] : source[field]
    return typeof value === 'number' && Number.isFinite(value) && value >= 0
      ? String(Number((value * 100).toFixed(10))) : ''
  }
  return { target_pct: percent('take_profit_pct', 'take_profit'), stop_pct: percent('stop_loss_pct', 'stop_loss') }
}
