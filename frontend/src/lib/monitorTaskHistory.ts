import type { AlertEvent } from './api'

export type TaskHistoryEvent = AlertEvent & { taskDate?: string; taskKind?: string; taskRecords?: AlertEvent[] }

export function groupMonitorTaskHistory(events: AlertEvent[]): TaskHistoryEvent[] {
  const output: TaskHistoryEvent[] = [], groups = new Map<string, TaskHistoryEvent>()
  for (const event of [...events].sort((a, b) => b.ts - a.ts)) {
    if (!event.strategy_id || !['signal_calculation_failed', 'signal_calculation_succeeded'].includes(event.type)) {
      output.push(event)
      continue
    }
    const kind = String(event.details?.task ?? (event.signals?.find(value => ['buy', 'sell', 'select'].includes(value)) || 'buy'))
    const day = String(event.details?.as_of ?? event.message.match(/\d{4}-\d{2}-\d{2}/)?.[0]
      ?? new Date(event.ts).toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' }))
    const key = `${event.strategy_id}:${day}:${kind}`
    const previous = groups.get(key)
    if (previous) previous.taskRecords!.push(event)
    else {
      const row = { ...event, taskDate: day, taskKind: kind, taskRecords: [event] }
      groups.set(key, row)
      output.push(row)
    }
  }
  return output
}
