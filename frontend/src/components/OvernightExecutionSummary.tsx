/** 特殊分钟执行的统一说明，参数编辑器和回测入口共用。 */
export function OvernightExecutionSummary({ params, momentum = false }: {
  params: Record<string, unknown>
  momentum?: boolean
}) {
  const percent = (key: string, fallback: number) => `${(Number(params[key] ?? fallback) * 100).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')}%`
  const money = (key: string, fallback: number) => Number(params[key] ?? fallback).toLocaleString('zh-CN')
  return (
    <div className="rounded-xl border border-sky-500/25 bg-sky-500/5 p-3 text-xs leading-5 text-secondary" data-testid="overnight-execution">
      <p className="font-medium text-foreground">隔夜分钟执行</p>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt>买入</dt><dd>{momentum ? '14:30确认，14:32单分钟最高价按容量买入' : '14:45确认，14:47～14:49按容量分片'}</dd>
        <dt>卖出</dt><dd>{momentum
          ? `次交易日扣费净${percent('stop_loss_pct', .06)}止损优先、毛${percent('take_profit_pct', .06)}止盈；${String(params.guard_time ?? '10:30')}兜底`
          : `次交易日11:15完整早盘判断；3分钟放量下跌；${String(params.guard_time ?? '11:22')}兜底`}</dd>
        <dt>成交</dt><dd>{momentum ? '分钟内触价模拟' : '信号后下一分钟'}，最晚{String(params.sell_deadline ?? (momentum ? '11:30' : '13:30'))}；午休不成交；分钟参与率{percent('participation', momentum ? .5 : .1)}</dd>
        <dt>资金</dt><dd>现金{money('initial_cash', 100000)}元，单票含费≤净值{percent('entry_nav_fraction', .2)}，连续复利{Number(params.entry_budget ?? 0) > 0 ? `，另限${money('entry_budget', 0)}元` : ''}，最多{String(params.position_limit ?? 5)}只</dd>
        <dt>费用</dt><dd>佣金{percent('commission', momentum ? .0002 : .00025)}，同股票同日同方向最低{money('minimum_commission', 5)}元；过户费{percent('transfer_fee', momentum ? 0 : .00001)}；卖出印花税{percent('stamp_tax', .0005)}</dd>
      </dl>
      <p className="mt-2 text-muted">按字段旁的单位编辑；选股、资金、费用及卖出门槛在「策略参数」中设置。到期余股保留持仓并标记失败。</p>
      <p className="mt-1 text-muted">支持历史回测与持仓信号监控；此隔夜策略的模拟账户自动委托尚不支持容量分片。研究结果未获独立样本验证。</p>
    </div>
  )
}
