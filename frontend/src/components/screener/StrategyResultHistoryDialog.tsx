import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { api, type StrategyResultHistoryFilters, type StrategyResultHistorySummary } from '@/lib/api'
import { hasPermission } from '@/lib/accountSession'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DatePicker } from '@/components/DatePicker'

type Props = {
  strategyId: string
  onClose: () => void
}

export function StrategyResultHistoryDialog(props: Props) {
  return <HistoryContent key={props.strategyId} {...props} />
}

function HistoryContent({ strategyId, onClose }: Props) {
  const client = useQueryClient()
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined])
  const [recordId, setRecordId] = useState('')
  const [draft, setDraft] = useState<StrategyResultHistoryFilters>({})
  const [filters, setFilters] = useState<StrategyResultHistoryFilters>({})
  const [filterError, setFilterError] = useState('')
  const [deleting, setDeleting] = useState<StrategyResultHistorySummary | null>(null)
  const [message, setMessage] = useState('')
  const canDelete = hasPermission('business:write')
  const cursor = cursors[cursors.length - 1]
  const history = useQuery({
    queryKey: QK.screenerHistory(strategyId, cursor, filters),
    queryFn: () => api.screenerResultHistory(strategyId, cursor, filters),
  })
  const detail = useQuery({
    queryKey: QK.screenerHistoryDetail(strategyId, recordId),
    queryFn: () => api.screenerResultHistoryDetail(strategyId, recordId),
    enabled: !!recordId,
  })
  const deletion = useMutation({
    mutationFn: (record: StrategyResultHistorySummary) => api.screenerResultHistoryDelete(strategyId, record.record_id),
    onSuccess: async (_result, record) => {
      const historyPrefix = QK.screenerHistory(strategyId).slice(0, 2)
      await client.cancelQueries({ queryKey: historyPrefix })
      await client.cancelQueries({ queryKey: QK.screenerHistoryDetail(strategyId, record.record_id) })
      client.setQueriesData<{ records: StrategyResultHistorySummary[]; next_cursor: string | null }>(
        { queryKey: historyPrefix },
        previous => previous && ({ ...previous, records: previous.records.filter(item => item.record_id !== record.record_id) }),
      )
      client.removeQueries({ queryKey: QK.screenerHistoryDetail(strategyId, record.record_id), exact: true })
      setRecordId(current => current === record.record_id ? '' : current)
      setCursors([undefined])
      setDeleting(null)
      setMessage('历史结果已删除')
      void client.invalidateQueries({ queryKey: historyPrefix })
      void client.invalidateQueries({ predicate: query => {
        const key = query.queryKey
        if (key[0] !== QK.screenerCached()[0]) return false
        const strategy = key[1] === 'strategy'
        const day = key[strategy ? 3 : 2]
        const asset = key[strategy ? 5 : key[1] === 'summary' ? 3 : 4]
        const timeframe = key[strategy ? 6 : key[1] === 'summary' ? 4 : 5]
        return (day === record.as_of || day === '') && asset === record.asset_type
          && (timeframe === record.timeframe || timeframe === 'all')
      } })
    },
  })
  const applyFilters = (next: StrategyResultHistoryFilters) => {
    if (next.start_date && next.end_date && next.start_date > next.end_date) {
      setFilterError('开始日期不能晚于结束日期')
      return
    }
    setFilterError('')
    setFilters(next)
    setCursors([undefined])
    setRecordId('')
    setMessage('')
  }
  const inputClass = 'w-full min-w-0 h-9 rounded-btn border border-border bg-elevated px-2 text-xs text-foreground'
  return (
    <Modal size="lg" onClose={onClose} closeDisabled={deletion.isPending} ariaLabel="策略历史结果">
      <div className="modal-header flex items-center justify-between gap-3">
        <h2 className="text-base font-medium text-foreground">策略历史结果</h2>
        <button className="modal-close" onClick={onClose} disabled={deletion.isPending} aria-label="关闭"><X className="h-4 w-4" /></button>
      </div>
      <div className="modal-body space-y-4">
        <p className="text-xs text-muted">按计算时的名称、版本、参数和日期查看保存结果。</p>
        <form className="grid grid-cols-2 gap-3 sm:grid-cols-4" onSubmit={event => {
          event.preventDefault()
          applyFilters(Object.fromEntries(Object.entries(draft).filter(([, value]) => value?.trim())
            .map(([key, value]) => [key, value!.trim()])))
        }}>
          <label className="col-span-2 space-y-1 text-xs text-muted">
            <span>名称或版本</span>
            <input aria-label="名称或版本" placeholder="输入历史名称或版本关键词" maxLength={200}
              className={inputClass} value={draft.q ?? ''}
              onChange={event => setDraft(current => ({ ...current, q: event.target.value }))} />
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span>资产类型</span>
            <select aria-label="资产类型" className={inputClass} value={draft.asset_type ?? ''}
              onChange={event => setDraft(current => ({ ...current, asset_type: event.target.value as StrategyResultHistoryFilters['asset_type'] }))}>
              <option value="">全部资产</option><option value="stock">股票</option><option value="etf">ETF</option>
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span>周期</span>
            <select aria-label="周期" className={inputClass} value={draft.timeframe ?? ''}
              onChange={event => setDraft(current => ({ ...current, timeframe: event.target.value as StrategyResultHistoryFilters['timeframe'] }))}>
              <option value="">全部周期</option><option value="1d">日线</option><option value="1m">分钟</option>
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span>开始日期</span>
            <DatePicker aria-label="开始日期" value={draft.start_date ?? ''} placeholder="不限日期" clearable
              className="w-full" buttonClassName="w-full justify-start" align="left"
              onChange={value => setDraft(current => ({ ...current, start_date: value }))} />
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span>结束日期</span>
            <DatePicker aria-label="结束日期" value={draft.end_date ?? ''} placeholder="不限日期" clearable
              className="w-full" buttonClassName="w-full justify-start"
              onChange={value => setDraft(current => ({ ...current, end_date: value }))} />
          </label>
          <div className="col-span-2 flex items-end gap-2">
            <button type="submit" className="h-9 rounded-btn bg-accent px-4 text-xs text-white">筛选</button>
            <button type="button" className="h-9 rounded-btn border border-border px-4 text-xs"
              onClick={() => { setDraft({}); applyFilters({}) }}>重置</button>
          </div>
        </form>
        {filterError && <p role="alert" className="text-xs text-danger">{filterError}</p>}
        {message && <p role="status" className="text-xs text-accent">{message}</p>}
        {history.isLoading && <p role="status">读取中…</p>}
        {history.isError && (
          <div role="alert" className="text-sm text-danger">
            历史结果读取失败：{history.error.message}
            <button className="ml-2 underline" onClick={() => history.refetch()}>重试</button>
          </div>
        )}
        {history.isSuccess && history.data.records.length === 0 && <p className="text-sm text-muted">
          {Object.keys(filters).length ? '没有符合筛选条件的结果' : '暂无保存结果'}
        </p>}
        {history.data?.records.map(record => (
          <div key={record.record_id} className="rounded-btn border border-border p-3 space-y-1">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm min-w-0 break-words">{record.strategy_name} · {record.as_of}</span>
              <div className="flex shrink-0 gap-3">
                <button className="text-xs text-accent" onClick={() => setRecordId(record.record_id)}>查看</button>
                <button className="text-xs text-danger disabled:opacity-40" disabled={!canDelete || deletion.isPending}
                  title={!canDelete ? '没有删除历史结果的权限' : '删除这条历史结果'}
                  onClick={() => { deletion.reset(); setDeleting(record) }}>删除</button>
              </div>
            </div>
            <p className="text-xs text-muted break-all">
              {record.asset_type === 'etf' ? 'ETF' : '股票'} · {record.timeframe === '1m' ? '分钟' : '日线'}
              {' · '}{record.total} 只 · 版本 {record.strategy_version.slice(0, 12)}
            </p>
            <p className="text-xs text-muted">计算于 {new Date(record.computed_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</p>
          </div>
        ))}
        {recordId && (
          <section className="border-t border-border pt-3 space-y-2">
            {detail.isLoading && <p role="status">读取明细中…</p>}
            {detail.isError && <p role="alert" className="text-danger">明细读取失败：{detail.error.message}</p>}
            {detail.data && (
              <>
                <h3 className="text-sm font-medium">{detail.data.summary.strategy_name} · {detail.data.summary.as_of}</h3>
                <pre className="text-xs whitespace-pre-wrap break-all bg-elevated p-2 rounded-btn">
                  {JSON.stringify({ 参数: detail.data.summary.params, 配置: detail.data.summary.overrides }, null, 2)}
                </pre>
                {detail.data.result.rows.length === 0 && <p className="text-xs text-muted">该次计算无命中</p>}
                <div className="max-h-64 overflow-auto">
                  <table className="w-full text-xs text-left">
                    <thead><tr><th className="p-2">资产</th><th className="p-2">名称</th><th className="p-2">评分</th></tr></thead>
                    <tbody>{detail.data.result.rows.map((row, index) => (
                      <tr key={`${row.symbol}-${index}`} className="border-t border-border">
                        <td className="p-2">{row.symbol}</td><td className="p-2">{row.name ?? '—'}</td>
                        <td className="p-2">{row.score ?? '—'}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </>
            )}
          </section>
        )}
      </div>
      <div className="modal-footer flex gap-3">
        <button className="text-xs disabled:opacity-40" disabled={cursors.length === 1 || history.isFetching || deletion.isPending}
          onClick={() => { setCursors(previous => previous.slice(0, -1)); setRecordId('') }}>上一页</button>
        <button className="text-xs disabled:opacity-40" disabled={!history.data?.next_cursor || history.isFetching || deletion.isPending}
          onClick={() => { setCursors(previous => [...previous, history.data!.next_cursor!]); setRecordId('') }}>下一页</button>
        <button className="ml-auto text-xs" disabled={deletion.isPending} onClick={onClose}>关闭</button>
      </div>
      {deleting && <ConfirmDialog title="删除历史结果" danger pending={deletion.isPending}
        confirmText="确认删除" onClose={() => { setDeleting(null); deletion.reset() }}
        onConfirm={() => deletion.mutate(deleting)}>
        <p>确定删除「{deleting.strategy_name}」在 {deleting.as_of} 的
          {deleting.asset_type === 'etf' ? 'ETF' : '股票'}{deleting.timeframe === '1m' ? '分钟' : '日线'}结果吗？
          删除后无法恢复；再次运行策略会保存新的结果。</p>
        {deletion.isError && <p role="alert" className="mt-2 text-danger">删除失败：{deletion.error.message}，请重试。</p>}
      </ConfirmDialog>}
    </Modal>
  )
}
