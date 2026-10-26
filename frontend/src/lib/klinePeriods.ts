import type { KlinePeriod, PipelineJob } from './api'

export const KLINE_PERIOD_LABELS: Record<KlinePeriod, string> = {
  '1m': '1分K', '5m': '5分K', '10m': '10分K',
  '15m': '15分K', '1d': '日K', '1w': '周K',
}

export function klineJobPeriod(job: Pick<PipelineJob, 'kind' | 'result'>): KlinePeriod | null {
  if (job.kind === 'minute_sync') return '1m'
  const candidate = job.result?.period ?? job.kind?.replace(/^kline_/, '')
  return candidate && Object.hasOwn(KLINE_PERIOD_LABELS, candidate) ? candidate as KlinePeriod : null
}
