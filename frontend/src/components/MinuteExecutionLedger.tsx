import type { StrategyBacktestResult } from '@/lib/api'

export function MinuteExecutionLedger({ result }: { result: StrategyBacktestResult }) {
  if (!result.fills?.length && !result.entry_orders?.length) return null
  const execution = result.stats.execution
  const remaining = Array.isArray(execution?.remaining_positions) ? execution.remaining_positions : []
  const download = () => {
    const url = URL.createObjectURL(new Blob(
      [JSON.stringify({ config: result.config, entry_orders: result.entry_orders, fills: result.fills, signals: result.signals, execution: result.stats.execution }, null, 2)],
      { type: 'application/json' },
    ))
    const link = document.createElement('a')
    link.href = url
    link.download = `minute-execution-${result.run_id}.json`
    link.click()
    URL.revokeObjectURL(url)
  }
  return (
    <details className="rounded-md border border-border bg-surface px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium text-foreground">分钟成交分片（{result.fills?.length ?? 0}行）· 卖出信号（{result.signals?.filter(signal => signal.side !== 'buy').length ?? 0}笔）</summary>
      {result.error && (
        <p className="mt-2 text-warning">
          本次实际成交与持仓已保留，未生成完整净值。
          {remaining.map((position: { symbol: string; remaining: number }) => `${position.symbol} 剩余${position.remaining}股`).join('；')}
        </p>
      )}
      <button type="button" onClick={download} className="my-2 rounded-btn border border-border px-2 py-1 text-secondary">下载完整成交与信号</button>
      {!!result.entry_orders?.length && (
        <div className="mb-2 border-y border-border/50">
          {result.entry_orders.slice(0, 20).map((order, index) => (
            <div key={`${order.symbol}-${order.window_start}-${index}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/30 py-2 last:border-b-0">
              <span className="font-mono text-foreground">{order.symbol}</span>
              <span>目标 {order.target_shares.toLocaleString()} 股</span>
              <span>成交 {order.filled_shares.toLocaleString()} 股</span>
              <span>撤单 {order.cancelled_shares.toLocaleString()} 股</span>
              <span className="text-muted">均价 {order.average_price == null ? '—' : order.average_price.toFixed(4)}</span>
            </div>
          ))}
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full whitespace-nowrap text-left">
          <thead><tr>{['完成分钟', '证券', '方向', '股数', '价格', '增量费用', '参与率'].map(label => <th key={label} className="p-1 font-medium text-muted">{label}</th>)}</tr></thead>
          <tbody>{result.fills?.slice(0, 20).map((fill, i) => (
            <tr key={`${fill.datetime}-${fill.symbol}-${i}`} className="border-t border-border/30">
              <td className="p-1">{fill.datetime}</td><td>{fill.symbol}</td><td>{fill.side === 'buy' ? '买入' : '卖出'}</td>
              <td>{fill.shares}</td><td>{fill.price.toFixed(4)}</td><td>{fill.fee.toFixed(4)}</td><td>{(fill.participation * 100).toFixed(2)}%</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <p className="mt-2 text-muted">显示前20行；最低佣金按同证券、同交易日、同方向累计计算。下载包含全部分片及信号。</p>
    </details>
  )
}
