import type { WatchlistEntry } from '@/lib/api'

/** 下载当前清单的股票代码单列；首现去重并保留前导零和交易所后缀。 */
export function downloadWatchlistCsv(entries: WatchlistEntry[], filename: string) {
  const codes = [...new Set(entries.map(entry => entry.symbol))]
  if (!codes.length) throw new Error('没有股票可导出')
  const blob = new Blob(['\ufeff' + ['股票代码', ...codes].join('\r\n') + '\r\n'], {
    type: 'text/csv;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename.replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
