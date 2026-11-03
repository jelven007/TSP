import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowUpRight, BellRing, CalendarClock, FolderOpen, Pencil, Plus, Search, Trash2 } from 'lucide-react'
import { api, type Lot, type LotBatch } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DatePicker } from '@/components/DatePicker'
import { DateShortcuts } from '@/components/DateShortcuts'
import { LotExpiryPicker } from '@/components/LotExpiryPicker'
import { LotBatchDialog } from '@/components/LotBatchDialog'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { EmptyState } from '@/components/EmptyState'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import type { ColumnConfig } from '@/lib/list-columns'
import { toNavItems } from '@/lib/listNav'
import { boardTag } from '@/components/stock-table/primitives'

const LOT_COLUMNS = ([
  { id: 'symbol', label: '代码/名称', align: 'left' },
  { id: 'qty', label: '数量(参考)', align: 'right' },
  { id: 'cost', label: '成本价', align: 'right' },
  { id: 'price', label: '现价', align: 'right' },
  { id: 'pnl', label: '盈亏%', align: 'right' },
  { id: 'target', label: '止盈%', align: 'right' },
  { id: 'stop', label: '止损%', align: 'right' },
  { id: 'buy_date', label: '买入日期', align: 'center' },
  { id: 'remaining', label: '剩余天数', align: 'right' },
  { id: 'remind_date', label: '到期提醒', align: 'left' },
] satisfies Pick<ColumnConfig, 'id' | 'label' | 'align'>[]).map<ColumnConfig>(column => ({
  ...column, visible: true, source: { type: 'computed', key: column.id },
}))

/** 剩余天数单元格: 到期日 − 今天, 可为负 = 已超期; 无到期日 → — */
function RemainingDays({ remind }: { remind?: string | null }) {
  if (!remind) return <span className="text-muted/60">—</span>
  const remindMs = new Date(`${remind}T00:00:00`).getTime()
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const n = Math.floor((remindMs - today.getTime()) / 86400000)
  if (n < 0) return <span className="font-mono text-warning">已超期{-n}天</span>
  return <span className="font-mono text-secondary">{n}天</span>
}

/** 成本 vs 现价的盈亏% (纯价格比例, 无数量参与) */
function CostPnL({ close, cost }: { close?: number; cost: number }) {
  if (close == null || !(cost > 0)) return <span className="text-muted/60">—</span>
  const pnl = (close - cost) / cost
  return <span className={cn('font-mono', priceColorClass(pnl))}>{fmtPct(pnl)}</span>
}

export function Lots() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<Lot | null>(null) // null=关闭
  const [creating, setCreating] = useState(false)
  const [activeBatch, setActiveBatch] = useState<string | null>(null)
  const [deletingBatch, setDeletingBatch] = useState<{ batch: LotBatch; count: number } | null>(null)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const lotsQuery = useQuery({ queryKey: QK.lots, queryFn: api.lotsList })
  const lots = useMemo(() => lotsQuery.data?.lots ?? [], [lotsQuery.data?.lots])
  const batchesQuery = useQuery({ queryKey: QK.lotBatches, queryFn: api.lotBatches })
  const batches = useMemo(() => {
    const rows = batchesQuery.data?.batches ?? []
    const legacy: LotBatch[] = lots.some(lot => !lot.batch_id)
      ? [{ id: 'legacy', name: '历史股票', number: '', created_at: '' }] : []
    return [...rows, ...legacy]
  }, [batchesQuery.data?.batches, lots])
  const selectedBatch = batches.some(b => b.id === activeBatch) ? activeBatch : batches[0]?.id
  const visibleLots = useMemo(() => lots.filter(lot =>
    selectedBatch === 'legacy' ? !lot.batch_id : lot.batch_id === selectedBatch), [lots, selectedBatch])
  const batchCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const lot of lots) {
      const id = lot.batch_id || 'legacy'
      counts.set(id, (counts.get(id) ?? 0) + 1)
    }
    return counts
  }, [lots])
  const currentBatch = batches.find(batch => batch.id === selectedBatch)

  const allSymbols = useMemo(() => Array.from(new Set(lots.map(l => l.symbol))), [lots])
  const namesQuery = useQuery({
    queryKey: ['instrument-names', allSymbols.join(',')],
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = useMemo(() => namesQuery.data?.names ?? {}, [namesQuery.data?.names])

  // 9999 哨兵: 未记买入日期的排最后
  const sortedLots = useMemo(() => {
    return [...visibleLots].sort((a, b) => (a.buy_date || '9999-12-31').localeCompare(b.buy_date || '9999-12-31'))
  }, [visibleLots])
  const lotsNavItems = useMemo(
    () => toNavItems(sortedLots.map(l => ({ symbol: l.symbol, name: symbolNames[l.symbol] }))),
    [sortedLots, symbolNames],
  )

  const dailyQuery = useQuery({
    queryKey: QK.lotsKline(allSymbols.join(',')),
    queryFn: () => api.klineDailyBatch(allSymbols, 5),
    enabled: allSymbols.length > 0,
    staleTime: 60000,
  })
  const lastPrices = useMemo(() => {
    const m: Record<string, number> = {}
    for (const [sym, rows] of Object.entries(dailyQuery.data?.data ?? {})) {
      const last = rows[rows.length - 1]
      if (last?.close != null) m[sym] = Number(last.close)
    }
    return m
  }, [dailyQuery.data])

  const del = useMutation({
    mutationFn: api.lotDelete,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      setConfirmId(null)
    },
  })

  const batchDel = useMutation({
    mutationFn: (id: string) => api.lotBatchDelete(id),
    onSuccess: async (_, id) => {
      await qc.cancelQueries({ queryKey: QK.lots })
      await qc.cancelQueries({ queryKey: QK.lotBatches })
      qc.setQueryData<Awaited<ReturnType<typeof api.lotsList>>>(QK.lots, old =>
        old && { ...old, lots: old.lots.filter(lot => lot.batch_id !== id) })
      qc.setQueryData<Awaited<ReturnType<typeof api.lotBatches>>>(QK.lotBatches, old =>
        old && { ...old, batches: old.batches.filter(batch => batch.id !== id) })
      setActiveBatch(null)
      setPreviewSymbol(null)
      setConfirmId(null)
      setDeletingBatch(null)
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.lotBatches })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
    },
  })

  // 删除: 第一次进确认态, 第二次真删, 3 秒后自动复位 (与监控中心一致)
  const handleClickDelete = (id: string) => {
    if (confirmId === id) {
      if (resetTimer.current) clearTimeout(resetTimer.current)
      setConfirmId(null)
      del.mutate(id)
    } else {
      setConfirmId(id)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setConfirmId(null), 3000)
    }
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="持仓"
        subtitle="按批次管理股票，自动生成止盈止损与到期提醒"
        titleExtra={
          <span className="inline-flex items-baseline gap-0.5 rounded-md bg-elevated/70 px-2 py-0.5 text-[11px]" title="当前批次记录 / 全部股票记录">
            <span className="font-mono font-semibold tabular-nums text-secondary">{visibleLots.length}</span>
            <span className="text-muted/50">/</span>
            <span className="font-mono tabular-nums text-muted">{lots.length}</span>
            <span className="ml-0.5 text-muted/60">条</span>
          </span>
        }
        right={
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => navigate('/monitor')}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-3 text-xs text-secondary transition-colors hover:bg-elevated/80 hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              title="查看自动生成的监控规则">
              <BellRing className="h-3.5 w-3.5" aria-hidden="true" />监控中心
              <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
            </button>
            <div className="h-5 w-px bg-border" aria-hidden="true" />
            <button type="button" onClick={() => setCreating(true)}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent/15 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/25 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />新增批次
            </button>
            {currentBatch?.number && (
              <button type="button"
                disabled={lotsQuery.isLoading || batchesQuery.isLoading || lotsQuery.isError || batchesQuery.isError || del.isPending || batchDel.isPending}
                onClick={() => {
                  batchDel.reset()
                  setDeletingBatch({ batch: currentBatch, count: visibleLots.length })
                }}
                title={`删除当前批次 ${currentBatch.name}（${currentBatch.number}）`}
                aria-label="删除批次"
                className="inline-flex h-8 w-8 items-center justify-center rounded-btn bg-danger/10 text-danger transition-colors duration-150 hover:bg-danger/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-danger disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-danger/10"
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>
        }
      />
      {batches.length > 0 && (
        <div className="flex h-10 shrink-0 items-stretch border-b border-border bg-surface/40 px-3 sm:px-5">
          <div role="tablist" aria-label="持仓批次" className="flex min-w-0 flex-1 items-stretch gap-1 overflow-x-auto">
            {batches.map(batch => {
              const active = selectedBatch === batch.id
              return (
                <button key={batch.id} type="button" role="tab" aria-selected={active}
                  aria-controls="holding-batch-panel" id={`holding-batch-${batch.id}`}
                  title={batch.number ? `${batch.name} · ${batch.number}` : batch.name}
                  onClick={() => setActiveBatch(batch.id)}
                  className={cn('my-1.5 inline-flex shrink-0 items-center gap-1.5 rounded-btn border px-3 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent',
                    active ? 'border-accent/40 bg-accent/10 text-accent' : 'border-transparent text-secondary hover:bg-elevated hover:text-foreground')}>
                  <span>{batch.name}</span>
                  <span className={cn('font-mono text-[10px] tabular-nums', active ? 'text-accent/80' : 'text-muted')}>
                    {batchCounts.get(batch.id) ?? 0}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      )}
      <div id="holding-batch-panel" role="tabpanel" aria-labelledby={selectedBatch ? `holding-batch-${selectedBatch}` : undefined}
        className="flex min-h-0 flex-1 flex-col gap-3 px-3 py-3 sm:px-5">
          {lotsQuery.isLoading || batchesQuery.isLoading ? (
            <div role="status" className="flex flex-1 items-center justify-center text-sm text-muted">加载中…</div>
          ) : lotsQuery.isError || batchesQuery.isError ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-3">
              <div role="alert" className="text-sm text-danger">批次加载失败</div>
              <button type="button"
                onClick={() => { lotsQuery.refetch(); batchesQuery.refetch() }}
                className="h-8 rounded-btn bg-elevated px-3 text-xs text-secondary transition-colors hover:text-foreground"
              >
                重试
              </button>
            </div>
          ) : visibleLots.length === 0 ? (
            <div className="min-h-0 flex-1 overflow-y-auto">
              <EmptyState icon={batches.length ? FolderOpen : BellRing}
                title={batches.length ? '当前批次没有股票' : '还没有批次'}
                hint="点击右上角「新增批次」录入股票、成本价和提醒条件，系统会自动同步价格与到期监控规则。" />
            </div>
          ) : (
            <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border bg-surface">
            <StockDataTable columns={LOT_COLUMNS} rows={sortedLots} rowKey={(lot: Lot) => lot.id}
              minWidth={1100} headerSticky extraHeader="操作"
              className="min-w-full"
              rowClassName={(lot: Lot) => cn('border-t border-border hover:bg-elevated/50', lot.symbol === previewSymbol && 'bg-accent/10 hover:bg-accent/15')}
              renderCell={(lot: Lot, column) => {
                const numericClass = 'whitespace-nowrap px-3 py-2.5 text-right font-mono text-xs tabular-nums'
                switch (column.id) {
                  case 'symbol': {
                    const board = boardTag(lot.symbol)
                    return (
                        <td className="px-3 py-2.5">
                          <button type="button"
                            onClick={() => setPreviewSymbol(lot.symbol)}
                            title={`查看 ${lot.symbol} 日K`}
                            className="inline-flex items-center gap-1.5 whitespace-nowrap text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                          >
                            <span className="font-mono text-xs text-foreground transition-colors group-hover:text-accent">{lot.symbol}</span>
                            {symbolNames[lot.symbol] && <span className="max-w-28 truncate text-xs text-secondary transition-colors group-hover:text-foreground">{symbolNames[lot.symbol]}</span>}
                            {board && <span className={`inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded border text-[9px] font-bold leading-none ${board.color}`}>{board.label}</span>}
                          </button>
                        </td>
                    )
                  }
                  case 'qty': return <td className={cn(numericClass, 'text-secondary')}>{lot.qty}</td>
                  case 'cost': return <td className={cn(numericClass, 'text-foreground')}>{fmtPrice(lot.cost_price)}</td>
                  case 'price': return <td className={cn(numericClass, 'text-secondary')}>{lastPrices[lot.symbol] != null ? fmtPrice(lastPrices[lot.symbol]) : '—'}</td>
                  case 'pnl': return <td className={numericClass}><CostPnL close={lastPrices[lot.symbol]} cost={lot.cost_price} /></td>
                  case 'target': return <td className={cn(numericClass, 'text-bull')}>{lot.target_pct > 0 ? `${lot.target_pct}%` : '—'}</td>
                  case 'stop': return <td className={cn(numericClass, 'text-bear')}>{lot.stop_pct > 0 ? `${lot.stop_pct}%` : '—'}</td>
                  case 'buy_date': return <td className="whitespace-nowrap px-3 py-2.5 text-center font-mono text-xs text-muted">{lot.buy_date || '—'}</td>
                  case 'remaining': return <td className={numericClass}><RemainingDays remind={lot.remind_date} /></td>
                  case 'remind_date': return (
                        <td className="whitespace-nowrap px-3 py-2.5 text-xs">
                          {lot.remind_date ? (
                            <span className="inline-flex items-center gap-1.5 text-secondary">
                              <CalendarClock className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                              <span className="font-mono tabular-nums">{lot.remind_date}</span>
                              {lot.lead_days > 0 && <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">提前{lot.lead_days}天</span>}
                            </span>
                          ) : <span className="text-muted/60">—</span>}
                        </td>
                  )
                  default: return null
                }
              }}
              renderExtraCol={(lot: Lot) => (
                        <td className="px-3 py-2.5">
                          <div className="flex items-center justify-end gap-1">
                            <button type="button"
                              onClick={() => setEditing(lot)}
                              title="编辑股票"
                              aria-label={`编辑股票 ${lot.symbol}`}
                              className="inline-flex h-7 w-7 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-accent/10 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                            >
                              <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                            </button>
                            {confirmId === lot.id ? (
                              <button type="button"
                                onClick={() => handleClickDelete(lot.id)}
                                title="确认删除股票"
                                className="inline-flex h-7 items-center gap-1 rounded-btn bg-danger/10 px-2 text-[11px] font-medium text-danger transition-colors hover:bg-danger/20"
                              >
                                <Trash2 className="h-3 w-3" aria-hidden="true" />确认
                              </button>
                            ) : (
                              <button type="button"
                                onClick={() => handleClickDelete(lot.id)}
                                title="删除股票 (同步删除生成的监控规则)"
                                aria-label={`删除股票 ${lot.symbol}`}
                                className="inline-flex h-7 w-7 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-danger/10 hover:text-danger focus-visible:outline focus-visible:outline-2 focus-visible:outline-danger"
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                              </button>
                            )}
                          </div>
                        </td>
              )}
            />
            </div>
          )}
        <p className="shrink-0 text-[11px] leading-relaxed text-muted">
          提醒规则同步至监控中心；现价取最近日K收盘价，盈亏为价格涨跌参考，不计费用。此页用于提醒，不作为持仓账本。
        </p>
      </div>

      {editing && <LotDialog lot={editing} onClose={() => setEditing(null)} />}
      {creating && <LotBatchDialog nextNumber={batchesQuery.data?.next_number}
        onClose={() => setCreating(false)} onCreated={batch => { setActiveBatch(batch.id); setCreating(false) }} />}

      {deletingBatch && (
        <ConfirmDialog title="删除批次" confirmText="确认删除批次" danger pending={batchDel.isPending}
          onClose={() => setDeletingBatch(null)} onConfirm={() => batchDel.mutate(deletingBatch.batch.id)}>
          <p>确定删除「{deletingBatch.batch.name}」<span className="font-mono">（{deletingBatch.batch.number}）</span>？</p>
          <p className="mt-2">将删除该批次的 {deletingBatch.count} 条股票记录，以及对应的止盈止损与到期提醒。此操作无法撤销。</p>
          {batchDel.isError && <p role="alert" className="mt-2 text-danger">
            {batchDel.error instanceof Error ? batchDel.error.message : '删除失败，请重试'}
          </p>}
        </ConfirmDialog>
      )}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol ? symbolNames[previewSymbol] : undefined}
        navList={lotsNavItems}
        onNavigate={(sym) => setPreviewSymbol(sym)}
        onClose={() => setPreviewSymbol(null)}
      />
    </div>
  )
}

function LotDialog({ lot, onClose }: { lot: Lot; onClose: () => void }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Lot>(() => ({ ...lot }))
  const [symbolQuery, setSymbolQuery] = useState('')
  const [error, setError] = useState('')

  // 数字字段用本地字符串承载 (可先清空再输入), 提交时才解析成数值;
  // 否则受控 number + parseFloat 会在清空瞬间把值塞回 0, 导致「0 去不掉」。
  const [nums, setNums] = useState<Record<string, string>>(() => {
    const f = (v: number | undefined | null) => (v == null || v === 0 ? '' : String(v))
    return {
      qty: f(lot.qty), cost_price: f(lot.cost_price),
      target_pct: f(lot.target_pct), stop_pct: f(lot.stop_pct),
      lead_days: f(lot.lead_days),
    }
  })
  const numField = (key: string) => ({
    value: nums[key] ?? '',
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setNums(s => ({ ...s, [key]: e.target.value })),
  })

  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbolQuery, 'stock'),
    queryFn: () => api.instrumentSearch(symbolQuery, 20, 'stock'),
    enabled: symbolQuery.trim().length > 0,
  })

  const save = useMutation({
    mutationFn: (vals: Partial<Lot>) => api.lotSave({ ...draft, ...vals, symbol: draft.symbol.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const parseNum = (s: string | undefined) => {
    const n = parseFloat(s ?? '')
    return Number.isFinite(n) ? n : 0
  }

  const submit = () => {
    setError('')
    if (!draft.symbol.trim()) return setError('请选择标的')
    const vals: Partial<Lot> = {
      qty: parseNum(nums.qty),
      cost_price: parseNum(nums.cost_price),
      target_pct: parseNum(nums.target_pct),
      stop_pct: parseNum(nums.stop_pct),
      lead_days: Math.floor(parseNum(nums.lead_days)),
    }
    if (!((vals.cost_price ?? 0) > 0)) return setError('成本价必须为正数')
    if ((vals.qty ?? 0) < 0 || (vals.target_pct ?? 0) < 0 || (vals.stop_pct ?? 0) < 0 || (vals.lead_days ?? 0) < 0) {
      return setError('数量 / 百分比 / 提前天数不能为负数')
    }
    if (!((vals.target_pct ?? 0) > 0 || (vals.stop_pct ?? 0) > 0 || draft.remind_date)) {
      return setError('止盈% / 止损% / 到期日 至少设置一项')
    }
    save.mutate(vals)
  }

  return (
    <Modal onClose={onClose} closeDisabled={save.isPending} ariaLabel="编辑股票" size="sm">
      <div className="modal-header flex items-center justify-between">
        <span className="text-sm font-medium text-foreground">编辑股票</span>
        <span className="text-[10px] text-muted">保存后自动同步监控规则</span>
      </div>
      <div className="space-y-3 px-4 py-4">
        {/* 标的 */}
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted">标的</span>
          {draft.symbol ? (
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded bg-elevated px-2 py-1 font-mono text-[11px] text-secondary">
                {draft.symbol}
                <button onClick={() => setDraft(d => ({ ...d, symbol: '' }))} className="text-muted hover:text-danger cursor-pointer"><span className="text-[10px]">✕</span></button>
              </span>
              <span className="text-[10px] text-muted">点 ✕ 可重选</span>
            </div>
          ) : (
            <div className="relative">
              <input
                value={symbolQuery}
                onChange={e => setSymbolQuery(e.target.value)}
                placeholder="搜索代码或名称..."
                autoFocus
                className="h-9 w-full rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground focus:outline-none focus:border-accent/50"
              />
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" />
              {symbolSearch.data && symbolSearch.data.results.length > 0 && (
                <div className="absolute z-10 mt-1 max-h-48 w-full overflow-auto rounded border border-border bg-surface shadow-lg">
                  {symbolSearch.data.results.map(r => (
                    <button
                      key={r.symbol}
                      onClick={() => { setDraft(d => ({ ...d, symbol: r.symbol })); setSymbolQuery('') }}
                      className="block w-full px-2.5 py-1.5 text-left text-[11px] hover:bg-elevated cursor-pointer"
                    >
                      <span className="font-mono text-foreground/80">{r.symbol}</span>
                      <span className="ml-1.5 text-muted">{r.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">数量 (参考)</span>
            <input type="number" min={0} placeholder="0" {...numField('qty')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">成本价</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('cost_price')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止盈%</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('target_pct')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止损%</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('stop_pct')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">买入日期 (可选)</span>
            <DateShortcuts value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} options={[{ label: '今天', days: 0 }]} />
            <DatePicker value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} placeholder="不记录" />
          </div>
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">到期日 (可选)</span>
            <LotExpiryPicker ariaLabel="股票到期日期" buyDate={draft.buy_date}
              value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} />
          </div>
        </div>

        {draft.remind_date && (
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">提前提醒天数</span>
            <input type="number" min={0} placeholder="1" {...numField('lead_days')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
            <span className="block text-[10px] text-muted">提醒仅在交易时段评估; 到期日若逢周末或长假, 请把提前天数调大些 (建议 ≥ 2, 长假更大)</span>
          </label>
        )}

        {error && <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] text-danger">{error}</div>}
      </div>
      <div className="modal-footer flex items-center justify-end gap-2">
        <button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated cursor-pointer">取消</button>
        <button
          onClick={submit}
          disabled={save.isPending}
          className={cn('h-9 rounded-btn px-4 text-xs font-medium bg-accent/90 text-white hover:bg-accent cursor-pointer disabled:opacity-50')}
        >
          {save.isPending ? '保存中...' : '保存'}
        </button>
      </div>
    </Modal>
  )
}
