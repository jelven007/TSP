import type { EntryGroups } from './api'

export const ENTRY_GROUP_FIELDS = [
  ['score_min', '综合分下限', 0, 100, .1],
  ['tech_min', '技术分下限', 0, 100, .1],
  ['fund_min', '资金分下限', 0, 100, .1],
  ['momentum_min_pct', '20日动量下限（%）', -100, 10000, .1],
  ['momentum_max_pct', '20日动量上限（%）', -100, 10000, .1],
  ['bias_max_pct', 'MA20乖离上限（%）', -100, 10000, .1],
  ['slope_min_pct', 'MA20五日变化下限（%）', -100, 10000, .1],
  ['rank_min', '五维排名下限', 1, 30, 1],
  ['rank_max', '五维排名上限', 1, 30, 1],
  ['breadth_min_pct', '前日上涨广度下限（%）', 0, 100, .1],
  ['gap_min_pct', '开盘缺口下限（%）', -100, 10000, .1],
  ['gap_max_pct', '开盘缺口上限（%）', -100, 10000, .1],
  ['intraday_min_pct', '相对开盘涨幅下限（%）', -100, 10000, .1],
  ['position_min_pct', '日内价格位置下限（%）', 0, 100, .1],
] as const

export const defaultEntryGroups = (): EntryGroups => ({
  a: { enabled: true, score_min: 55, tech_min: 75, fund_min: 70,
    momentum_min_pct: 18, momentum_max_pct: 40, bias_max_pct: 100, slope_min_pct: 1,
    rank_min: 2, rank_max: 20, breadth_min_pct: null, gap_min_pct: -1, gap_max_pct: 10,
    intraday_min_pct: .8, position_min_pct: 90 },
  b: { enabled: true, score_min: 70, tech_min: 65, fund_min: 40,
    momentum_min_pct: 5, momentum_max_pct: 50, bias_max_pct: 25, slope_min_pct: 0,
    rank_min: 6, rank_max: 25, breadth_min_pct: 25, gap_min_pct: -5, gap_max_pct: 1,
    intraday_min_pct: null, position_min_pct: null },
})

export function validateEntryGroups(groups?: EntryGroups | null) {
  if (!groups) return
  if (!groups.a.enabled && !groups.b.enabled) throw new Error('A/B入场筛选至少启用一组')
  for (const name of ['a', 'b'] as const) {
    const group = groups[name]
    if (group.enabled && !ENTRY_GROUP_FIELDS.some(([key]) => group[key] != null))
      throw new Error(`${name.toUpperCase()}组启用时至少设置一个条件`)
    for (const [key, label, min, max, step] of ENTRY_GROUP_FIELDS) {
      const value = group[key]
      if (value != null && (!Number.isFinite(value) || value < min || value > max
        || (step === 1 && !Number.isInteger(value))))
        throw new Error(`${name.toUpperCase()}组${label}须为${min}～${max}${step === 1 ? '的整数' : ''}`)
    }
    for (const [min, max, label] of [
      ['momentum_min_pct', 'momentum_max_pct', '20日动量'],
      ['rank_min', 'rank_max', '五维排名'], ['gap_min_pct', 'gap_max_pct', '开盘缺口'],
    ] as const)
      if (group[min] != null && group[max] != null && group[min]! > group[max]!)
        throw new Error(`${name.toUpperCase()}组${label}下限不得大于上限`)
  }
}
