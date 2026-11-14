export const BACKTEST_DEFAULTS_VERSION = 2

export const REQUESTED_BACKTEST_DEFAULTS = {
  maxPositions: '4',
  stampTax: '1',
  slippage: '10',
  minimumCommission: '5',
  transferFee: '5',
} as const

type BacktestDefaults = {
  [K in keyof typeof REQUESTED_BACKTEST_DEFAULTS]: string
} & {
  defaultsVersion: number
}

export function loadBacktestDefaults<T extends Record<string, unknown>>(
  saved: T | null,
): T & BacktestDefaults {
  const state = (saved ?? {}) as T
  if (typeof state.defaultsVersion === 'number' && state.defaultsVersion >= BACKTEST_DEFAULTS_VERSION) {
    return state as T & BacktestDefaults
  }
  if (state.defaultsVersion === 1) {
    // 只更新旧持仓默认值，避免再次覆盖已经保存的费用设置。
    return {
      ...state,
      maxPositions: state.maxPositions == null || state.maxPositions === '5'
        ? REQUESTED_BACKTEST_DEFAULTS.maxPositions
        : state.maxPositions,
      defaultsVersion: BACKTEST_DEFAULTS_VERSION,
    } as T & BacktestDefaults
  }
  return {
    ...state,
    ...REQUESTED_BACKTEST_DEFAULTS,
    defaultsVersion: BACKTEST_DEFAULTS_VERSION,
  }
}
