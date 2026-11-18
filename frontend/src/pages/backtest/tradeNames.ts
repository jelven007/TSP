import type { StrategyBacktestTrade } from '@/lib/api'

type NamedTrade = Pick<StrategyBacktestTrade, 'symbol' | 'name'>

function savedName(trade: NamedTrade): string {
  const name = trade.name?.trim() ?? ''
  return name === trade.symbol || name === trade.symbol.split('.')[0] ? '' : name
}

/** Frozen names take precedence; directory lookup only supplies missing labels. */
export function tradeName(trade: NamedTrade, names: Record<string, string> = {}): string {
  return savedName(trade) || savedName({ symbol: trade.symbol, name: names[trade.symbol] }) || trade.symbol
}

export function missingTradeNameSymbols(trades: NamedTrade[]): string[] {
  const known = new Set(trades.filter(trade => savedName(trade)).map(trade => trade.symbol))
  return [...new Set(trades.filter(trade => !known.has(trade.symbol)).map(trade => trade.symbol))].sort()
}
