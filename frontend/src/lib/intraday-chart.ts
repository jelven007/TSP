import type { NullableMinuteKlineRow } from '@/lib/api'

export type DailySummary = {
  date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** 仅汇总实际收到的分钟，不将缺失开盘价或成交额伪装为真实值。 */
export function summarizeMinutes(data: NullableMinuteKlineRow[]): NullableMinuteKlineRow | null {
  if (!data.length) return null
  const highs = data.map(row => row.high).filter(isFiniteNumber)
  const lows = data.map(row => row.low).filter(isFiniteNumber)
  const hasCompleteVolume = data.every(row => isFiniteNumber(row.volume))
  const hasCompleteAmount = data.every(row => isFiniteNumber(row.amount))
  return {
    datetime: data[0].datetime,
    open: isFiniteNumber(data[0].open) ? data[0].open : null,
    high: highs.length > 0 ? Math.max(...highs) : null,
    low: lows.length > 0 ? Math.min(...lows) : null,
    close: isFiniteNumber(data[data.length - 1].close) ? data[data.length - 1].close : null,
    volume: hasCompleteVolume
      ? data.reduce((total, row) => total + row.volume!, 0)
      : null,
    amount: hasCompleteAmount
      ? data.reduce((total, row) => total + row.amount!, 0)
      : null,
  }
}

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

export function computeIntradayAverage(data: NullableMinuteKlineRow[]): (number | null)[] {
  const result: (number | null)[] = []
  let amount = 0
  let volume = 0
  let hasAmount = true
  for (const row of data) {
    if (isFiniteNumber(row.amount) && isFiniteNumber(row.volume) && row.volume >= 0) {
      amount += row.amount
      volume += row.volume * 100
    } else {
      hasAmount = false
    }
    result.push(hasAmount && volume > 0 ? amount / volume : null)
  }
  return result
}

function generateFullDayTimes(): string[] {
  const times: string[] = []
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  for (let hour = 13; hour <= 15; hour++) {
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = 0; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()
