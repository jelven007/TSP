import { storage } from './storage'
import type { ColumnConfig, ColumnGroup } from './list-columns'

export type StrategyResultTab = 'sell' | 'select' | 'buy'
const common = [['symbol', '股票代码'], ['name', '名称']] as const
const live = [['live_price', '现价'], ['live_change_pct', '涨跌幅'], ['live_turnover', '换手率'],
  ['live_vol_ratio', '量比'], ['live_signals', '信号']] as const
const definitions = {
  sell: [...common, ['entry_date', '买入信号日期'], ['last_datetime', '触发时间'],
    ['close', '触发价'], ['change_pct', '触发时涨幅'], ['score', '信号评分'],
    ['volume', '分钟成交量'], ['amount', '分钟成交额'], ['suggested_sell_price', '建议卖出价'], ...live],
  select: [...common, ['selection_rank', '排序'], ['reference_date', '数值参考日'],
    ['reference_close', '参考价'], ['reference_change_pct', '参考日涨幅'],
    ['reference_float_cap', '流通市值(亿)'], ['amount_mean_20d', '20日均额(亿)'],
    ['score', '选股评分'], ...live],
  buy: [...common, ['last_datetime', '触发时间'], ['close', '触发价'],
    ['change_pct', '触发时涨幅'], ['score', '信号评分'], ['volume', '分钟成交量'], ['amount', '分钟成交额'],
    ['suggested_lots_100k', '建议购买量/10万'], ...live],
} as const

/** 相同字段共用列宽，不让表格按内容重新分配宽度。 */
export const STRATEGY_RESULT_WIDTHS: Record<string, number> = {
  symbol: 108, name: 96, last_datetime: 154, entry_date: 110, reference_date: 100,
  close: 80, reference_close: 80, live_price: 80, change_pct: 100, reference_change_pct: 100,
  live_change_pct: 88, live_turnover: 88, live_vol_ratio: 72, score: 80, selection_rank: 56,
  reference_float_cap: 110, amount_mean_20d: 110, volume: 100, amount: 116,
  suggested_lots_100k: 148, suggested_sell_price: 148, live_signals: 280,
}
export function defaultStrategyResultColumns(tab: StrategyResultTab): ColumnConfig[] {
  return definitions[tab].map(([key, label]) => ({
    id: `builtin:${key}`, source: { type: 'builtin', key }, label,
    visible: key !== 'entry_date' && key !== 'reference_date',
    pinned: key === 'symbol' || key === 'name',
    align: ['symbol', 'name', 'last_datetime', 'live_signals', 'entry_date', 'reference_date'].includes(key)
      ? 'left' : 'right',
  }))
}
export function strategyResultGroups(tab: StrategyResultTab): ColumnGroup[] {
  return [
    { id: 'saved', label: '已保存结果', icon: '📋',
      keys: definitions[tab].map(([key]) => key).filter(key => !key.startsWith('live_')) },
    { id: 'live', label: '当日行情', icon: '📈', keys: live.map(([key]) => key) },
  ]
}
export type StrategyResultColumnConfig = Record<StrategyResultTab, ColumnConfig[]>
export function loadStrategyResultColumns(): StrategyResultColumnConfig {
  const saved = storage.strategySignalColumns.get({})
  return Object.fromEntries((['sell', 'select', 'buy'] as const).map(tab => {
    const defaults = defaultStrategyResultColumns(tab)
    const items = Array.isArray(saved?.[tab]) ? saved[tab] : []
    const seen = new Set<string>()
    const columns: ColumnConfig[] = []
    for (const item of items) {
      const column = defaults.find(col => col.id === item?.id)
      if (!column || seen.has(column.id)) continue
      seen.add(column.id)
      columns.push({ ...column, visible: column.pinned || typeof item.visible !== 'boolean' ? column.visible : item.visible })
    }
    // 旧偏好中新增加的列跟随默认前置列插入，保留已有列的相对顺序与显隐。
    defaults.forEach((column, i) => {
      if (seen.has(column.id)) return
      const previous = defaults.slice(0, i).reverse().find(col => columns.some(item => item.id === col.id))
      const index = previous ? columns.findIndex(col => col.id === previous.id) + 1 : 0
      columns.splice(index, 0, column)
    })
    return [tab, columns]
  })) as StrategyResultColumnConfig
}
export function saveStrategyResultColumns(columns: StrategyResultColumnConfig) {
  storage.strategySignalColumns.set(Object.fromEntries(Object.entries(columns)
    .map(([tab, cols]) => [tab, cols.map(({ id, visible }) => ({ id, visible }))])))
}
