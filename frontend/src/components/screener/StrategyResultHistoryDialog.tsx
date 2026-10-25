import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'

export function StrategyResultHistoryDialog({ strategyId, onClose }: {
  strategyId: string
  onClose: () => void
}) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined])
  const [recordId, setRecordId] = useState('')
  const cursor = cursors[cursors.length - 1]
  const history = useQuery({
    queryKey: QK.screenerHistory(strategyId, cursor),
    queryFn: () => api.screenerResultHistory(strategyId, cursor),
  })
  const detail = useQuery({
    queryKey: QK.screenerHistoryDetail(strategyId, recordId),
    queryFn: () => api.screenerResultHistoryDetail(strategyId, recordId),
    enabled: !!recordId,
  })
  return (
    <Modal size="lg" onClose={onClose} ariaLabel="策略历史结果">
      <div className="modal-header flex items-center justify-between gap-3">
        <h2 className="text-base font-medium text-foreground">策略历史结果</h2>
        <button className="modal-close" onClick={onClose} aria-label="关闭"><X className="h-4 w-4" /></button>
      </div>
      <div className="modal-body space-y-4">
        <p className="text-xs text-muted">按计算时的名称、版本、参数和日期查看保存结果。</p>
        {history.isLoading && <p role="status">读取中…</p>}
        {history.isError && (
          <div role="alert" className="text-sm text-danger">
            历史结果读取失败：{history.error.message}
            <button className="ml-2 underline" onClick={() => history.refetch()}>重试</button>
          </div>
        )}
        {history.isSuccess && history.data.records.length === 0 && <p className="text-sm text-muted">暂无保存结果</p>}
        {history.data?.records.map(record => (
          <div key={record.record_id} className="rounded-btn border border-border p-3 space-y-1">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm">{record.strategy_name} · {record.as_of}</span>
              <button className="text-xs text-accent shrink-0" onClick={() => setRecordId(record.record_id)}>查看</button>
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
        <button className="text-xs disabled:opacity-40" disabled={cursors.length === 1}
          onClick={() => { setCursors(previous => previous.slice(0, -1)); setRecordId('') }}>上一页</button>
        <button className="text-xs disabled:opacity-40" disabled={!history.data?.next_cursor}
          onClick={() => { setCursors(previous => [...previous, history.data!.next_cursor!]); setRecordId('') }}>下一页</button>
        <button className="ml-auto text-xs" onClick={onClose}>关闭</button>
      </div>
    </Modal>
  )
}
