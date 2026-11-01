import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type LotBatch } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnDateFromUtc } from '@/lib/format'
import { strategyLotRisk, strategyLotStocks } from '@/lib/strategyLotDraft'
import { LotBatchDialog } from '@/components/LotBatchDialog'
import { Modal } from '@/components/Modal'

export function StrategyLotBatchDialog({ strategyId, recordId, rows, onClose, onCreated }: {
  strategyId: string | null
  recordId?: string
  rows: Record<string, unknown>[]
  onClose: () => void
  onCreated: (batch: LotBatch) => void
}) {
  const [today] = useState(() => cnDateFromUtc(new Date().toISOString()))
  const batches = useQuery({ queryKey: QK.lotBatches, queryFn: api.lotBatches })
  const saved = useQuery({
    queryKey: QK.screenerHistoryDetail(strategyId ?? '', recordId ?? ''),
    queryFn: () => api.screenerResultHistoryDetail(strategyId!, recordId!),
    enabled: !!strategyId && !!recordId,
    retry: false,
  })
  const current = useQuery({
    queryKey: QK.strategyDetail(strategyId ?? ''),
    queryFn: () => api.strategyGet(strategyId!),
    enabled: !!strategyId && !recordId,
    retry: false,
  })
  const risk = recordId ? saved : current
  if (batches.isSuccess && (!strategyId || risk.isSuccess)) {
    const source = recordId ? saved.data?.summary : current.data
    const initial = { stocks: strategyLotStocks(rows), buy_date: today,
      ...(source ? strategyLotRisk(source) : { target_pct: '', stop_pct: '' }) }
    return <LotBatchDialog nextNumber={batches.data.next_number} initial={initial} onClose={onClose} onCreated={onCreated} />
  }
  const error = batches.error ?? risk.error
  return <Modal size="lg" ariaLabel="新增批次" onClose={onClose}>
    <div className="modal-header flex items-center justify-between">
      <span className="text-sm font-medium">新增批次</span>
      <button onClick={onClose} aria-label="关闭新增批次" className="modal-close">✕</button>
    </div>
    <div className="p-4">
      {error ? <>
        <p role="alert" className="text-xs text-danger">{error instanceof Error ? error.message : '批次参数加载失败'}</p>
        <button onClick={() => { if (batches.isError) void batches.refetch(); if (risk.isError) void risk.refetch() }}
          className="mt-3 rounded-btn border border-border px-3 py-2 text-xs">重试</button>
      </> : <p role="status" className="text-xs text-muted">正在读取批次号和策略参数…</p>}
    </div>
  </Modal>
}
