import type { CustomSignal, StrategyDetail } from '@/lib/api'
import { customSignalColumn, lateSessionSummary, morningLiquidationSummary, s01SignalSummary } from '@/lib/customSignals'

export function customExecutionDescription(
  detail: StrategyDetail | undefined, signals: CustomSignal[], overrides: Record<string, unknown>,
) {
  const selected = (kind: 'entry' | 'exit') => {
    const columns = (overrides[`${kind}_signals`] ?? detail?.[`${kind}_signals`] ?? []) as string[]
    return columns.flatMap(column => {
      const signal = signals.find(item => customSignalColumn(item) === column)
      if (!signal) return [{ name: column, lines: [column.startsWith('csg') ? '信号定义尚未确认' : '按所选信号确认'] }]
      if (signal.enabled === false) return [{ name: signal.name, lines: ['已禁用，不产生买卖信号'] }]
      const lines = signal.template === 'late_session_strength' ? lateSessionSummary(signal)
        : signal.template === 'morning_liquidation' ? morningLiquidationSummary(signal)
        : signal.template === 's01_late_entry' || signal.template === 's01_morning_exit' ? s01SignalSummary(signal)
        : ['按当前保存的信号条件确认']
      return [{ name: signal.name, lines }]
    })
  }
  const entries = selected('entry'), exits = selected('exit')
  const minute = detail?.execution_backend === 'minute_filter'
  const hasTemplate = (kind: 'entry' | 'exit', template: string) => signals.some(signal =>
    signal.template === template &&
    ((overrides[`${kind}_signals`] ?? detail?.[`${kind}_signals`] ?? []) as string[]).includes(customSignalColumn(signal)))
  const late = hasTemplate('entry', 'late_session_strength')
  const morning = hasTemplate('exit', 'morning_liquidation')
  return {
    entries, exits,
    entryLabel: !entries.length ? '未选择入场触发器' : late
      ? '尾盘走强信号触发时间 → 结束时间' : '所选入场信号确认后的成交窗口',
    exitLabel: !exits.length ? '未选择出场触发器' : morning
      ? '早盘清仓信号触发时间 → 兜底时间' : '所选出场信号确认后的成交窗口',
    entry: minute
      ? `${late ? '尾盘走强' : '分钟入场'}信号确认后，从下一根完整且可成交的分钟开始${late ? '持续买入至信号结束时间' : '按所选信号的成交窗口撮合'}，按每分钟K线的最高价成交；买足提前结束，到期撤余单。`
      : '建仓按所选日线口径确定成交时点，取各分钟K线最高价并受建仓分钟参与率限制；次日开盘从首根分钟起做5分钟容量撮合，信号日收盘使用收盘分钟。',
    exit: morning
      ? '按实际买入日期，在下一交易日观察所选清仓信号；确认后从下一根完整且可成交分钟持续卖出，按每分钟K线的最低价成交，兜底时间到点立即停止，余单撤销、余股保留并报告失败。兜底未设置时，成交截止为当日收盘。'
      : '清仓按所选口径及当前出场信号、风控和持有期限确定时点，取各分钟K线最低价并受清仓分钟参与率限制；未选出场信号时不产生信号卖出。',
    missing: minute && (morning || detail?.minute_execution)
      ? '缺整日分钟K分区直接失败；窗口内缺某分钟跳过该分钟，尝试后续可成交分钟，截止不延长；截止仍有余股保留实际成交并报告失败。'
      : minute ? '逐日核验分钟K覆盖，缺数据的日期及原因随回测记录；仅用已完成行情撮合。'
      : '日线条件只使用已完成数据；分钟成交需要对应历史分钟K。',
  }
}

export function CustomExecutionSummary({ value, compact = false }: { value: ReturnType<typeof customExecutionDescription>; compact?: boolean }) {
  return <div data-testid="custom-execution-description" className="space-y-1 text-[11px] leading-5 text-secondary">
    <p className="font-medium text-foreground">当前策略回测说明</p>
    <p>{value.entry}</p>
    <p>{value.exit}</p>
    <details open={!compact}><summary className="cursor-pointer text-accent">当前入场 / 出场信号参数</summary>
    {([['入场', value.entries], ['出场', value.exits]] as const).map(([label, selected]) =>
      <div key={label}>
        {!selected.length ? <p>{label}：未选择触发器</p> : selected.map(signal =>
          <div key={signal.name}><p className="font-medium">{label} · {signal.name}</p>
            {signal.lines.map(line => <p key={line}>{line}</p>)}
          </div>)}
      </div>)}
    </details>
    <p className="text-muted">{value.missing}</p>
  </div>
}
