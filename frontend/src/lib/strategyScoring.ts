import type { StrategyDetail } from './api'

export function strategyScoringDescription(detail: Pick<StrategyDetail, 'id'>): string {
  if (detail.id === 'custom_mvxxcs01') {
    // 这里只解释固定评分契约；实际数值由共享因子计算，当前条件见 S01 唯一参考页。
    return '使用因子「当日低振幅成交额评分」（权重100%）：评分 = −当日振幅 + 0.001 × ln（当日累计成交额）。振幅为小数比例，成交额单位为元；原始评分降序，同分按股票代码升序。'
  }
  if (detail.id === 'custom_mvxxcs03') {
    return 'S03预选按五维综合评分：技术40%、资金20%、情绪20%、基本面10%、消息10%；各维度0～100分，综合分降序、同分代码升序，取前30。恢复后的入场顺序为先A组后B组：A组按五维排名升序，B组按资金分降序，再按五维排名、代码升序；入场排名分为0～100分，五维综合分另存。旧冻结结果保留原评分口径。'
  }
  if (detail.id === 'custom_mvxxcp88') {
    return '排名由策略生成。s02按ATR14÷昨收降序，排名分数为0～100分，同值按触发涨幅、股票代码排序。'
  }
  return '排名由策略生成，读取当前源码，按保存的排序方向及同分规则执行。'
}
