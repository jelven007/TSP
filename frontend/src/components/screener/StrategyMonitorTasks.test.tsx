import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { StrategyMonitoring } from '@/lib/api'
import { StrategyMonitorTasks } from './StrategyMonitorTasks'

it('shows calendar, retained scope and excluded members without shrinking the pool', () => {
  const value: StrategyMonitoring = {
    strategy_id: 'custom_mvxxcp88', enabled: true, as_of: '2026-11-20', is_trading_day: true,
    tasks: {
      select: { state: 'success', windows: [], pool_total: 5224, pool_usable: 5216,
        missing_count: 1, invalid_symbols: { '002122.SZ': '缺少分钟收盘证据' },
        candidate_total: 31, scorable_total: 30, count: 30, source_date: '2026-11-20' },
      buy: { state: 'waiting', windows: [] }, sell: { state: 'waiting', windows: [] },
    },
  }
  const html = renderToStaticMarkup(<StrategyMonitorTasks value={value} />)
  expect(html).toContain('完整范围 5224')
  expect(html).toContain('有效日线 5216')
  expect(html).toContain('缺失 1')
  expect(html).toContain('002122.SZ：缺少分钟收盘证据')
  expect(html).toContain('输入日：2026-11-20')
  expect(html).toContain('自定义策略盘后“选”任务')
  const weekend = renderToStaticMarkup(<StrategyMonitorTasks value={{ ...value, is_trading_day: false }} />)
  expect(weekend).toContain('非交易日')
})

it('shows independent phase receipts and keeps candidates distinct from final buy count', () => {
  const value: StrategyMonitoring = {
    strategy_id: 'custom_mvxxcp88', enabled: true, as_of: '2026-11-23',
    tasks: {
      select: { state: 'success', windows: [] }, sell: { state: 'waiting', windows: [] },
      buy: { state: 'failed', windows: [], count: 0, published: false, signals: {
        late: { phase_policy: 'independent_v1', breadth: { state: 'failed', errors: ['缺分钟'] },
          stocks: { state: 'success', count: 3 } },
      } },
    },
  }
  const html = renderToStaticMarkup(<StrategyMonitorTasks value={value} />)
  expect(html).toContain('广度：计算失败')
  expect(html).toContain('预选股：已计算')
  expect(html).toContain('内部候选 3')
  expect(html).toContain('最终买 未确认')
  expect(html).toContain('两类条件均满足后生成买信息')
  const rejected = renderToStaticMarkup(<StrategyMonitorTasks value={{ ...value, tasks: {
    ...value.tasks, buy: { ...value.tasks.buy, state: 'rejected', published: true, count: 0 },
  } }} />)
  expect(rejected).toContain('最终买 0')
})

it.each([1, 2, 3, 4] as const)('shows S05 integer branch %s and matching schedules', code => {
  const value: StrategyMonitoring = {
    strategy_id: 'custom_mvxxcs05', enabled: true, as_of: '2026-11-27', branch_code: code,
    tasks: {
      select: { state: 'waiting', windows: [{ start: '22:00:00', end: '23:00:00' }] },
      buy: { state: code === 4 ? 'rejected' : 'waiting', windows: code === 4 ? [] : [
        { start: '14:17:00', end: '14:28:00', signal_name: 'S05对应入场信号' }] },
      sell: { state: 'waiting', windows: [
        { start: '10:11:00', end: '11:40:00', signal_name: 'S05对应出场信号' }] },
    },
  }
  const html = renderToStaticMarkup(<StrategyMonitorTasks value={value} />)
  expect(html).toContain(`分支参数：${code}`)
  expect(html).toContain(['尾段加速', '低缺口资金趋势', '板块强势综合', '均不满足'][code - 1])
  expect(html).toContain('S05盘后“选”任务')
  if (code === 4) expect(html).toContain('不安排买入')
  else {
    expect(html).toContain('14:17:00～14:28:00')
  }
  expect(html).toContain('10:11:00～11:40:00')
  expect(html).toContain('S05对应出场信号')
  expect(html).toContain('按前一交易日买入分支匹配出场信号')
})
