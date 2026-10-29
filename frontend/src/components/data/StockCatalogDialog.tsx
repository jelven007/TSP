import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, Loader2, Plus, RefreshCw } from 'lucide-react'
import { api, type StockCatalogFields, type StockCatalogRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { SettingsModal } from './SettingsModal'
import { DatePicker } from '@/components/DatePicker'

const inputClass = 'w-full rounded-btn border border-border bg-base px-3 py-2 text-xs text-foreground focus:outline-none focus:border-accent'
const buttonClass = 'rounded-btn border border-border px-3 py-2 text-xs hover:bg-elevated disabled:opacity-50 disabled:cursor-not-allowed'
const sourceLabel = (source: string | null) => ({ sse: '上交所', szse: '深交所', manual: '管理员' }[source ?? ''] ?? '历史快照')
const boardOptions = [
  { value: '沪主板', exchange: 'SH' },
  { value: '深主板', exchange: 'SZ' },
  { value: '创业板', exchange: 'SZ' },
  { value: '科创板', exchange: 'SH' },
] as const

type Draft = { symbol: string; name: string; listing_date: string; region: string; industry: string; total_shares: string; float_shares: string; tick_size: string }
const emptyDraft = (): Draft => ({
  symbol: '', name: '', listing_date: '', region: 'CN', industry: '', total_shares: '', float_shares: '', tick_size: '',
})
type Action = { kind: 'create' | 'update'; symbol: string; fields: StockCatalogFields } | { kind: 'delete'; symbol: string }

export function StockCatalogDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [exchange, setExchange] = useState('')
  const [board, setBoard] = useState('')
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [deleting, setDeleting] = useState<StockCatalogRow | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    if (search.trim() === query) return
    const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); setPageInput('') }, 250)
    return () => clearTimeout(timer)
  }, [search, query])
  const rows = useQuery({
    queryKey: QK.stockCatalog(query, exchange, page, board),
    queryFn: () => api.stockCatalog(query, exchange, page, 50, board),
    refetchInterval: 30_000,
  })
  const exportCsv = useMutation({
    mutationFn: () => api.exportStockCatalog(query, exchange, board),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'stock-catalog.csv'
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    },
    onError: (err: Error) => setError(err.message),
  })
  const availableBoards = boardOptions.filter(option => !exchange || option.exchange === exchange)
  function selectExchange(value: string) {
    setExchange(value)
    if (board && value && !boardOptions.some(option => option.value === board && option.exchange === value)) {
      setBoard('')
    }
    setPage(1)
    setPageInput('')
  }
  const totalPages = Math.max(1, Math.ceil((rows.data?.total ?? 0) / 50))
  const targetPage = Number(pageInput)
  const canJump = pageInput.trim() !== '' && Number.isInteger(targetPage)
    && targetPage >= 1 && targetPage <= totalPages
    && !rows.isFetching && (rows.data?.total ?? 0) > 0
  function jumpToPage() {
    if (canJump) {
      setPage(targetPage)
      setPageInput('')
    }
  }
  const change = useMutation<unknown, Error, Action>({
    mutationFn: (action: Action) => {
      if (action.kind === 'delete') return api.deleteStockCatalogRow(action.symbol)
      if (action.kind === 'create') return api.createStockCatalogRow({ ...action.fields, symbol: action.symbol })
      return api.updateStockCatalogRow(action.symbol, action.fields)
    },
    onSuccess: () => {
      setDraft(null); setEditing(null); setDeleting(null); setError('')
      for (const queryKey of [
        ['stock-catalog'], QK.dataStatus, ['instrument-search'], ['watchlist-enriched'],
        ['overview-market'], ['table-schema', 'instruments'],
      ]) void qc.invalidateQueries({ queryKey })
    },
    onError: (err: Error) => setError(err.message),
  })
  function openEditor(row?: StockCatalogRow) {
    setEditing(row?.symbol ?? null)
    setDeleting(null); setError('')
    setDraft(row ? {
      symbol: row.symbol, name: row.name, listing_date: row.listing_date ?? '', region: row.region ?? 'CN',
      industry: row.industry ?? '', total_shares: String(row.total_shares ?? ''),
      float_shares: String(row.float_shares ?? ''), tick_size: String(row.tick_size ?? ''),
    } : emptyDraft())
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    const numeric = (text: string) => text === '' ? null : Number(text)
    change.mutate({
      kind: editing ? 'update' : 'create', symbol: draft.symbol.trim().toUpperCase(),
      fields: {
        name: draft.name.trim(), listing_date: draft.listing_date || null, region: draft.region.trim() || 'CN',
        industry: draft.industry.trim() || null, total_shares: numeric(draft.total_shares),
        float_shares: numeric(draft.float_shares), tick_size: numeric(draft.tick_size),
      },
    })
  }
  return (
    <SettingsModal title="个股维表管理" size="xl" onClose={onClose} closeDisabled={change.isPending}>
      <div className="space-y-4">
        <p className="text-xs leading-relaxed text-secondary">
          来源为沪深交易所官方名单，系统启动时及每天北京时间 09:00 自动更新。管理员修改即时生效；
          下一次官方同步会重新核对股票名单、名称和上市日期，缺失的股本信息沿用已有值。
        </p>
        <div className="flex flex-wrap gap-2">
          <input aria-label="搜索股票代码或名称" placeholder="搜索代码或名称" value={search} onChange={e => setSearch(e.target.value)} className={`${inputClass} flex-1 min-w-40`} />
          <select aria-label="交易所筛选" value={exchange} onChange={e => selectExchange(e.target.value)} className={`${inputClass} !w-auto`}>
            <option value="">全部交易所</option><option value="SH">上海</option><option value="SZ">深圳</option>
          </select>
          <select aria-label="板块筛选" value={board} onChange={e => { setBoard(e.target.value); setPage(1); setPageInput('') }} className={`${inputClass} !w-auto`}>
            <option value="">全部板块</option>
            {availableBoards.map(({ value }) => <option key={value} value={value}>{value}</option>)}
          </select>
          <button className={buttonClass} onClick={() => { void rows.refetch() }} disabled={rows.isFetching} aria-label="刷新名单">
            <RefreshCw className={`h-4 w-4 ${rows.isFetching ? 'animate-spin' : ''}`} />
          </button>
          <button className={`${buttonClass} inline-flex items-center gap-1 text-accent`} disabled={change.isPending || rows.isError} onClick={() => openEditor()}>
            <Plus className="h-3.5 w-3.5" />新增股票
          </button>
          <button className={`${buttonClass} inline-flex items-center gap-1`} disabled={exportCsv.isPending || rows.isError}
            onClick={() => exportCsv.mutate()} title="导出全部符合当前筛选的股票">
            <Download className="h-3.5 w-3.5" />{exportCsv.isPending ? '导出中…' : '导出CSV'}
          </button>
        </div>
        {error && <p role="alert" className="text-xs text-bull">{error}</p>}
        {draft && (
          <form onSubmit={submit} className="rounded-card border border-accent/30 bg-base/40 p-4 space-y-3">
            <h4 className="text-sm font-medium">{editing ? '编辑股票' : '新增股票'}</h4>
            <fieldset disabled={change.isPending} className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              {([
                ['symbol', '股票代码', 'text'], ['name', '股票名称', 'text'], ['listing_date', '上市日期', 'date'],
                ['region', '地区', 'text'], ['industry', '行业', 'text'], ['total_shares', '总股本（股）', 'number'],
                ['float_shares', '流通股本（股）', 'number'], ['tick_size', '最小价格变动（元）', 'number'],
              ] as const).map(([key, label, type]) => (
                <label key={key} className="text-xs text-secondary space-y-1 block">
                  <span>{label}</span>
                  {type === 'date' ? (
                    <DatePicker aria-label={label} value={draft[key]} clearable placeholder="未记录"
                      className="w-full" buttonClassName="w-full justify-start" align="left" disabled={change.isPending}
                      onChange={value => setDraft({ ...draft, [key]: value })} />
                  ) : <input aria-label={label} className={inputClass} type={type} value={draft[key]}
                    required={key === 'symbol' || key === 'name'} disabled={key === 'symbol' && !!editing}
                    step={type === 'number' ? 'any' : undefined} min={type === 'number' ? '0.000001' : undefined}
                    maxLength={type === 'text' ? 100 : undefined} placeholder={key === 'symbol' ? '600000.SH / 000001.SZ' : undefined}
                    onChange={e => setDraft({ ...draft, [key]: e.target.value })} />}
                </label>
              ))}
            </fieldset>
            <div className="flex gap-2">
              <button type="submit" disabled={change.isPending} className={`${buttonClass} text-accent inline-flex gap-1 items-center`}>
                {change.isPending && <Loader2 className="h-3 w-3 animate-spin" />}保存
              </button>
              <button type="button" disabled={change.isPending} className={buttonClass} onClick={() => { setDraft(null); setError('') }}>取消编辑</button>
            </div>
          </form>
        )}
        {deleting && (
          <div role="alertdialog" aria-label="确认删除股票" className="rounded-card border border-bull/30 p-4 space-y-3">
            <p className="text-xs">确认删除 {deleting.name}（{deleting.symbol}）？历史行情仍保留。若它仍在官方名单中，下次同步会重新加入。</p>
            <div className="flex gap-2">
              <button disabled={change.isPending} className={`${buttonClass} text-bull`} onClick={() => change.mutate({ kind: 'delete', symbol: deleting.symbol })}>确认删除</button>
              <button disabled={change.isPending} className={buttonClass} onClick={() => { setDeleting(null); setError('') }}>取消删除</button>
            </div>
          </div>
        )}
        {rows.isError ? (
          <div role="alert" className="text-xs text-bull py-4">{rows.error.message}<button className={`${buttonClass} ml-2`} onClick={() => { void rows.refetch() }}>重试</button></div>
        ) : rows.isLoading ? (
          <p role="status" className="text-xs text-secondary py-6">正在加载股票名单…</p>
        ) : (
          <>
            <div className="overflow-x-auto rounded-btn border border-border">
              <table className="w-full min-w-[760px] text-xs text-left">
                <thead className="bg-elevated text-muted"><tr>{['代码', '名称', '板块', '上市日期', '总股本（股）', '流通股本（股）', '来源', '操作'].map(label => <th key={label} className="px-3 py-2 font-medium">{label}</th>)}</tr></thead>
                <tbody>
                  {rows.data?.items.map(row => (
                    <tr key={row.symbol} className="border-t border-border hover:bg-elevated/50">
                      <td className="px-3 py-2 font-mono">{row.symbol}</td><td className="px-3 py-2">{row.name}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{row.board || '—'}</td><td className="px-3 py-2 whitespace-nowrap font-mono">{row.listing_date || '—'}</td>
                      <td className="px-3 py-2 font-mono">{row.total_shares?.toLocaleString() ?? '—'}</td><td className="px-3 py-2 font-mono">{row.float_shares?.toLocaleString() ?? '—'}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{sourceLabel(row.snapshot_source)}</td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        <button aria-label={`编辑 ${row.symbol}`} disabled={change.isPending} className="text-accent mr-3" onClick={() => openEditor(row)}>编辑</button>
                        <button aria-label={`删除 ${row.symbol}`} disabled={change.isPending} className="text-bull" onClick={() => { setDeleting(row); setDraft(null); setError('') }}>删除</button>
                      </td>
                    </tr>
                  ))}
                  {rows.data?.items.length === 0 && <tr><td colSpan={8} className="px-3 py-8 text-center text-muted">没有匹配的股票</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center justify-between text-xs text-secondary gap-2">
              <span>共 {rows.data?.total ?? 0} 只 · 第 {page} / {totalPages} 页</span>
              <div className="flex flex-wrap items-center gap-2">
                <button className={buttonClass} disabled={page <= 1 || rows.isFetching} onClick={() => setPage(page - 1)}>上一页</button>
                <button className={buttonClass} disabled={page * 50 >= (rows.data?.total ?? 0) || rows.isFetching} onClick={() => setPage(page + 1)}>下一页</button>
                <label className="inline-flex items-center gap-2">
                  到
                  <input aria-label="跳转页码" type="number" inputMode="numeric" min={1} max={totalPages} step={1}
                    className={`${inputClass} !w-16`} value={pageInput} placeholder={String(page)}
                    disabled={rows.isFetching || (rows.data?.total ?? 0) === 0}
                    onChange={e => setPageInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); jumpToPage() } }} />
                  页
                </label>
                <button className={buttonClass} disabled={!canJump} onClick={jumpToPage}>跳转</button>
              </div>
            </div>
          </>
        )}
      </div>
    </SettingsModal>
  )
}
