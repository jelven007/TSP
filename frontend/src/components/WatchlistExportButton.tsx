import { useEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { api } from '@/lib/api'
import { accountScope } from '@/lib/accountSession'
import { downloadWatchlistCsv } from '@/lib/watchlistCsv'
import type { WatchlistGroupFilter } from '@/components/WatchlistGroups'

export function WatchlistExportButton({ selectedGroup, groupName }: {
  selectedGroup: WatchlistGroupFilter
  groupName?: string
}) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const exportName = selectedGroup === 'all' ? '全部自选股'
    : selectedGroup === 'ungrouped' ? '未分组' : groupName || '当前分组'

  const exportSelected = async () => {
    if (pending) return
    const scope = accountScope()
    setPending(true)
    setError('')
    try {
      const { symbols } = await api.watchlistList()
      if (!mounted.current || scope !== accountScope()) return
      const entries = selectedGroup === 'all' ? symbols
        : symbols.filter(entry => selectedGroup === 'ungrouped'
          ? !entry.group_ids?.length
          : entry.group_ids?.includes(selectedGroup))
      downloadWatchlistCsv(entries, selectedGroup === 'all' ? '全部自选股.csv' : `${exportName}_股票列表.csv`)
    } catch (err) {
      if (mounted.current && scope === accountScope()) setError(err instanceof Error ? err.message : '导出失败')
    } finally {
      if (mounted.current && scope === accountScope()) setPending(false)
    }
  }

  return (
    <div className="relative">
      <button type="button" onClick={exportSelected} disabled={pending}
        className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated text-secondary hover:text-foreground disabled:opacity-50"
        title={pending ? '导出中…' : `导出${exportName}的股票代码为 CSV`}
        aria-label="导出当前分类股票代码"
        aria-busy={pending}>
        <Download className="h-4 w-4" aria-hidden="true" />
      </button>
      {error && <span role="alert" className="absolute right-0 top-full z-20 mt-1 whitespace-nowrap rounded bg-surface border border-border p-2 text-xs text-danger">{error}</span>}
    </div>
  )
}
