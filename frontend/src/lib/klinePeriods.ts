import type { IndexKlinePeriod, KlinePeriod, PipelineJob } from './api'

export const KLINE_PERIOD_LABELS: Record<KlinePeriod | IndexKlinePeriod, string> = {
  '1m': '1分K', '5m': '5分K', '10m': '10分K',
  '15m': '15分K', '1d': '日K', '1w': '周K', '1mo': '月K',
}

export const INDEX_KLINE_PERIODS: IndexKlinePeriod[] = ['1d', '1w', '1mo']

export function isLocalMinutePeriod(period: string): boolean {
  return period === '5m' || period === '10m' || period === '15m'
}

export function isLocalDerivedPeriod(period: string): boolean {
  return isLocalMinutePeriod(period) || period === '1w'
}

export function klineJobPeriod(job: Pick<PipelineJob, 'kind' | 'result'>): KlinePeriod | IndexKlinePeriod | null {
  if (job.kind === 'minute_sync') return '1m'
  const candidate = job.result?.period ?? job.kind?.replace(/^kline_/, '')
  return candidate && Object.hasOwn(KLINE_PERIOD_LABELS, candidate) ? candidate as KlinePeriod | IndexKlinePeriod : null
}
