export const BASIC_FILTER_RANGES = [
  { key: 'market_cap', label: '总市值', unit: '亿', scale: 1e8, step: '5' },
  { key: 'float_cap', label: '流通市值', unit: '亿', scale: 1e8, step: '5' },
  { key: 'amount', label: '成交额', unit: '亿', scale: 1e8, step: '0.5' },
  { key: 'turnover', label: '换手率', unit: '%', scale: 1, step: '0.5' },
  { key: 'change_pct', label: '涨跌幅', unit: '%', scale: 1, step: '0.1' },
  { key: 'amplitude', label: '上下振幅', unit: '%', scale: 1, step: '0.1' },
  { key: 'price', label: '价格范围', unit: '元', scale: 1, step: '0.1' },
] as const

type RangeKey = `${typeof BASIC_FILTER_RANGES[number]['key']}_${'min' | 'max'}`
export type NumericBasicFilter = Partial<Record<RangeKey | 'exclude_new_days', number | null>> & {
  enabled?: boolean
  data_reference?: 'previous_trade_day' | 'signal_day'
}

export function numericBasicFilter(value: NumericBasicFilter): NumericBasicFilter {
  return Object.fromEntries([
    ['enabled', value.enabled !== false],
    ['data_reference', 'previous_trade_day'],
    ...BASIC_FILTER_RANGES.flatMap(({ key }) => (['min', 'max'] as const)
      .map(side => [`${key}_${side}`, value[`${key}_${side}`] ?? null])),
    ['exclude_new_days', value.exclude_new_days ?? null],
  ])
}

export function validateBasicFilter(value: NumericBasicFilter): string | null {
  for (const { key, label } of BASIC_FILTER_RANGES) {
    const low = value[`${key}_min`], high = value[`${key}_max`]
    for (const number of [low, high]) {
      if (key === 'change_pct') {
        if (number != null && (!Number.isFinite(number) || number < -50 || number > 50
          || Math.abs(number * 10 - Math.round(number * 10)) > 1e-8)) return '涨跌幅须在 -50%～+50% 之间，最多保留1位小数'
        continue
      }
      if (number != null && (!Number.isFinite(number) || number < 0)) return `${label}必须为非负数值`
    }
    if (low != null && high != null && low > high) return `${label}最小值不能大于最大值`
  }
  const days = value.exclude_new_days
  if (days != null && (!Number.isInteger(days) || days < 0)) return '上市天数必须为非负整数'
  return null
}
