import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { api, type LotBatch } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import type { LotBatchStockDraft } from '@/lib/strategyLotDraft'
import { Modal } from './Modal'
import { DatePicker } from './DatePicker'
import { DateShortcuts } from './DateShortcuts'
import { LotExpiryPicker } from './LotExpiryPicker'

export interface LotBatchInitialDraft {
  stocks: LotBatchStockDraft[]
  target_pct: string
  stop_pct: string
  buy_date: string
}

/** 手动登记和策略结果共用表单；策略草稿仅在打开时初始化。 */
export function LotBatchDialog({ nextNumber, initial, onClose, onCreated }: {
  nextNumber?: string | null
  initial?: LotBatchInitialDraft
  onClose: () => void
  onCreated: (batch: LotBatch) => void
}) {
  const qc = useQueryClient()
  const requestId = useRef(crypto.randomUUID())
  const [name, setName] = useState('')
  const [query, setQuery] = useState('')
  const [stocks, setStocks] = useState<LotBatchStockDraft[]>(() => initial?.stocks ?? [])
  const [draft, setDraft] = useState(() => ({ buy_date: initial?.buy_date ?? '', remind_date: '' }))
  const [nums, setNums] = useState(() => ({
    target_pct: initial?.target_pct ?? '', stop_pct: initial?.stop_pct ?? '', lead_days: '1',
  }))
  const [error, setError] = useState('')
  const expiryEdited = useRef(false)
  const defaultExpiry = useQuery({
    queryKey: QK.lotExpiryOptions(initial?.buy_date ?? ''),
    queryFn: () => api.lotExpiryOptions(initial!.buy_date),
    enabled: !!initial,
    staleTime: 300000,
    retry: false,
  })
  useEffect(() => {
    if (defaultExpiry.data?.next_trading_day && !expiryEdited.current) {
      setDraft(current => ({ ...current, remind_date: defaultExpiry.data.next_trading_day! }))
    }
  }, [defaultExpiry.data])
  const search = useQuery({
    queryKey: QK.instrumentSearch(query, 'stock'),
    queryFn: () => api.instrumentSearch(query, 20, 'stock'),
    enabled: query.trim().length > 0,
  })
  const save = useMutation({
    mutationFn: api.lotBatchCreate,
    onSuccess: result => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.lotBatches })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      onCreated(result.batch)
    },
    onError: err => {
      setError(err instanceof Error ? err.message : String(err))
      qc.invalidateQueries({ queryKey: QK.lotBatches })
    },
  })
  const input = 'h-9 w-full min-w-0 rounded-btn border border-border bg-base px-3 text-xs text-foreground'
  const submit = () => {
    setError('')
    if (!stocks.length) return setError('请至少添加一只股票')
    if (initial && stocks.some(s => !s.qty.trim() || !s.cost.trim())) return setError('请补填每只股票的买入股数和买入均价')
    const target = Number(nums.target_pct), stop = Number(nums.stop_pct), lead = Number(nums.lead_days)
    if (![target, stop, lead].every(v => Number.isFinite(v) && v >= 0) || !Number.isInteger(lead)) {
      return setError('百分比须为非负数，提前天数须为非负整数')
    }
    if (!target && !stop && !draft.remind_date) return setError('止盈% / 止损% / 到期日 至少设置一项')
    if (stocks.some(s => !Number.isFinite(Number(s.cost)) || Number(s.cost) <= 0 ||
      !Number.isFinite(Number(s.qty)) || Number(s.qty) < 0)) return setError('每只股票的成本价须为正数，数量不能为负数')
    save.mutate({ name: name.trim(), request_id: requestId.current, stocks: stocks.map(s => ({
      symbol: s.symbol, qty: Number(s.qty), cost_price: Number(s.cost), target_pct: target, stop_pct: stop,
      buy_date: draft.buy_date || null, remind_date: draft.remind_date || null, lead_days: lead,
    })) })
  }
  return <Modal size="lg" ariaLabel="新增批次" onClose={onClose} closeDisabled={save.isPending}>
    <div className="modal-header flex items-center justify-between gap-2">
      <span className="text-sm font-medium">新增批次</span>
      <button onClick={onClose} disabled={save.isPending} aria-label="关闭新增批次" className="modal-close">✕</button>
    </div>
    <div className="space-y-4 px-4 py-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="space-y-1.5 text-xs text-muted">名称
          <input aria-label="批次名称" maxLength={80} value={name} onChange={e => setName(e.target.value)}
            placeholder="自定义名称，留空使用默认名称" className={input} autoFocus />
        </label>
        <label className="space-y-1.5 text-xs text-muted">批次号
          <input aria-label="批次号" readOnly value={nextNumber ?? (nextNumber === null ? '今日已达到99批次上限' : '读取中…')} className={input} />
        </label>
      </div>
      <p className="text-[11px] text-muted">批次号按北京时间创建日期 + 当日序号01–99生成，保存时确定最终编号。</p>
      {initial && <p className="text-xs text-muted">已带入当前页股票的买入股数、买入均价和策略止盈止损。缺少成交数据的股票请补填。
        持仓提醒按成本价比例触发，不含策略成交费用和时间兜底规则。</p>}
      <div className="space-y-2">
        <label className="block text-xs text-muted">添加股票 · 已选 {stocks.length} 只
          <input aria-label="搜索添加股票" value={query} onChange={e => setQuery(e.target.value)}
            placeholder="搜索代码或名称，逐只添加" className={cn(input, 'mt-1.5')} />
        </label>
        {query.trim() && <div className="max-h-40 overflow-y-auto rounded-btn border border-border">
          {search.isLoading && <p role="status" className="p-3 text-xs text-muted">搜索中…</p>}
          {search.isError && <p role="alert" className="p-3 text-xs text-danger">搜索失败，请重新输入</p>}
          {search.data?.results.map(r => <button key={r.symbol} disabled={stocks.some(s => s.symbol === r.symbol)}
            onClick={() => { setStocks(s => [...s, { symbol: r.symbol, name: r.name ?? '', qty: '', cost: '' }]); setQuery('') }}
            className="block w-full px-3 py-2 text-left text-xs hover:bg-elevated disabled:opacity-40">
            {r.symbol} {r.name} {stocks.some(s => s.symbol === r.symbol) && '已添加'}
          </button>)}
          {search.data?.results.length === 0 && <p className="p-3 text-xs text-muted">没有匹配股票</p>}
        </div>}
        <div className="space-y-2">
          {stocks.map((s, i) => <div key={s.symbol} className="grid grid-cols-2 sm:grid-cols-[1fr_120px_120px_32px] gap-2 items-end rounded-btn border border-border p-3">
            <div className="col-span-2 sm:col-span-1 text-xs"><span className="font-mono">{s.symbol}</span> {s.name}</div>
            {(['qty', 'cost'] as const).map(field => <label key={field} className="text-[11px] text-muted">
              {field === 'qty' ? '数量 (参考)' : '成本价'}
              <input aria-label={`${s.symbol} ${field === 'qty' ? '数量' : '成本价'}`} type="number" min={0} step="any"
                value={s[field]} onChange={e => setStocks(list => list.map((item, index) => index === i ? { ...item, [field]: e.target.value } : item))}
                className={cn(input, 'mt-1')} placeholder={field === 'qty' ? '0' : '必填'} />
            </label>)}
            <button aria-label={`移除股票 ${s.symbol}`} onClick={() => setStocks(list => list.filter(item => item.symbol !== s.symbol))}
              className="p-2 text-danger justify-self-end"><Trash2 className="h-4 w-4" /></button>
          </div>)}
        </div>
      </div>
      <p className="text-xs text-secondary">本批次股票共用以下提醒参数，保存后可逐只编辑。</p>
      <div className="grid grid-cols-2 gap-3">
        {(['target_pct', 'stop_pct'] as const).map(key => <label key={key} className="text-xs text-muted">
          {key === 'target_pct' ? '止盈%' : '止损%'}
          <input aria-label={key === 'target_pct' ? '批次止盈%' : '批次止损%'} type="number" min={0} step="any" value={nums[key]}
            onChange={e => setNums(s => ({ ...s, [key]: e.target.value }))} className={cn(input, 'mt-1.5')} placeholder="0" />
        </label>)}
        <div className="space-y-1.5 text-xs text-muted">买入日期 (可选)
          <DateShortcuts value={draft.buy_date} onChange={v => { expiryEdited.current = true; setDraft(s => ({ ...s, buy_date: v })) }} options={[{ label: '今天', days: 0 }]} />
          <DatePicker aria-label="批次买入日期" value={draft.buy_date} onChange={v => { expiryEdited.current = true; setDraft(s => ({ ...s, buy_date: v })) }} />
        </div>
        <div className="space-y-1.5 text-xs text-muted">到期日 (可选)
          <LotExpiryPicker ariaLabel="批次到期日期" buyDate={draft.buy_date}
            value={draft.remind_date} onChange={v => { expiryEdited.current = true; setDraft(s => ({ ...s, remind_date: v })) }} />
        </div>
      </div>
      {draft.remind_date && <label className="block text-xs text-muted">提前提醒天数
        <input aria-label="批次提前提醒天数" type="number" min={0} step={1} value={nums.lead_days}
          onChange={e => setNums(s => ({ ...s, lead_days: e.target.value }))} className={cn(input, 'mt-1.5')} />
        <span className="block mt-1 text-[11px]">提醒仅在交易时段评估，周末或长假请适当增加提前天数。</span>
      </label>}
      {error && <p role="alert" className="rounded-btn bg-danger/10 p-3 text-xs text-danger">{error}</p>}
    </div>
    <div className="modal-footer flex justify-end gap-2">
      <button onClick={onClose} disabled={save.isPending} className="h-9 rounded-btn border border-border px-3 text-xs">取消</button>
      <button onClick={submit} disabled={save.isPending || nextNumber == null || (!!initial && defaultExpiry.isPending && !expiryEdited.current)}
        className="h-9 rounded-btn bg-accent px-4 text-xs text-white disabled:opacity-50">{save.isPending ? '保存中…' : '保存批次'}</button>
    </div>
  </Modal>
}
