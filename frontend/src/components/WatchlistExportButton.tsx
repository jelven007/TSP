import { useEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { api } from '@/lib/api'
import { accountScope } from '@/lib/accountSession'
import { downloadWatchlistCsv } from '@/lib/watchlistCsv'

export function WatchlistExportButton() {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const exportAll = async () => {
    if (pending) return
    const scope = accountScope()
    setPending(true)
    setError('')
    try {
      const { symbols } = await api.watchlistList()
      if (!mounted.current || scope !== accountScope()) return
      downloadWatchlistCsv(symbols, '全部自选股.csv')
    } catch (err) {
      if (mounted.current && scope === accountScope()) setError(err instanceof Error ? err.message : '导出失败')
    } finally {
      if (mounted.current && scope === accountScope()) setPending(false)
    }
  }

  return (
    <div className="relative">
      <button type="button" onClick={exportAll} disabled={pending}
        className="inline-flex items-center gap-1.5 h-8 px-2 rounded-btn bg-elevated text-secondary hover:text-foreground disabled:opacity-50"
        title="导出全部自选股为 CSV，仅包含股票代码，不受当前分组或筛选影响"
        aria-label="导出全部自选股">
        <Download className="h-4 w-4" />
        <span className="text-xs whitespace-nowrap">{pending ? '导出中…' : '导出全部'}</span>
      </button>
      {error && <span role="alert" className="absolute right-0 top-full z-20 mt-1 whitespace-nowrap rounded bg-surface border border-border p-2 text-xs text-danger">{error}</span>}
    </div>
  )
}
