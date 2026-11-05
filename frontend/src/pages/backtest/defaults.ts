export const BACKTEST_DEFAULTS_VERSION = 1

export const REQUESTED_BACKTEST_DEFAULTS = {
  maxPositions: '5',
  stampTax: '1',
  slippage: '10',
  minimumCommission: '5',
  transferFee: '5',
} as const

type BacktestDefaults = typeof REQUESTED_BACKTEST_DEFAULTS & {
  defaultsVersion: number
}

export function loadBacktestDefaults<T extends Record<string, unknown>>(
  saved: T | null,
): T & BacktestDefaults {
  const state = (saved ?? {}) as T
  if (state.defaultsVersion === BACKTEST_DEFAULTS_VERSION) {
    return state as T & BacktestDefaults
  }
  return {
    ...state,
    ...REQUESTED_BACKTEST_DEFAULTS,
    defaultsVersion: BACKTEST_DEFAULTS_VERSION,
  }
}
