/**
 * 模拟交易 (虚拟账户) — 多账户对比主视图 + 行展开账户操作。
 *
 * 对比 (默认): 全账户收益降序榜单 (同本金同费率口径), 归一化净值叠加,
 * 点行展开即完整账户操作 (下单/跟单规则/费用/导出), 无需跳转页面;
 * 批量创建一次构造一批同规格账户各绑一条跟单规则。
 *
 * 口径提示: 持仓现价用最近日线收盘价 (收盘定版一致, 盘中估算见设计方案
 * docs/reference/trading-ai.md#paper)。费用/滑点参数与回测引擎同名同默认值。
 * 多账户: 所有查询按账户隔离 (queryKey 前缀 'paper'), 切换即换一套数据。
 */
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { accountStorage as localStorage } from '@/lib/accountSession'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from '@/lib/echarts'
import { Banknote, ChevronDown, ChevronUp, CircleDollarSign, Clock, Download, GitCompare, Info, PieChart, Plus, RefreshCw, Settings, Trash2, TrendingUp, Wallet, X } from 'lucide-react'
import { api, type PaperAutoRule, type PaperAutoSkip, type PaperCompareRow, type PaperFees, type PaperFill, type PaperOrder, type PaperValuation, type StrategyDetail } from '@/lib/api'
import { storage } from '@/lib/storage'
import { backtestRuleDraft, equalEquityPct } from '@/lib/paperRuleDraft'
import { QK } from '@/lib/queryKeys'
import { paperAccountChanged } from '@/lib/paperAccountQueries'
import { useQuoteStatus } from '@/lib/useSharedQueries'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { toast } from '@/components/Toast'
import { PaperCorporateActions } from '@/components/PaperCorporateActions'

const ORDER_TYPE_LABEL: Record<string, string> = {
  market: '即时',
  next_open: '次日开盘',
  close: '当日收盘',
}

type AutoRuleSide = 'auto' | 'buy' | 'sell'
const AUTO_SIDE_LABEL: Record<AutoRuleSide, string> = { auto: '自动', buy: '买入', sell: '卖出' }

function isNonnegativeInteger(value: string) {
  return value.trim() !== '' && Number.isSafeInteger(Number(value)) && Number(value) >= 0
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待成交',
  filled: '已成交',
  cancelled: '已撤单',
  expired: '已过期',
}

function fmtMoney(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function downloadTradesCsv(account: string, fills: PaperFill[]) {
  const esc = (value: string | number | null | undefined) => {
    const text = value == null ? '' : String(value)
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }
  const lines = ['日期,代码,方向,数量,成交价,费用,类型,订单号,公司行为,现金金额']
  for (const fill of fills) {
    lines.push([
      fill.date, fill.symbol,
      fill.kind === 'corporate_action' ? fill.label : fill.kind === 'corp_action' ? '除权' : fill.side === 'buy' ? '买入' : '卖出',
      fill.qty ?? '', fill.price ?? '', fill.fee ?? '',
      fill.kind ?? 'fill', fill.order_id ?? '', fill.action_id ?? '', fill.cash_amount ?? '',
    ].map(esc).join(','))
  }
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `模拟交易成交台账_${account}_${new Date().toISOString().slice(0, 10)}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
}

/** 账户 id: 字母数字开头短横线/下划线, 与后端校验一致 */
function genAccountId() {
  return `acc_${Date.now().toString(36)}${Math.floor(Math.random() * 36).toString(36)}`
}

/** 归一化到首日=100 (基准对比); 空洞(缺行情日)保留 null 不连线 */
function normalizeBase(values: Array<number | null | undefined>): Array<number | null> {
  const first = values.find((v): v is number => v != null && v > 0)
  if (!first) return values.map(() => null)
  return values.map(v => (v != null && v > 0 ? Number(((v / first) * 100).toFixed(2)) : null))
}

const VALUATION_BASIS_LABEL: Record<PaperValuation['basis'], string> = {
  live: '盘中实时估算',
  daily_close: '最近日线收盘估算',
  cost: '成本占位 (缺行情)',
}

/** 估值口径徽章: 标注估算口径、数据时间与覆盖, 区别于按收盘定版的净值曲线。 */
function ValuationBadge({ v }: { v: PaperValuation }) {
  const tone = v.basis === 'live' ? 'text-accent border-accent/30 bg-accent/5'
    : v.basis === 'daily_close' ? 'text-muted border-border bg-elevated/40'
    : 'text-warning border-warning/30 bg-warning/5'
  const ts = v.quote_ts ? new Date(v.quote_ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : null
  return (
    <div className={cn('mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-btn border px-2.5 py-1 text-[11px]', tone)}>
      <span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" />{VALUATION_BASIS_LABEL[v.basis]}</span>
      <span className="text-muted">估值, 非收盘定版净值</span>
      {ts && <span className="font-mono text-muted">数据时间 {ts}</span>}
      <span className="text-muted">覆盖 {v.covered}/{v.total}</span>
      {v.missing.length > 0 && (
        <span className="text-warning" title={`以下持仓缺当前行情, 按成本占位: ${v.missing.join('、')}`}>
          {v.missing.length} 只缺价按成本计
        </span>
      )}
    </div>
  )
}

/** 净值曲线 (echarts line, 跟随主题色): 账户归一化 vs 沪深300 归一化 (首日=100) */
function NavChart({ nav }: { nav: Array<{ date: string; nav: number; benchmark_close?: number }> }) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!elRef.current) return
    chartRef.current = echarts.init(elRef.current, undefined, { renderer: 'canvas' })
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    // 跟随亮暗主题切换重设坐标轴配色
    const observer = new MutationObserver(() => {
      const darkNow = document.documentElement.classList.contains('dark')
      const axisColor = darkNow ? '#8E8E96' : '#52525B'
      const splitColor = darkNow ? '#353539' : '#E4E4E7'
      chartRef.current?.setOption({
        xAxis: { axisLine: { lineStyle: { color: splitColor } }, axisLabel: { color: axisColor } },
        yAxis: { axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: splitColor } } },
      })
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => {
      window.removeEventListener('resize', onResize)
      observer.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const dark = document.documentElement.classList.contains('dark')
    const axisColor = dark ? '#8E8E96' : '#52525B'
    const splitColor = dark ? '#353539' : '#E4E4E7'
    chart.setOption({
      grid: { left: 56, right: 16, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => fmtMoney(v),
      },
      xAxis: {
        type: 'category',
        data: nav.map(n => n.date),
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: axisColor, fontSize: 10, formatter: (v: number) => fmtMoney(v, 0) },
        splitLine: { lineStyle: { color: splitColor } },
      },
      series: [
        {
          name: '虚拟账户',
          type: 'line',
          data: normalizeBase(nav.map(n => n.nav)),
          showSymbol: false,
          lineStyle: { color: '#8B5CF6', width: 2 },
          areaStyle: { color: 'rgba(139, 92, 246, 0.08)' },
        },
        ...(nav.some(n => n.benchmark_close) ? [{
          name: '沪深300',
          type: 'line' as const,
          data: normalizeBase(nav.map(n => n.benchmark_close)),
          showSymbol: false,
          lineStyle: { color: dark ? '#8E8E96' : '#52525B', width: 1.5, type: 'dashed' as const },
        }] : []),
      ],
      legend: {
        top: 0,
        right: 0,
        textStyle: { color: axisColor, fontSize: 10 },
        itemWidth: 14,
      },
    })
  }, [nav])

  return <div ref={elRef} className="h-48 w-full" />
}

function StatCard({ label, value, sub, valueClass, icon: Icon, iconCls }: {
  label: string; value: string; sub?: string; valueClass?: string
  icon?: React.ComponentType<{ className?: string }>; iconCls?: string
}) {
  return (
    <div className="rounded-card border border-border bg-surface px-4 py-3 transition-colors duration-150 hover:border-accent/25 hover:bg-elevated/30">
      <div className="flex items-center gap-1.5 text-[11px] text-muted">
        {Icon && <Icon className={cn('h-3.5 w-3.5', iconCls)} />}
        {label}
      </div>
      <div className={cn('mt-1 font-mono text-xl font-semibold tabular', valueClass)}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

/** 新建账户默认值 (localStorage): 上次创建用的初始资金/费用, 作为下次预填默认。 */
const NEW_ACCOUNT_DEFAULTS_KEY = 'paper.newAccountDefaults'
type NewAccountDefaults = {
  cash?: string; commissionWan?: string; stampQian?: string; slippageBps?: string
}

function loadNewAccountDefaults(): Required<NewAccountDefaults> {
  let saved: NewAccountDefaults = {}
  try { saved = JSON.parse(localStorage.getItem(NEW_ACCOUNT_DEFAULTS_KEY) || '{}') } catch { /* 忽略坏数据 */ }
  return {
    cash: saved.cash ?? '1000000',
    commissionWan: saved.commissionWan ?? '2.5',   // 万2.5
    stampQian: saved.stampQian ?? '1',             // 千1 (仅卖出)
    slippageBps: saved.slippageBps ?? '5',         // 5bps
  }
}

/** 初始化向导: 选中账户不存在或正在新建时展示。
 * onCancel 由外层按「是否已有其他账户」传入 —— 一个账户都没有时必须先建一个, 不给取消。
 * 费用三参数可设置, 且记住为下次新建的默认值。 */
function SetupCard({ accId, onDone, onCancel }: { accId: string; onDone: (createdId: string) => void; onCancel?: () => void }) {
  const defaults = loadNewAccountDefaults()
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaults.stampQian)
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [name, setName] = useState('')
  const m = useMutation({
    mutationFn: () =>
      api.paperCreateAccount({
        initial_cash: Number(cash),
        account_id: accId,
        name: name.trim() || undefined,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
      }),
    onSuccess: r => {
      // 记住本次取值, 作为下次新建账户的默认
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({
          cash, commissionWan, stampQian, slippageBps,
        }))
      } catch { /* 存储不可用时静默 */ }
      onDone(r.account.id)
    },
  })
  const valid = Number(cash) > 0
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0
  return (
    <div className="page-card w-full p-4">
      <div className="flex items-center gap-2">
        <CircleDollarSign className="h-5 w-5 text-accent" />
        <h2 className="text-[16px] leading-6 font-semibold">创建虚拟账户</h2>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        虚拟资金 + 真实行情价格模拟撮合, 不涉及任何真实资金。费用口径与回测一致,
        创建后仍可在「费用」里调整; 本次取值会记住, 作为下次新建的默认。
      </p>
      <label className="mt-4 block text-xs text-secondary">账户名称 (可选)</label>
      <input
        value={name}
        onChange={e => setName(e.target.value)}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent/50"
        placeholder={accId === 'default' ? '默认账户' : `账户 ${accId}`}
      />
      <label className="mt-3 block text-xs text-secondary">初始虚拟资金 (元)</label>
      <input
        type="number"
        value={cash}
        onChange={e => setCash(e.target.value)}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 font-mono text-sm outline-none focus:border-accent/50"
        placeholder="1000000"
      />
      <div className="mt-3 grid grid-cols-3 gap-2">
        <div>
          <label className="block text-[11px] text-muted">佣金 (万)</label>
          <input type="number" step="0.1" min="0" value={commissionWan} onChange={e => setCommissionWan(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="block text-[11px] text-muted">印花税 (千)</label>
          <input type="number" step="0.1" min="0" value={stampQian} onChange={e => setStampQian(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="block text-[11px] text-muted">滑点 (bps)</label>
          <input type="number" step="1" min="0" value={slippageBps} onChange={e => setSlippageBps(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
      </div>
      <div className="mt-4 flex gap-2">
        <button
          onClick={() => valid && m.mutate()}
          disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
        >
          {m.isPending ? '创建中…' : '创建账户'}
        </button>
        {onCancel && (
          <button
            onClick={onCancel}
            disabled={m.isPending}
            className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
            title="放弃创建, 返回原账户"
          >
            取消
          </button>
        )}
      </div>
      {m.isError && <div className="mt-2 text-xs text-danger">{String((m.error as Error).message)}</div>}
    </div>
  )
}

// ================================================================
// 通用联想输入 (紧凑版): 受控输入 + 选项下拉 + 键盘导航 + 外点关闭。
// 数据获取由父组件负责 (instrumentSearch 异步搜索 / 策略列表本地过滤),
// 本组件只管交互与渲染; 模式对齐 Watchlist.StockSearchBox 一族搜索框。
// ================================================================
interface SuggestOption {
  /** 选中后写入输入框的值 (完整代码 / 策略 id) */
  value: string
  /** 主文本 (截断) */
  label: string
  /** 可选等宽左槽 (股票代码) */
  left?: string
  /** 可选右槽 (板性徽标 / 来源等) */
  hint?: ReactNode
}

function SuggestInput({
  value,
  onChange,
  options,
  onSelect,
  placeholder,
  className,
  loading,
}: {
  value: string
  onChange: (v: string) => void
  options: SuggestOption[]
  onSelect: (opt: SuggestOption) => void
  placeholder?: string
  className?: string
  loading?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function handlePick(opt: SuggestOption) {
    onSelect(opt)
    setOpen(false)
    setActiveIdx(-1)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || options.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, options.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const pick = options[activeIdx >= 0 ? activeIdx : 0]
      if (pick) handlePick(pick)
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <input
        type="text"
        value={value}
        onChange={e => { onChange(e.target.value); setOpen(true); setActiveIdx(-1) }}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className={className}
      />
      {open && (loading || options.length > 0) && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 max-h-56 overflow-y-auto rounded-card border border-border bg-base shadow-xl">
          {loading && options.length === 0 ? (
            <div className="px-3 py-3 text-center text-[11px] text-muted">搜索中…</div>
          ) : (
            options.map((opt, i) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => handlePick(opt)}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors duration-100',
                  i === activeIdx ? 'bg-accent/10 text-accent' : 'hover:bg-elevated text-foreground',
                )}
              >
                {opt.left && <span className="shrink-0 font-mono text-[11px]">{opt.left}</span>}
                <span className="min-w-0 flex-1 truncate">{opt.label}</span>
                {opt.hint}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}

/** 下单面板 */
function OrderForm({ acc, onDone }: { acc: string; onDone: () => void }) {
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('100')
  const [amount, setAmount] = useState('10000')
  const [qtyMode, setQtyMode] = useState<'qty' | 'amount'>('qty')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('market')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const pendingRequest = useRef<{ signature: string; id: string } | null>(null)

  const m = useMutation({
    mutationFn: (body: Parameters<typeof api.paperOrderCreate>[0]) => api.paperOrderCreate(body, acc),
    onSuccess: r => {
      pendingRequest.current = null
      setMsg({ ok: true, text: `已提交 ${ORDER_TYPE_LABEL[r.order.order_type]}单 ${r.order.side === 'buy' ? '买入' : '卖出'} ${r.order.symbol} x ${r.order.qty}` })
      onDone()
    },
    onError: e => setMsg({ ok: false, text: String((e as Error).message) }),
  })

  const canSubmit = symbol.trim().length >= 6
    && (qtyMode === 'qty' ? Number(qty) > 0 && Number(qty) % 100 === 0 : Number(amount) > 0)

  const submitOrder = () => {
    if (!canSubmit || m.isPending) return
    const body = {
      symbol: symbol.trim().toUpperCase(), side,
      ...(qtyMode === 'qty' ? { qty: Number(qty) } : { amount: Number(amount) }),
      order_type: orderType,
    }
    const signature = JSON.stringify([acc, body])
    if (pendingRequest.current?.signature !== signature) {
      pendingRequest.current = { signature, id: crypto.randomUUID() }
    }
    m.mutate({ ...body, request_id: pendingRequest.current.id })
  }

  // 标的联想: 代码 / 中文名 / 拼音首字母。
  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbol.trim(), 'stock'),
    queryFn: () => api.instrumentSearch(symbol.trim(), 20, 'stock'),
    enabled: symbol.trim().length > 0,
    staleTime: 30_000,
  })
  const symbolOptions: SuggestOption[] = (symbolSearch.data?.results ?? []).map(r => {
    const b = boardTag(r.symbol)
    return {
      value: r.symbol,
      label: r.name || r.code || r.symbol,
      left: r.symbol,
      hint: b ? <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] leading-none', b.color)}>{b.label}</span> : null,
    }
  })

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-sm font-medium">下单</div>
      <div className="mt-3 space-y-3">
        <div>
          <label className="text-[11px] text-muted">代码 (代码 / 中文名 / 拼音首字母联想)</label>
          <SuggestInput
            value={symbol}
            onChange={setSymbol}
            options={symbolOptions}
            onSelect={opt => setSymbol(opt.value)}
            loading={symbolSearch.isFetching}
            placeholder="600519.SH / 茅台 / MZ"
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm uppercase outline-none focus:border-accent/50"
          />
        </div>
        <div className="flex gap-2">
          {(['buy', 'sell'] as const).map(s => (
            <button
              key={s}
              onClick={() => setSide(s)}
              className={cn(
                'flex-1 rounded-btn border py-1.5 text-xs font-medium transition-all duration-150 ease-smooth',
                side === s
                  ? s === 'buy'
                    ? 'border-bull/50 bg-bull/10 text-bull shadow-[0_0_0_1px_rgba(240,68,56,0.08)]'
                    : 'border-bear/50 bg-bear/10 text-bear shadow-[0_0_0_1px_rgba(18,183,106,0.08)]'
                  : 'border-border text-muted hover:border-border hover:bg-elevated/60 hover:text-secondary',
              )}
            >
              {s === 'buy' ? '买入' : '卖出'}
            </button>
          ))}
        </div>
        <div>
          <div className="flex items-center justify-between">
            <label className="text-[11px] text-muted">{qtyMode === 'qty' ? '数量 (股, 100 的整数倍)' : '金额 (元, 按最近收盘价折算)'}</label>
            <div className="flex gap-1 text-[10px]">
              {(['qty', 'amount'] as const).map(mo => (
                <button
                  key={mo}
                  onClick={() => setQtyMode(mo)}
                  className={cn('rounded px-1.5 py-px transition-colors', qtyMode === mo ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                >
                  {mo === 'qty' ? '按数量' : '按金额'}
                </button>
              ))}
            </div>
          </div>
          <input
            type="number"
            value={qtyMode === 'qty' ? qty : amount}
            onChange={e => (qtyMode === 'qty' ? setQty(e.target.value) : setAmount(e.target.value))}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50"
          />
        </div>
        <div>
          <label className="text-[11px] text-muted">订单类型</label>
          <div className="mt-1 flex gap-1.5">
            {(Object.keys(ORDER_TYPE_LABEL) as Array<'market' | 'next_open' | 'close'>).map(t => (
              <button
                key={t}
                onClick={() => setOrderType(t)}
                title={t === 'market' ? '盘中按最新快照价成交' : undefined}
                className={cn(
                  'flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary',
                )}
              >
                {ORDER_TYPE_LABEL[t]}
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={submitOrder}
          disabled={!canSubmit || m.isPending}
          className={cn(
            'w-full rounded-btn py-2 text-sm font-medium text-white transition-all duration-150 ease-smooth disabled:opacity-40 disabled:cursor-not-allowed',
            side === 'buy' ? 'bg-bull hover:bg-bull/85' : 'bg-bear hover:bg-bear/85',
          )}
        >
          {m.isPending ? '提交中…' : `${side === 'buy' ? '买入' : '卖出'} ${symbol.trim().toUpperCase() || ''}`}
        </button>
        {msg && (
          <div className={cn(
            'rounded-btn border px-2 py-1.5 text-[11px]',
            msg.ok
              ? side === 'buy' ? 'border-bull/20 bg-bull/[0.06] text-bull' : 'border-bear/20 bg-bear/[0.06] text-bear'
              : 'border-danger/20 bg-danger/[0.06] text-danger',
          )}>
            {msg.text}
          </div>
        )}
      </div>
    </div>
  )
}

/** 策略候选对比 (V3): 模拟交易统计 vs 回测候选 (回测报告的持久化标量摘要, 口径与回测对齐)。
 * 单位口径: 候选 metrics 为小数 (fmtPct 渲染), 模拟交易统计为百分数原值 — 各自按己方单位渲染, 不做数值换算。 */
interface PaperCompareStats {
  total_return_pct: number | null
  annual_pct: number | null
  max_drawdown: number | null
  win_rate: number | null
  n_trades: number | null
  profit_loss_ratio: number | null
}

function CandidateCompareCard({ paper }: { paper: PaperCompareStats }) {
  const candsQ = useQuery({ queryKey: QK.backtestCandidates, queryFn: api.backtestCandidates })
  const candidates = (candsQ.data?.items ?? []).filter(c => c.kind === 'strategy')
  const [selId, setSelId] = useState('')
  const sel = candidates.find(c => c.id === selId) ?? candidates[0]
  const m = sel?.metrics ?? {}
  const paperPct = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
  const candPct = (v: number | null | undefined) => (v == null ? '—' : fmtPct(v))
  const num = (v: number | null | undefined, digits = 0) => (v == null ? '—' : v.toFixed(digits))
  const rows: Array<[string, string, string]> = [
    ['累计收益', paperPct(paper.total_return_pct), candPct(m.total_return)],
    ['年化收益', paperPct(paper.annual_pct), candPct(m.annual_return)],
    ['最大回撤', paper.max_drawdown != null ? `-${paper.max_drawdown.toFixed(2)}%` : '—', candPct(m.max_drawdown)],
    ['胜率', paper.win_rate != null ? `${paper.win_rate.toFixed(1)}%` : '—', candPct(m.win_rate)],
    ['交易次数', num(paper.n_trades), num(m.n_trades)],
    ['盈亏比', num(paper.profit_loss_ratio, 2), num(m.profit_factor, 2)],
    ['夏普比率', '—', num(m.sharpe, 2)],
  ]
  return (
    <div className="rounded-card border border-border/60 bg-surface p-3">
      <div className="flex items-center gap-2">
        <div className="shrink-0 text-sm font-medium">策略对比</div>
        {candidates.length > 0 ? (
          <select
            value={sel?.id ?? ''}
            onChange={e => setSelId(e.target.value)}
            className="min-w-0 flex-1 rounded-btn border border-border bg-base px-1.5 py-1 text-xs text-secondary"
            title="选择要对比的策略候选"
          >
            {candidates.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        ) : (
          <span className="text-[10px] text-muted">暂无策略候选</span>
        )}
      </div>
      {sel ? (
        <table className="mt-2 w-full text-[11px]">
          <thead>
            <tr className="text-muted">
              <th className="py-0.5 text-left font-normal">指标</th>
              <th className="py-0.5 text-right font-normal">模拟交易</th>
              <th className="max-w-0 truncate py-0.5 text-right font-normal" title={sel.name}>{sel.name} (回测)</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map(([label, p, c]) => (
              <tr key={label} className="border-t border-border/40">
                <td className="py-1 font-sans text-muted">{label}</td>
                <td className="py-1 text-right text-secondary">{p}</td>
                <td className="py-1 text-right text-secondary">{c}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="mt-2 text-[11px] leading-relaxed text-muted">
          在策略页保存回测候选后, 可与模拟交易同口径对比 (累计/年化/回撤/胜率等)。
        </div>
      )}
    </div>
  )
}

/** 自动跟单规则列表 + 新建表单 (V2): 监控事件 → 自动下单 (按账户隔离) */
// ================================================================
// 费用与撮合设置 (当前账户弹窗): 佣金/印花税/滑点 + 涨跌停排队。
// 只影响之后的新成交, 不重算历史台账 (口径与回测费用模型一致)。
// ================================================================
function AccountEditModal({ accId, name = '', initialCash = 0, capitalEditable = false,
  feesOnly = false, fees, queue, onSaved, onClose }: {
  accId: string
  name?: string
  initialCash?: number
  capitalEditable?: boolean
  feesOnly?: boolean
  fees: PaperFees
  queue: boolean
  onSaved: () => void
  onClose: () => void
}) {
  const [accountName, setAccountName] = useState(name)
  const [cash, setCash] = useState(String(initialCash))
  const [commissionWan, setCommissionWan] = useState(String(fees.commission_pct * 10000))
  const [stampQian, setStampQian] = useState(String(fees.stamp_tax_pct * 1000))
  const [slippageBps, setSlippageBps] = useState(String(fees.slippage_bps))
  const [minimum, setMinimum] = useState(String(fees.min_commission ?? 5))
  const [transferWan, setTransferWan] = useState(String((fees.transfer_fee_pct ?? 0) * 10000))
  const [taxMode, setTaxMode] = useState(fees.stamp_tax_mode ?? 'fixed')
  const [queueOn, setQueueOn] = useState(queue)
  const [err, setErr] = useState<string | null>(null)
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: () => {
      const values = {
        commission_pct: Number(commissionWan) / 10000,
        stamp_tax_pct: Number(stampQian) / 1000,
        slippage_bps: Number(slippageBps),
        min_commission: Number(minimum),
        transfer_fee_pct: Number(transferWan) / 10000,
        stamp_tax_mode: taxMode,
        queue_limit_orders: queueOn,
      }
      return feesOnly ? api.paperSettings(values, accId) : api.paperUpdateAccount({
        ...values, name: accountName.trim(),
        ...(capitalEditable ? { initial_cash: Number(cash) } : {}),
      }, accId)
    },
    onSuccess: async () => {
      await paperAccountChanged(qc, accId, 'updated')
      onSaved()
    },
    onError: e => setErr(String((e as Error).message)),
  })
  // 与后端 update_settings 的范围校验一致 (前端先行提示)
  const valid = (feesOnly || (accountName.trim().length > 0 && accountName.trim().length <= 80
    && (!capitalEditable || (cash.trim() !== '' && Number.isFinite(Number(cash)) && Number(cash) > 0))))
    && [commissionWan, stampQian, slippageBps, minimum, transferWan]
      .every(value => value.trim() !== '' && Number.isFinite(Number(value)))
    && Number(commissionWan) >= 0 && Number(commissionWan) <= 100
    && Number(stampQian) >= 0 && Number(stampQian) <= 50
    && Number(slippageBps) >= 0 && Number(slippageBps) <= 200
    && Number(minimum) >= 0 && Number(minimum) <= 10000
    && Number(transferWan) >= 0 && Number(transferWan) <= 100
  const inputCls = 'mt-1 w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  return (
    <Modal onClose={onClose} labelledBy="fee-settings-title" size="sm" closeDisabled={m.isPending}>
        <div className="modal-header flex items-center gap-2">
          <Settings className="h-4 w-4 text-accent" />
          <h3 id="fee-settings-title" className="min-w-0 flex-1 text-sm font-semibold text-foreground">
            {feesOnly ? '账户费用与撮合设置' : '修改模拟账户'}
            <span className="ml-1.5 font-mono text-[10px] text-muted">{accId}</span>
          </h3>
          <button onClick={onClose} disabled={m.isPending} aria-label="关闭账户修改" className="modal-close"><X className="h-4 w-4" /></button>
        </div>
      <div className="modal-body">
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          {feesOnly ? '作用于当前账户全部手动单和自动规则，仅影响之后的新成交，已有台账不重算。'
            : '名称同步更新账户展示；费用与排队设置影响后续成交，历史成交及费用保留。'}
        </p>
        {!feesOnly && <>
        <div className="mt-4">
          <label htmlFor="paper-edit-name" className="block text-[11px] text-muted">账户名称</label>
          <input id="paper-edit-name" maxLength={80} value={accountName} onChange={e => setAccountName(e.target.value)} disabled={m.isPending} className={inputCls} />
        </div>
        <div className="mt-3">
          <label htmlFor="paper-edit-cash" className="block text-[11px] text-muted">初始资金 (元)</label>
          <input id="paper-edit-cash" type="number" min="0" value={cash} onChange={e => setCash(e.target.value)} disabled={!capitalEditable || m.isPending} className={cn(inputCls, 'disabled:opacity-50')} />
          <p className="mt-1 text-[10px] text-muted">
            {capitalEditable ? '未产生业务记录，可同时调整初始资金和现金。' : '已有委托、成交、持仓或净值记录，初始资金不可修改。'}
          </p>
        </div>
        </>}
        <fieldset disabled={m.isPending} className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="paper-fee-commission" className="block text-[11px] text-muted">佣金（万分比，双边）</label>
            <input id="paper-fee-commission" type="number" step="0.1" min="0" max="100" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label htmlFor="paper-fee-minimum" className="block text-[11px] text-muted">单笔最低佣金（元）</label>
            <input id="paper-fee-minimum" type="number" min="0" max="10000" step="0.01" value={minimum} onChange={e => setMinimum(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label htmlFor="paper-fee-tax-mode" className="block text-[11px] text-muted">印花税模式（股票卖出）</label>
            <select id="paper-fee-tax-mode" value={taxMode} onChange={e => setTaxMode(e.target.value as typeof taxMode)} className={inputCls}>
              <option value="fixed">固定费率</option><option value="historical">按成交日期</option>
            </select>
          </div>
          <div>
            <label htmlFor="paper-fee-tax" className="block text-[11px] text-muted">固定印花税（千分比，卖出）</label>
            <input id="paper-fee-tax" disabled={taxMode === 'historical'} type="number" step="0.1" min="0" max="50" value={stampQian} onChange={e => setStampQian(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label htmlFor="paper-fee-transfer" className="block text-[11px] text-muted">额外过户费（万分比，双边）</label>
            <input id="paper-fee-transfer" type="number" min="0" max="100" step="0.01" value={transferWan} onChange={e => setTransferWan(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label htmlFor="paper-fee-slippage" className="block text-[11px] text-muted">滑点（万分比）</label>
            <input id="paper-fee-slippage" type="number" step="1" min="0" max="200" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={inputCls} />
          </div>
        <label className="flex cursor-pointer items-center justify-between gap-2 rounded-btn border border-border bg-base px-3 py-2 sm:col-span-2">
          <span className="text-xs text-secondary">
            涨跌停排队
            <span className="ml-1 text-[10px] text-muted">触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)</span>
          </span>
          <input type="checkbox" checked={queueOn} onChange={e => setQueueOn(e.target.checked)} className="h-4 w-4 shrink-0" />
        </label>
        </fieldset>
        <p className="mt-3 text-[11px] leading-relaxed text-muted">
          万2.5：10万元成交佣金25元；1万元成交按最低佣金计收。1万分比＝1bps＝0.01%；
          滑点调整成交价。过户费只填佣金未包含的额外部分。按日期模式：2023-08-28前1‰，之后0.5‰。
        </p>
        {err && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{err}</div>}
      </div>
        <div className="modal-footer flex gap-2">
          <button
            onClick={() => valid && m.mutate()}
            disabled={!valid || m.isPending}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {m.isPending ? '保存中…' : feesOnly ? '保存账户费用' : '保存'}
          </button>
          <button onClick={onClose} disabled={m.isPending} className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50">
            取消
          </button>
        </div>
    </Modal>
  )
}

// ================================================================
// 多账户横向对比弹窗: 归一化净值叠加图 + 指标对比表 (行=指标, 列=账户)。
// ================================================================
const COMPARE_COLORS = ['#3B82F6', '#F59E0B', '#10B981', '#EF4444', '#8B5CF6', '#06B6D4', '#EC4899', '#84CC16']

function CompareChart({ rows }: { rows: PaperCompareRow[] }) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!elRef.current) return
    chartRef.current = echarts.init(elRef.current, undefined, { renderer: 'canvas' })
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    const observer = new MutationObserver(() => {
      const darkNow = document.documentElement.classList.contains('dark')
      const axisColor = darkNow ? '#8E8E96' : '#52525B'
      const splitColor = darkNow ? '#353539' : '#E4E4E7'
      chartRef.current?.setOption({
        xAxis: { axisLine: { lineStyle: { color: splitColor } }, axisLabel: { color: axisColor } },
        yAxis: { axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: splitColor } } },
      })
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => {
      window.removeEventListener('resize', onResize)
      observer.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const dark = document.documentElement.classList.contains('dark')
    const axisColor = dark ? '#8E8E96' : '#52525B'
    const splitColor = dark ? '#353539' : '#E4E4E7'
    chart.setOption({
      grid: { left: 52, right: 16, top: 30, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => v?.toFixed(4),
      },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: axisColor, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
        splitLine: { lineStyle: { color: splitColor } },
      },
      legend: { top: 0, right: 0, textStyle: { color: axisColor, fontSize: 10 }, itemWidth: 14 },
      series: rows.map((r, i) => ({
        name: r.name,
        type: 'line' as const,
        showSymbol: false,
        // 归一化: 定版净值 / 首日净值 (不同本金也能公平对比)
        data: r.nav.map(n => [n.date, Number((n.nav / (r.nav[0]?.nav || 1)).toFixed(4))]),
        lineStyle: { color: COMPARE_COLORS[i % COMPARE_COLORS.length], width: 2 },
      })),
    })
  }, [rows])

  return <div ref={elRef} className="h-56 w-full" />
}

function AutoSkipsPanel({ skips }: { skips: PaperAutoSkip[] }) {
  if (skips.length === 0) return null
  const shown = skips.slice(0, 12)
  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center gap-1.5 text-sm font-medium">
        <Info className="h-3.5 w-3.5 text-muted" /> 未执行原因
        <span className="ml-1 text-[10px] text-muted">自动跟单跳过留痕 · 诊断用, 不影响撮合</span>
      </div>
      <div className="mt-2 space-y-1">
        {shown.map((s, i) => (
          <div key={`${s.ts}-${i}`} className="flex items-center gap-2 rounded-btn px-2 py-1 text-[11px] hover:bg-elevated/50">
            <span className="w-24 shrink-0 font-mono text-muted">{s.symbol || '—'}</span>
            <span className="min-w-0 flex-1 truncate text-secondary" title={s.rule_name ? `规则「${s.rule_name}」: ${s.message}` : s.message}>
              {s.message}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-muted">{s.ts.slice(11, 19)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function AutoRulesPanel({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const rulesQ = useQuery({ queryKey: QK.paperAutoRules(acc), queryFn: () => api.paperAutoRules(acc) })
  const rules = rulesQ.data?.rules ?? []
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<PaperAutoRule | null>(null)

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: QK.paperAutoRules(acc) })
    void qc.invalidateQueries({ queryKey: QK.paperCompare })
    void qc.invalidateQueries({ queryKey: QK.paperOrders(acc) })
    void qc.invalidateQueries({ queryKey: QK.paperOverview(acc) })
  }

  const toggleM = useMutation({
    mutationFn: (args: { id: string; enabled: boolean }) => api.paperAutoRuleSetEnabled(args.id, args.enabled, acc),
    onSuccess: invalidate,
  })
  const deleteM = useMutation({
    mutationFn: (id: string) => api.paperAutoRuleDelete(id, acc),
    onSuccess: invalidate,
  })

  const closeForm = () => { setCreating(false); setEditing(null) }
  return (
    <>
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">自动跟单规则</div>
        <button onClick={() => setCreating(true)} className="flex items-center gap-1 rounded-btn bg-accent/10 px-2.5 py-1 text-xs text-accent transition-colors hover:bg-accent/20">
          <Plus className="h-3 w-3" /> 新建规则
        </button>
      </div>
      <div className="mt-1 text-[11px] leading-relaxed text-muted">
        策略可自动跟随买卖信号，退出时全平；自动退出不受冷却限制，仍遵守 T+1 和待成交卖单校验。
      </div>
      {rules.length === 0 ? (
        <div className="py-8 text-center text-xs text-muted">暂无规则 — 新建一条, 让策略信号自动进入模拟交易</div>
      ) : (
        <div className="mt-2 space-y-1">
          {rules.map(r => (
            <div key={r.id} className="flex flex-wrap items-center gap-2 rounded-btn px-2 py-1.5 text-xs transition-colors hover:bg-elevated/40">
              <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', r.enabled ? 'bg-bear' : 'bg-muted')} />
              <span className="w-28 shrink-0 truncate font-medium">{r.name}</span>
              <span className="w-20 shrink-0 text-muted">{r.match_kind === 'strategy' ? '跟策略' : '跟规则'}</span>
              <span className="w-32 shrink-0 truncate font-mono text-[11px] text-muted" title={r.match_id}>{r.match_id}</span>
              <span className={cn('shrink-0 font-medium', r.side === 'auto' ? 'text-accent' : r.side === 'buy' ? 'text-bull' : 'text-bear')}>{AUTO_SIDE_LABEL[r.side]}</span>
              <span className="shrink-0 font-mono text-[11px] text-muted">
                {r.side === 'auto' && '买 '}
                {r.size_mode === 'fixed_amount' ? fmtMoney(r.size_value, 0) : `${r.size_value}% 权益`}
                {r.side === 'auto' && ' · 卖全平'}
              </span>
              <span className="shrink-0 text-[11px] text-muted">持仓上限 {(r.max_positions ?? 0) > 0 ? `${r.max_positions}只` : '不限'}</span>
              <span className="shrink-0 text-[11px] text-muted">
                {r.candidate_priority === 'pool' ? '候选池默认顺序' : r.candidate_priority === 'score' ? '评分优先' : r.candidate_priority === 'symbol' ? '代码排序' : '事件顺序'}
                {(r.max_symbol_exposure_pct ?? 0) > 0 && ` · 单股≤${r.max_symbol_exposure_pct}%`}
                {(r.max_total_exposure_pct ?? 0) > 0 && ` · 总仓≤${r.max_total_exposure_pct}%`}
                {r.risk_follow && ' · 策略风控'}
              </span>
              <span className="shrink-0 text-[11px] text-muted">{r.execution_mode === 's02_minute'
                ? 'S02：五分钟容量买入 · 次交易日早盘清仓'
                : `建仓 ${ORDER_TYPE_LABEL[r.buy_order_type ?? r.order_type]} · 清仓 ${ORDER_TYPE_LABEL[r.sell_order_type ?? r.order_type]} · ${r.side === 'auto' ? '买入' : ''}冷却${r.cooldown_days}自然日`}</span>
              <button
                onClick={() => toggleM.mutate({ id: r.id, enabled: !r.enabled })}
                className={cn('ml-auto shrink-0 rounded-btn px-2 py-0.5 text-[10px] transition-colors',
                  r.enabled ? 'bg-bear/10 text-bear hover:bg-bear/20' : 'bg-elevated text-muted hover:text-secondary')}
              >
                {r.enabled ? '停用' : '启用'}
              </button>
              <button onClick={() => setEditing(r)} disabled={toggleM.isPending || deleteM.isPending}
                aria-label={`修改规则 ${r.name}`}
                className="shrink-0 rounded-btn border border-border px-2 py-0.5 text-[10px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-40">
                修改
              </button>
              <button onClick={() => deleteM.mutate(r.id)} className="shrink-0 rounded p-0.5 text-muted hover:text-danger" title="删除规则">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
    {creating && <AutoRuleForm key={`${acc}:new`} acc={acc}
      onDone={() => { closeForm(); invalidate() }} onCancel={closeForm} />}
    {editing && <AutoRuleForm key={`${acc}:${editing.id}`} acc={acc} initialRule={editing}
      onDone={() => { closeForm(); invalidate() }} onCancel={closeForm} />}
    </>
  )
}

function StrategyRiskSummary({ strategy }: { strategy: StrategyDetail }) {
  const pct = (v: number | null | undefined) => v == null ? '未设置' : `${Number((Math.abs(v) * 100).toFixed(4))}%`
  const risk = strategy.entry_risk
  return <div className="mt-3 space-y-1 text-[11px] leading-relaxed text-muted">
    <p>来源：{strategy.name}（{strategy.id}），参数来源为当前策略；开仓时再冻结本轮政策。</p>
    <p>止损（相对买入成本）：{pct(strategy.stop_loss)}；止盈：{pct(strategy.take_profit)}；峰值回落止损：{pct(strategy.trailing_stop)}。</p>
    <p>回撤止盈：收益达到 {pct(strategy.trailing_take_profit_activate)} 后，峰值回撤 {pct(strategy.trailing_take_profit_drawdown)}；最长持仓：{strategy.max_hold_days == null ? '未设置' : `${strategy.max_hold_days}交易日`}。</p>
    <p>入场保护：成交溢价 {pct(risk?.max_entry_premium_pct)}；ATR止损倍数 {risk?.stop_atr_multiple ?? '未设置'}；
      信号低点保护 {risk?.stop_signal_low ? '开启' : '关闭'}；单笔计划风险 {pct(risk?.risk_per_trade_pct)}（不保证最大实际损失）。</p>
    <p>选股范围、基础过滤、日线叠加和评分沿所选策略维护。缺必要风险参考时不放行。</p>
    {(strategy.minute_execution || strategy.execution_backend === 'minute_filter') && <p className="text-warning">
      {strategy.id === 'custom_mvxxcp88'
        ? 'S02请选择“跟随S02分钟买卖规则”：五分钟容量建仓，次交易日按冻结的早盘清仓参数卖出。'
        : '此策略的专用分钟容量执行尚未接入自动跟单，保留未执行原因。'}
    </p>}
  </div>
}

function AccountFeeSummary({ fees }: { fees: PaperFees }) {
  return <p className="text-[11px] leading-relaxed text-muted">
    佣金万{Number((fees.commission_pct * 10000).toFixed(6))}（双边，最低{fees.min_commission ?? 5}元）；
    印花税{fees.stamp_tax_mode === 'historical' ? '按卖出成交日期' : `千${Number((fees.stamp_tax_pct * 1000).toFixed(6))}（股票卖出）`}；
    额外过户费万{Number(((fees.transfer_fee_pct ?? 0) * 10000).toFixed(6))}（双边）；
    滑点万{fees.slippage_bps}。
  </p>
}

function AutoRuleForm({ acc, initialRule, onDone, onCancel }: {
  acc: string; initialRule?: PaperAutoRule; onDone: () => void; onCancel: () => void
}) {
  const [name, setName] = useState(initialRule?.name ?? '')
  const [matchKind, setMatchKind] = useState<'strategy' | 'rule'>(initialRule?.match_kind ?? 'strategy')
  const [matchId, setMatchId] = useState(initialRule?.match_id ?? '')
  const [side, setSide] = useState<AutoRuleSide>(initialRule?.side ?? 'auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>(initialRule?.size_mode ?? 'fixed_amount')
  const [amountValue, setAmountValue] = useState(String(initialRule?.size_mode === 'fixed_amount' ? initialRule.size_value : 10000))
  const [pctValue, setPctValue] = useState(initialRule?.size_mode === 'pct_equity' ? String(initialRule.size_value) : '')
  const sizeValue = sizeMode === 'fixed_amount' ? amountValue : pctValue
  const setSizeValue = sizeMode === 'fixed_amount' ? setAmountValue : setPctValue
  const orderType = initialRule?.order_type ?? 'next_open'
  const [buyOrderType, setBuyOrderType] = useState(initialRule?.buy_order_type ?? orderType)
  const [sellOrderType, setSellOrderType] = useState(initialRule?.sell_order_type ?? orderType)
  const [executionMode, setExecutionMode] = useState<'orders' | 's02_minute'>(initialRule?.execution_mode ?? 'orders')
  const s02Eligible = matchKind === 'strategy' && matchId === 'custom_mvxxcp88' && side === 'auto'
  const s02Execution = s02Eligible && executionMode === 's02_minute'
  const [cooldown, setCooldown] = useState(String(initialRule?.cooldown_days ?? 5))
  const [maxPositions, setMaxPositions] = useState(String(initialRule?.max_positions ?? 0))
  const [candidatePriority, setCandidatePriority] = useState<NonNullable<PaperAutoRule['candidate_priority']>>(initialRule ? initialRule.candidate_priority ?? 'event' : 'score')
  const [symbolExposure, setSymbolExposure] = useState(String(initialRule?.max_symbol_exposure_pct ?? 0))
  const [totalExposure, setTotalExposure] = useState(String(initialRule?.max_total_exposure_pct ?? 0))
  const [riskFollow, setRiskFollow] = useState(initialRule?.risk_follow ?? false)
  const [msg, setMsg] = useState<string | null>(null)
  const [preview, setPreview] = useState(false)
  const [feeOpen, setFeeOpen] = useState(false)
  const [feeDraft, setFeeDraft] = useState<Partial<PaperFees> | null>(null)
  const [saved] = useState(() => storage.strategyBacktestLast.get(null))
  const overviewQ = useQuery({ queryKey: QK.paperOverview(acc), queryFn: () => api.paperOverview(acc) })
  const strategyQ = useQuery({
    queryKey: QK.strategyDetail(matchId), queryFn: () => api.strategyGet(matchId),
    enabled: matchKind === 'strategy' && !!matchId, staleTime: 60_000,
  })
  const savedId = saved?.selectedStrategy ?? ''
  const savedStrategyQ = useQuery({
    queryKey: QK.strategyDetail(savedId), queryFn: () => api.strategyGet(savedId),
    enabled: !!savedId, staleTime: 60_000,
  })
  const backtestDraft = backtestRuleDraft(saved, savedStrategyQ.data)
  const equalPct = equalEquityPct(totalExposure, maxPositions)
  const currentFees = overviewQ.data?.fees
  const fillDraft = () => {
    if (!backtestDraft.available) return
    const d = backtestDraft.rule
    setMatchKind('strategy'); setMatchId(d.match_id!)
    if (d.max_positions != null) setMaxPositions(String(d.max_positions))
    if (d.max_total_exposure_pct != null) setTotalExposure(String(d.max_total_exposure_pct))
    if (d.size_value != null) { setSizeMode('pct_equity'); setPctValue(String(d.size_value)) }
    if (d.buy_order_type) setBuyOrderType(d.buy_order_type)
    if (d.sell_order_type) setSellOrderType(d.sell_order_type)
    setFeeDraft(backtestDraft.fees)
    setPreview(false)
    setMsg(null)
  }

  const m = useMutation({
    mutationFn: () => {
      setMsg(null)
      const body = {
        name: name.trim(),
        match_kind: matchKind,
        match_id: matchId.trim(),
        side,
        size_mode: sizeMode,
        size_value: Number(sizeValue),
        order_type: orderType,
        buy_order_type: buyOrderType,
        sell_order_type: sellOrderType,
        ...(s02Eligible || initialRule?.execution_mode ? {
          execution_mode: s02Execution ? 's02_minute' as const : 'orders' as const,
        } : {}),
        cooldown_days: s02Execution ? 0 : Number(cooldown),
        max_positions: Number(maxPositions),
        candidate_priority: s02Execution ? 'score' as const : candidatePriority,
        max_symbol_exposure_pct: Number(symbolExposure),
        max_total_exposure_pct: Number(totalExposure),
        risk_follow: riskFollow && matchKind === 'strategy' && side !== 'sell',
      }
      return initialRule
        ? api.paperAutoRuleUpdate(initialRule.id, body, acc)
        : api.paperAutoRuleCreate({ ...body, enabled: true }, acc)
    },
    onSuccess: onDone,
    onError: e => setMsg(String((e as Error).message)),
  })

  const valid = name.trim() && matchId.trim() && Number.isFinite(Number(sizeValue)) && Number(sizeValue) > 0
    && (sizeMode !== 'pct_equity' || Number(sizeValue) <= 100)
    && isNonnegativeInteger(cooldown) && isNonnegativeInteger(maxPositions)
    && [symbolExposure, totalExposure].every(v => v.trim() !== '' && Number.isFinite(Number(v)) && Number(v) >= 0 && Number(v) <= 100)
    && (side !== 'auto' || matchKind === 'strategy')
    && (candidatePriority !== 'pool' || matchKind === 'strategy')

  // ID 联想: 跟策略 → 策略引擎列表; 跟监控规则 → 监控规则列表。本地按中文名/ID 过滤。
  const strategiesQ = useQuery({
    queryKey: QK.screenerStrategies('any', 'all'),
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({
    queryKey: QK.monitorRules,
    queryFn: api.monitorRulesList,
    staleTime: 30_000,
  })
  const idQuery = matchId.trim().toLowerCase()
  const idOptions: SuggestOption[] = (() => {
    if (matchKind === 'strategy') {
      return (strategiesQ.data?.presets ?? [])
        .filter(s => !idQuery || s.name.toLowerCase().includes(idQuery) || s.id.toLowerCase().includes(idQuery))
        .slice(0, 8)
        .map(s => ({
          value: s.id,
          label: s.name || s.id,
          hint: <span className="shrink-0 font-mono text-[10px] text-muted">{s.id}</span>,
        }))
    }
    return (rulesQ.data?.rules ?? [])
      .filter(r => !idQuery || r.name.toLowerCase().includes(idQuery) || r.id.toLowerCase().includes(idQuery))
      .slice(0, 8)
      .map(r => ({
        value: r.id,
        label: r.name || r.id,
        hint: <span className={cn('shrink-0 font-mono text-[10px]', r.enabled ? 'text-muted' : 'text-warning/70')}>{r.id}</span>,
      }))
  })()

  const content = (
    <>
      <div className="modal-header flex items-center justify-between text-sm font-medium">
        <h3 id="auto-rule-form-title">{initialRule ? '修改' : '新建'}自动跟单规则</h3>
        <button onClick={onCancel} disabled={m.isPending} aria-label="关闭跟单规则" className="modal-close disabled:opacity-40">
          <X className="h-4 w-4" />
        </button>
      </div>
      <fieldset disabled={m.isPending} className="modal-body space-y-5">
        <div className="rounded-btn border border-border bg-base/50 p-3 text-[11px] leading-relaxed text-muted">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>账户初始资金 {fmtMoney(overviewQ.data?.initial_cash, 0)}元 · 当前权益 {fmtMoney(overviewQ.data?.total, 0)}元。
              {initialRule ? `规则当前${initialRule.enabled ? '启用' : '停用'}，保存参数保持此状态。` : '新建规则保存后启用。'}</span>
            <button onClick={() => setPreview(!preview)} className="rounded-btn border border-border px-3 py-1.5 text-accent">参考回测参数</button>
          </div>
          {preview && <div className="mt-3 space-y-2">
            <p>来源：{savedStrategyQ.data?.name ?? (savedId || '无已保存记录')}；最近成功回测设置，当前登录账户作用域。</p>
            {backtestDraft.available && <p>
              可同步：{backtestDraft.rule.max_positions != null && `最多${backtestDraft.rule.max_positions}只；`}
              {backtestDraft.rule.max_total_exposure_pct != null && `总仓位${backtestDraft.rule.max_total_exposure_pct}%；`}
              {backtestDraft.rule.size_value != null && `等权单次${backtestDraft.rule.size_value}%权益；`}
              {backtestDraft.rule.buy_order_type && `建仓${ORDER_TYPE_LABEL[backtestDraft.rule.buy_order_type]}；`}
              {backtestDraft.rule.sell_order_type && `清仓${ORDER_TYPE_LABEL[backtestDraft.rule.sell_order_type]}；`}
              兼容费用填独立草稿。
            </p>}
            <ul className="list-disc space-y-1 pl-4">{backtestDraft.notes.map(note => <li key={note}>{note}</li>)}</ul>
            <button disabled={!backtestDraft.available} onClick={fillDraft} className="rounded-btn bg-accent/10 px-3 py-1.5 text-accent disabled:opacity-40">填入草稿</button>
          </div>}
        </div>
        <section className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <h4 className="text-sm font-medium md:col-span-2">1. 跟单对象</h4>
        <div>
          <label htmlFor="auto-rule-name" className="text-[11px] text-muted">规则名称</label>
          <input id="auto-rule-name" value={name} onChange={e => setName(e.target.value)} placeholder="如: 跟单前高突破"
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="text-[11px] text-muted">信号来源</label>
          <div className="mt-1 flex gap-1.5">
            {(['strategy', 'rule'] as const).map(k => (
              <button key={k} onClick={() => { setMatchKind(k); setMatchId(''); if (k === 'rule') { if (side === 'auto') setSide('buy'); if (candidatePriority === 'pool') setCandidatePriority('event') } }}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  matchKind === k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                {k === 'strategy' ? '跟策略' : '跟监控规则'}
              </button>
            ))}
          </div>
        </div>
        <div className="md:col-span-2">
          <label className="text-[11px] text-muted">{matchKind === 'strategy' ? '策略 (输入中文名 / ID 联想, 留空聚焦看全部)' : '监控规则 (输入名称 / ID 联想)'}</label>
          <SuggestInput
            value={matchId}
            onChange={setMatchId}
            options={idOptions}
            onSelect={opt => setMatchId(opt.value)}
            loading={matchKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
            placeholder={matchKind === 'strategy' ? '如: 前高突破 / sg_...' : '如: 高位放量 / mrule_...'}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50"
          />
        </div>
        <div>
          <label className="text-[11px] text-muted">方向</label>
          <div className="mt-1 flex gap-1.5">
            {(['auto', 'buy', 'sell'] as const).map(sd => (
              <button key={sd} onClick={() => setSide(sd)} disabled={sd === 'auto' && matchKind !== 'strategy'}
                aria-pressed={side === sd}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] font-medium transition-colors',
                  side === sd ? (sd === 'auto' ? 'border-accent/40 bg-accent/10 text-accent' : sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted',
                  'disabled:cursor-not-allowed disabled:opacity-40')}>
                {AUTO_SIDE_LABEL[sd]}
              </button>
            ))}
          </div>
          <div className="mt-1 text-[10px] text-muted">
            {s02Execution ? '跟随S02买入信号与冻结的早盘清仓定义；股票池变化不直接下单。'
              : matchKind === 'rule' ? '监控规则需指定固定买卖方向，固定方向覆盖事件方向。' : '自动：买入信号/入池买入，卖出信号/出池全平。固定买卖覆盖信号方向。'}
          </div>
        </div>
        </section>
        <section className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <h4 className="text-sm font-medium md:col-span-2">2. 买入与仓位</h4>
        <div>
          <label className="text-[11px] text-muted">单次下单规模</label>
          <div className="mt-1 flex gap-1.5">
            <button onClick={() => setSizeMode('fixed_amount')}
              className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'fixed_amount' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>固定金额</button>
            <button onClick={() => setSizeMode('pct_equity')}
              className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'pct_equity' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>权益 %</button>
          </div>
        </div>
        <div>
          <label htmlFor="auto-rule-size" className="text-[11px] text-muted">{side === 'auto' ? '买入' : '每笔'}{sizeMode === 'fixed_amount' ? '金额 (元)' : '占权益 %'}</label>
          <input id="auto-rule-size" type="number" value={sizeValue} onChange={e => setSizeValue(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
          <div className="mt-1 text-[10px] text-muted">每只候选、每次下单的目标。权益含现金和持仓估值；实际按整手、费用和预算缩减。自动退出全平，不按此规模截取。</div>
        </div>
        <div>
          <label htmlFor="auto-rule-cooldown" className="text-[11px] text-muted">{side === 'auto' ? '买入冷却天数 (同标的)' : '冷却天数 (同标的)'}</label>
          <input id="auto-rule-cooldown" disabled={s02Execution} type="number" min="0" step="1" value={s02Execution ? '0' : cooldown} onChange={e => setCooldown(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
          <div className="mt-1 text-[10px] text-muted">自然日，以最近自动下单为起点，撤单仍计入；0不冷却。自动退出不受买入冷却限制。</div>
        </div>
        <div>
          <label htmlFor="auto-rule-max-positions" className="text-[11px] text-muted">最大持仓标的数 (0 不限)</label>
          <input id="auto-rule-max-positions" type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
          <div className="mt-1 text-[10px] text-muted">全账户持仓和待买标的均占名额；普通加仓不新增名额，组合批次跳过已有持仓/待买标的。账户硬上限50只。</div>
        </div>
        <div>
          <label htmlFor="auto-rule-priority" className="text-[11px] text-muted">候选买入顺序</label>
          <select id="auto-rule-priority" disabled={s02Execution} value={s02Execution ? 'score' : candidatePriority} onChange={e => setCandidatePriority(e.target.value as typeof candidatePriority)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm">
            <option value="score">评分从高到低（同分按代码，缺分最后）</option>
            <option value="symbol">按证券代码</option>
            <option value="event">按事件到达顺序</option>
            <option value="pool" disabled={matchKind !== 'strategy'}>候选股票池默认顺序</option>
          </select>
          <div className="mt-1 text-[10px] text-muted">{matchKind !== 'strategy' ? '监控规则没有策略候选池，不能选择池序。' :
            '只安排同批已触发候选。池序沿完整候选快照；S02按ATR比例降序，同值代码升序、未知末尾。缺顺位时留未执行原因。'}</div>
        </div>
        <div>
          <label htmlFor="auto-rule-symbol-exposure" className="text-[11px] text-muted">单股仓位上限 %（0 不限）</label>
          <input id="auto-rule-symbol-exposure" type="number" min="0" max="100" value={symbolExposure} onChange={e => setSymbolExposure(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm" />
        </div>
        <div>
          <label htmlFor="auto-rule-total-exposure" className="text-[11px] text-muted">账户总仓位上限 %（0 不限）</label>
          <input id="auto-rule-total-exposure" type="number" min="0" max="100" value={totalExposure} onChange={e => setTotalExposure(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm" />
          <div className="mt-1 text-[10px] text-muted">包含账户全部持仓、待成交买单和加仓；待卖单成交前不释放额度。</div>
        </div>
        <div className="rounded-btn bg-base p-3 text-[11px] leading-relaxed text-muted md:col-span-2">
          <p>单次目标 {sizeValue.trim() && Number(sizeValue) > 0
            ? `${fmtMoney(sizeMode === 'pct_equity' ? (overviewQ.data?.total ?? 0) * Number(sizeValue) / 100 : Number(sizeValue), 0)}元`
            : '待填写'}；单股/总仓位为累计上限，多规则组合采用较严格的非零上限。</p>
          <button disabled={equalPct == null} onClick={() => { setSizeMode('pct_equity'); setPctValue(String(equalPct)) }}
            className="mt-2 rounded-btn border border-border px-3 py-1 text-accent disabled:opacity-40">按回测等权口径计算</button>
          <span className="ml-2">{equalPct == null ? '需要正总仓位与1～50只持仓数' : `${totalExposure}% ÷ ${maxPositions}只 = ${equalPct}%权益/只`}</span>
        </div>
        </section>
        <section className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <h4 className="text-sm font-medium md:col-span-2">3. 成交方式</h4>
          {s02Eligible && <div className="md:col-span-2">
            <label htmlFor="auto-rule-execution" className="text-[11px] text-muted">策略成交方式</label>
            <select id="auto-rule-execution" value={executionMode} onChange={e => setExecutionMode(e.target.value as typeof executionMode)}
              className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm">
              <option value="s02_minute">跟随S02分钟买卖规则</option>
              <option value="orders">普通订单</option>
            </select>
          </div>}
          {s02Execution ? <p className="text-[11px] leading-relaxed text-muted md:col-span-2">
            买入沿S02确认后的五个交易分钟，以分钟最高价及成交量容量分片，买足提前结束，窗口结束撤余量。
            卖出遵守T+1，按买入时保存的S02早盘清仓参数逐分钟卖出，截止后仍有余股会明确记录失败。
            同一委托累计最低佣金只收一次，滑点计入现金成本；按S02评分顺序买入，无额外自然日冷却。
          </p> : <>{([['buy', buyOrderType, setBuyOrderType], ['sell', sellOrderType, setSellOrderType]] as const).map(([direction, value, setter]) => <div key={direction}>
            <label htmlFor={`auto-rule-${direction}-type`} className="text-[11px] text-muted">{direction === 'buy' ? '建仓' : '清仓'}订单类型</label>
            <select id={`auto-rule-${direction}-type`} value={value} onChange={e => setter(e.target.value as typeof value)}
              className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm">
              {(['next_open', 'close', 'market'] as const).map(t => <option key={t} value={t}>{ORDER_TYPE_LABEL[t]}</option>)}
            </select>
          </div>)}
          <p className="text-[11px] leading-relaxed text-muted md:col-span-2">即时在下单后的下一次有效实时快照撮合，次日开盘为单一开盘价；收盘仅在合法实际时点撮合，不回填已打印价格。两侧独立，旧共同类型作回退。卖出仍遵守T+1和待卖占用。</p></>}
        </section>
        <section>
        <h4 className="mb-3 text-sm font-medium">4. 策略风控</h4>
        {matchKind === 'strategy' && side !== 'sell' && (
          <label className="flex items-start gap-2 text-xs text-secondary">
            <input type="checkbox" checked={riskFollow} onChange={e => setRiskFollow(e.target.checked)} className="mt-0.5" />
            <span>跟随入场时策略风控
              <span className="mt-1 block text-[10px] text-muted">保存止损、止盈、移动风控和持有期。后续改策略不改变本轮政策；无入场快照的旧持仓从下一轮新开仓生效。</span>
            </span>
          </label>
        )}
        {matchKind !== 'strategy' || side === 'sell' ? <p className="text-[11px] text-muted">只有策略买入可冻结入场风控；固定卖出不建立新入场政策。</p>
          : strategyQ.data ? <StrategyRiskSummary strategy={strategyQ.data} />
          : <p className="mt-2 text-[11px] text-muted">{!matchId ? '选择策略后显示当前风控参数。' : strategyQ.isError ? '策略详情加载失败，请核对来源ID。' : '正在读取策略风控…'}</p>}
        </section>
        <section>
          <h4 className="mb-3 text-sm font-medium">5. 账户费用</h4>
          {currentFees ? <>
            <AccountFeeSummary fees={currentFees} />
            <p className="mt-1 text-[11px] text-muted">涨跌停排队{overviewQ.data?.queue_limit_orders ? '开启' : '关闭'}。费用影响本账户全部规则和手动单；“保存规则”不会保存费表。</p>
            {feeDraft && <div className="mt-2 rounded-btn border border-accent/30 p-2">
              <p className="text-[11px] text-accent">已导入回测费用草稿，尚未保存</p>
              <AccountFeeSummary fees={{ ...currentFees, ...feeDraft }} />
            </div>}
            <button onClick={() => setFeeOpen(true)} className="mt-2 rounded-btn border border-border px-3 py-1.5 text-xs text-accent">修改账户费用</button>
          </> : <p className="text-[11px] text-muted">账户费表尚未加载，不能用默认费用代替保存值。</p>}
        </section>
      </fieldset>
      <div className="modal-footer">
      <div className="flex gap-2">
        <button onClick={() => valid && m.mutate()} disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity disabled:opacity-40">
          {initialRule ? (m.isPending ? '保存中…' : '保存修改') : (m.isPending ? '创建中…' : '创建规则')}
        </button>
        <button onClick={onCancel} disabled={m.isPending} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:text-foreground disabled:opacity-40">取消</button>
      </div>
      {msg && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{msg}</div>}
      </div>
    </>
  )
  return <>
    <Modal size="md" labelledBy="auto-rule-form-title" onClose={onCancel} closeDisabled={m.isPending || feeOpen}>{content}</Modal>
    {feeOpen && currentFees && <AccountEditModal feesOnly accId={acc} fees={{ ...currentFees, ...feeDraft }}
      queue={overviewQ.data?.queue_limit_orders ?? false} onClose={() => setFeeOpen(false)}
      onSaved={() => { setFeeOpen(false); setFeeDraft(null) }} />}
  </>
}

// ================================================================
// 多账户对比 (V3): 模拟交易默认主视图 — 对比优先, 收益降序。
// ================================================================

/** 净值迷你走势 (纯 SVG polyline, 最近 ~30 个定版点; 红涨绿跌与全局口径一致) */
function NavSpark({ nav }: { nav: Array<{ date: string; nav: number }> }) {
  const pts = nav.map(n => n.nav).filter(v => v > 0).slice(-30)
  if (pts.length < 2) return <span className="block text-center text-[11px] text-muted">—</span>
  const w = 84, h = 24, pad = 2
  const min = Math.min(...pts)
  const max = Math.max(...pts)
  const span = max - min || 1
  const step = (w - pad * 2) / (pts.length - 1)
  const up = pts[pts.length - 1] >= pts[0]
  const d = pts
    .map((v, i) => `${(pad + i * step).toFixed(1)},${(h - pad - ((v - min) / span) * (h - pad * 2)).toFixed(1)}`)
    .join(' ')
  return (
    <svg width={w} height={h} className="mx-auto block" aria-hidden>
      <polyline points={d} fill="none" stroke={up ? '#F04438' : '#12B76A'} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function RankBadge({ rank, accent }: { rank: number; accent?: boolean }) {
  return (
    <span className={cn(
      'inline-flex h-5 w-5 items-center justify-center rounded-full font-mono text-[11px] font-semibold',
      accent && rank === 1 ? 'bg-accent/20 text-accent' : accent && rank <= 3 ? 'bg-elevated text-secondary' : 'text-muted',
    )}>
      {rank}
    </span>
  )
}

/** 对比榜单可排序列 (表头点击切换) */
type CmpSortKey = 'name' | 'total' | 'pnl_pct' | 'day_change_pct' | 'win_rate' | 'profit_loss_ratio' | 'rounds' | 'holdings_count' | 'last_nav_date'

/** 可空列: 无值行排序时恒垫底 (与方向无关), 见 sorted 比较器 */
const CMP_NULLABLE: Partial<Record<CmpSortKey, (r: PaperCompareRow) => boolean>> = {
  pnl_pct: r => r.pnl_pct == null,
  day_change_pct: r => r.day_change_pct == null,
  profit_loss_ratio: r => r.profit_loss_ratio == null,
  last_nav_date: r => r.last_nav_date == null,
}

function cmpSortVal(k: CmpSortKey, r: PaperCompareRow): string | number {
  switch (k) {
    case 'name': return r.name
    case 'total': return r.total ?? 0
    case 'pnl_pct': return r.pnl_pct ?? 0
    case 'day_change_pct': return r.day_change_pct ?? 0
    case 'win_rate': return r.win_rate ?? 0
    case 'profit_loss_ratio': return r.profit_loss_ratio ?? 0
    case 'rounds': return r.rounds ?? 0
    case 'holdings_count': return r.holdings_count ?? 0
    case 'last_nav_date': return r.last_nav_date ?? ''
  }
}

/** 可排序表头: 点击切换该列升降序, 活动列显方向箭头, 非活动列 hover 显淡箭头 (样式随 Review._DtTh) */
function SortTh({ label, sortKey, sort, onSort, align = 'left', title }: {
  label: string
  sortKey: CmpSortKey
  sort: { key: CmpSortKey; desc: boolean }
  onSort: (k: CmpSortKey) => void
  align?: 'left' | 'right'
  title?: string
}) {
  const active = sort.key === sortKey
  return (
    <th className={cn('group/th py-1.5 font-medium', align === 'right' && 'text-right')}>
      <button
        type="button"
        title={title}
        onClick={() => onSort(sortKey)}
        className={cn('inline-flex items-center gap-0.5 transition-colors hover:text-foreground', active && 'text-foreground')}
      >
        {label}
        {active ? (
          sort.desc
            ? <ChevronDown className="h-2.5 w-2.5 opacity-80" />
            : <ChevronUp className="h-2.5 w-2.5 opacity-80" />
        ) : (
          <ChevronDown className="h-2.5 w-2.5 opacity-0 transition-opacity group-hover/th:opacity-40" />
        )}
      </button>
    </th>
  )
}

function CompareView({ onCreateSingle }: { onCreateSingle: () => void }) {
  const [createOpen, setCreateOpen] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  // 表头排序: 默认累计收益降序; 同列再点翻转方向, 换列时数值列默认降序、账户名默认升序
  const [sort, setSort] = useState<{ key: CmpSortKey; desc: boolean }>({ key: 'pnl_pct', desc: true })
  const onSortCmp = (k: CmpSortKey) =>
    setSort(s => (s.key === k ? { key: k, desc: !s.desc } : { key: k, desc: k !== 'name' }))
  // 60s 轻轮询: 盘中自动跟单下单 / 盘后结算后榜单自然刷新 (保留旧数据防闪烁)。
  // 非交易时段榜单不变, 降为 5 分钟兜底 (字段缺失保持 60s)
  const { data: quoteStatus } = useQuoteStatus()
  const cmpQ = useQuery({
    queryKey: QK.paperCompare,
    queryFn: api.paperCompare,
    refetchInterval: () => (quoteStatus?.is_trading_hours === false ? 300_000 : 60_000),
    placeholderData: (prev: any) => prev,
  })

  const rows = useMemo(() => cmpQ.data?.accounts ?? [], [cmpQ.data?.accounts])
  useEffect(() => {
    if (cmpQ.data && expanded && !rows.some(row => row.account === expanded)) setExpanded(null)
  }, [cmpQ.data, expanded, rows])
  // 收益口径固定排序: 统计卡「收益最高」与净值叠加「收益前 8 名」不受表头排序影响
  const byPnl = useMemo(() => [...rows].sort((a, b) => (b.pnl_pct ?? -1e9) - (a.pnl_pct ?? -1e9)), [rows])
  const sorted = useMemo(() => {
    const dir = sort.desc ? -1 : 1
    const isNull = CMP_NULLABLE[sort.key]
    return [...rows].sort((a, b) => {
      if (isNull) {
        const na = isNull(a), nb = isNull(b)
        if (na || nb) return na === nb ? 0 : na ? 1 : -1 // 无值行恒垫底, 不随方向翻到顶部
      }
      const va = cmpSortVal(sort.key, a)
      const vb = cmpSortVal(sort.key, b)
      const c = typeof va === 'string' || typeof vb === 'string'
        ? String(va).localeCompare(String(vb), 'zh-Hans-CN')
        : va - vb
      return c * dir
    })
  }, [rows, sort])
  const chartRows = byPnl.filter(r => r.nav.length > 0).slice(0, 8)
  const avgPct = rows.length > 0
    ? rows.reduce((s, r) => s + (r.pnl_pct ?? 0), 0) / rows.length
    : null
  const totalAssets = rows.reduce((s, r) => s + (r.total ?? 0), 0)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="模拟"
        subtitle="多账户并行对比 — 同本金 · 同费率 · 点表头排序, 点行展开完整账户操作"
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCreateOpen(true)}
              className="flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:bg-accent/90"
              title="同本金同费率批量创建一批账户, 各绑一条策略/监控规则跟单, 保证对比口径一致"
            >
              <Plus className="h-3 w-3" />
              批量创建账户
            </button>
            <button
              onClick={onCreateSingle}
              className="flex items-center gap-0.5 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title="新建单个虚拟账户 (原流程)"
            >
              <Plus className="h-3 w-3" />
              单账户
            </button>
            <button
              onClick={() => cmpQ.refetch()}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
            >
              <RefreshCw className={cn('h-3 w-3', cmpQ.isFetching && 'animate-spin')} />
              刷新
            </button>
          </div>
        }
      />
      <div className="page-content min-h-0 flex-1 overflow-y-auto">
        {cmpQ.isLoading ? (
          <div className="page-card p-4 text-center text-sm text-muted" role="status">加载中…</div>
        ) : cmpQ.isError ? (
          <div className="page-card p-4 text-center text-sm text-danger">
            对比数据加载失败
            <button onClick={() => cmpQ.refetch()} className="ml-2 rounded-btn border border-border px-2 py-0.5 text-xs text-secondary hover:text-foreground">重试</button>
          </div>
        ) : rows.length === 0 ? (
          <div className="page-card w-full p-4 text-center">
            <GitCompare className="mx-auto h-8 w-8 text-muted/50" />
            <div className="mt-3 text-sm font-medium">还没有对比账户</div>
            <div className="mt-1.5 text-xs leading-relaxed text-muted">
              同本金、同费率批量创建一批账户, 各绑一个策略或监控规则,
              信号自动跟单 — 同口径的并行对比才有参考意义。
            </div>
            <div className="mt-4 flex justify-center gap-2">
              <button
                onClick={() => setCreateOpen(true)}
                className="flex items-center gap-1 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:bg-accent/90"
              >
                <Plus className="h-3.5 w-3.5" />
                批量创建账户
              </button>
              <button
                onClick={onCreateSingle}
                className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              >
                先建一个单账户
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatCard label="对比账户" value={String(rows.length)} icon={Wallet} iconCls="text-accent" />
              <StatCard label="合计虚拟资产" value={fmtMoney(totalAssets, 0)} icon={Banknote} iconCls="text-muted" />
              <StatCard
                label="平均累计收益"
                value={avgPct == null ? '—' : fmtPct(avgPct / 100)}
                valueClass={priceColorClass((avgPct ?? 0) / 100)}
                icon={TrendingUp}
                iconCls={priceColorClass((avgPct ?? 0) / 100)}
              />
              <StatCard
                label="收益最高"
                value={byPnl[0].name}
                sub={byPnl.length > 1 ? `+${(byPnl[0].pnl_pct ?? 0).toFixed(2)}% 高于第2名 ${((byPnl[0].pnl_pct ?? 0) - (byPnl[1].pnl_pct ?? 0)).toFixed(2)}pct` : undefined}
                valueClass="text-accent"
                icon={TrendingUp}
                iconCls="text-accent"
              />
            </div>

            {chartRows.length > 1 && (
              <div className="rounded-card border border-border bg-surface p-4">
                <div className="text-sm font-medium">归一化净值叠加 <span className="ml-1 text-[10px] text-muted">起点 = 1, 收益前 8 名 · 不同本金公平对比</span></div>
                <CompareChart rows={chartRows} />
              </div>
            )}

            <div className="rounded-card border border-border bg-surface p-4">
              <div className="flex items-center gap-2">
                <div className="text-sm font-medium">收益对比</div>
                <span className="text-[10px] text-muted">点表头排序 · 点行展开完整账户操作</span>
                <span className="ml-auto text-[10px] text-muted" title="结算后更新; 盘中自动跟单的单可见于展开区">数据截至最近结算日</span>
              </div>
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[920px] text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-[10px] text-muted">
                      <th className="py-1.5 pr-1 font-medium">#</th>
                      <SortTh label="账户" sortKey="name" sort={sort} onSort={onSortCmp} />
                      <SortTh label="总资产" sortKey="total" sort={sort} onSort={onSortCmp} align="right" />
                      <SortTh label="累计收益" sortKey="pnl_pct" sort={sort} onSort={onSortCmp} align="right" />
                      <SortTh label="最新日" sortKey="day_change_pct" sort={sort} onSort={onSortCmp} align="right" title="最近两个定版净值的涨跌" />
                      <SortTh label="胜率" sortKey="win_rate" sort={sort} onSort={onSortCmp} align="right" />
                      <SortTh label="盈亏比" sortKey="profit_loss_ratio" sort={sort} onSort={onSortCmp} align="right" title="平均盈利回合 / 平均亏损回合, 无亏损回合时为空" />
                      <SortTh label="回合" sortKey="rounds" sort={sort} onSort={onSortCmp} align="right" title="FIFO 配对的完整买卖回合" />
                      <SortTh label="持仓" sortKey="holdings_count" sort={sort} onSort={onSortCmp} align="right" />
                      <th className="py-1.5 text-center font-medium">净值趋势</th>
                      <SortTh label="最近结算" sortKey="last_nav_date" sort={sort} onSort={onSortCmp} align="right" />
                      <th className="w-14 py-1.5" />
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.map((r, i) => {
                      const open = expanded === r.account
                      return (
                        <Fragment key={r.account}>
                          <tr
                            className={cn('cursor-pointer border-t border-border/50 transition-colors hover:bg-elevated/40', open && 'bg-elevated/30')}
                            onClick={() => setExpanded(open ? null : r.account)}
                          >
                            <td className="py-2 pr-1" title="当前排序列位 (仅按累计收益降序时高亮为收益名次)"><RankBadge rank={i + 1} accent={sort.key === 'pnl_pct' && sort.desc} /></td>
                            <td className="py-2">
                              <div className="flex items-center gap-1.5">
                                <span className="max-w-44 truncate text-xs font-medium">{r.name}</span>
                                {r.status === 'frozen' && <span className="rounded-full bg-warning/15 px-1.5 py-px text-[9px] leading-4 text-warning">冻结</span>}
                                {(r.auto_rules ?? []).slice(0, 2).map((ar, j) => (
                                  <span
                                    key={`${ar.match_id}-${j}`}
                                    className={cn('max-w-32 truncate rounded-full px-1.5 py-px text-[9px] leading-4', ar.enabled ? 'bg-accent/10 text-accent' : 'bg-elevated text-muted')}
                                    title={`${ar.name} · ${ar.match_kind === 'strategy' ? '跟策略' : '跟规则'} ${ar.match_id} · ${ar.side === 'auto' ? '自动方向，退出全平' : AUTO_SIDE_LABEL[ar.side]} · 持仓上限 ${(ar.max_positions ?? 0) > 0 ? `${ar.max_positions}只` : '不限'}`}
                                  >
                                    {ar.name}{ar.side === 'auto' ? '·自动' : ar.side === 'sell' ? '·卖' : ''}
                                  </span>
                                ))}
                                {(r.auto_rules?.length ?? 0) > 2 && (
                                  <span className="shrink-0 text-[9px] text-muted" title={r.auto_rules!.slice(2).map(a => a.name).join(' / ')}>+{r.auto_rules!.length - 2}</span>
                                )}
                              </div>
                              <div className="font-mono text-[10px] text-muted">{r.account}</div>
                            </td>
                            <td className="py-2 text-right font-mono tabular">{fmtMoney(r.total, 0)}</td>
                            <td className="py-2 text-right font-mono font-semibold tabular">
                              <span className={priceColorClass((r.pnl_pct ?? 0) / 100)}>{fmtPct((r.pnl_pct ?? 0) / 100)}</span>
                            </td>
                            <td className="py-2 text-right font-mono tabular">
                              {r.day_change_pct != null
                                ? <span className={priceColorClass(r.day_change_pct / 100)}>{fmtPct(r.day_change_pct / 100)}</span>
                                : <span className="text-muted">—</span>}
                            </td>
                            <td className="py-2 text-right font-mono tabular text-secondary">{r.win_rate.toFixed(1)}%</td>
                            <td className="py-2 text-right font-mono tabular text-secondary">
                              {r.profit_loss_ratio != null ? r.profit_loss_ratio : <span className="text-muted">—</span>}
                            </td>
                            <td className="py-2 text-right font-mono tabular text-muted">{r.rounds}</td>
                            <td className="py-2 text-right font-mono tabular text-secondary">{r.holdings_count ?? 0}</td>
                            <td className="py-1.5"><NavSpark nav={r.nav} /></td>
                            <td className="py-2 text-right font-mono text-[11px] text-muted">{r.last_nav_date ?? '—'}</td>
                            <td className="py-2 text-right">
                              <ChevronDown className={cn('inline h-3.5 w-3.5 text-muted transition-transform duration-150', open && 'rotate-180')} />
                            </td>
                          </tr>
                          {open && (
                            <tr className="border-t border-border/50 bg-base/40">
                              <td colSpan={12} className="px-4 py-4">
                                <AccountPanel acc={r.account} name={r.name} onDeleted={() => setExpanded(null)} />
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>
      {createOpen && <BatchCreateModal onClose={() => setCreateOpen(false)} onCreated={() => setCreateOpen(false)} />}
    </div>
  )
}

// ================================================================
// 批量创建对比账户弹窗: 同口径由构造保证 (同本金/同费率/同跟单参数)。
// 来源行只带 名称+匹配目标; 方向/仓位/订单类型/冷却为全表共享参数。
// ================================================================
type BatchSourceRow = { name: string; kind: 'strategy' | 'rule'; matchId: string }

function BatchCreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (count: number) => void }) {
  const qc = useQueryClient()
  const defaults = loadNewAccountDefaults()
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaults.stampQian)
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [prefix, setPrefix] = useState('')
  const [rows, setRows] = useState<BatchSourceRow[]>([])
  const [quickKind, setQuickKind] = useState<'strategy' | 'rule'>('strategy')
  const [quickId, setQuickId] = useState('')
  // 共享跟单参数 (应用到全部来源 → 同规格对比)
  const [side, setSide] = useState<AutoRuleSide>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('pct_equity')
  const [sizeValue, setSizeValue] = useState('10')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('0')
  const hasMonitorSource = rows.some(row => row.kind === 'rule')

  const strategiesQ = useQuery({
    queryKey: QK.screenerStrategies('any', 'all'),
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList, staleTime: 30_000 })

  const m = useMutation({
    mutationFn: () =>
      api.paperArenaCreate({
        initial_cash: Number(cash),
        name_prefix: prefix.trim() || undefined,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
        sources: rows.map(r => ({
          name: r.name.trim() || undefined,
          match_kind: r.kind,
          match_id: r.matchId.trim(),
          side,
          size_mode: sizeMode,
          size_value: Number(sizeValue),
          order_type: orderType,
          cooldown_days: Number(cooldown),
          max_positions: Number(maxPositions),
        })),
      }),
    onSuccess: r => {
      qc.invalidateQueries({ queryKey: QK.paperAll })
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({ cash, commissionWan, stampQian, slippageBps }))
      } catch { /* 存储不可用时静默 */ }
      onCreated(r.created.length)
    },
  })

  const addRow = (row: BatchSourceRow) => {
    setRows(prev => (prev.some(r => r.matchId === row.matchId) ? prev : [...prev, row]))
  }
  const addAllStrategies = () => {
    const presets = (strategiesQ.data?.presets ?? []).slice(0, 20)
    setRows(prev => {
      const seen = new Set(prev.map(r => r.matchId))
      return [...prev, ...presets.filter(p => !seen.has(p.id)).map(p => ({ name: p.name || p.id, kind: 'strategy' as const, matchId: p.id }))]
    })
  }

  const valid = rows.length >= 1
    && rows.length <= 20
    && rows.every(r => r.matchId.trim().length > 0)
    && Number(cash) > 0
    && Number(sizeValue) > 0
    && (sizeMode !== 'pct_equity' || Number(sizeValue) <= 100)
    && isNonnegativeInteger(cooldown) && isNonnegativeInteger(maxPositions)
    && (side !== 'auto' || !hasMonitorSource)
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0

  const quickQuery = quickId.trim().toLowerCase()
  const quickOptions: SuggestOption[] = quickKind === 'strategy'
    ? (strategiesQ.data?.presets ?? [])
        .filter(s => !quickQuery || s.name.toLowerCase().includes(quickQuery) || s.id.toLowerCase().includes(quickQuery))
        .slice(0, 8)
        .map(s => ({ value: s.id, label: s.name || s.id, hint: <span className="shrink-0 font-mono text-[10px] text-muted">{s.id}</span> }))
    : (rulesQ.data?.rules ?? [])
        .filter(r => !quickQuery || r.name.toLowerCase().includes(quickQuery) || r.id.toLowerCase().includes(quickQuery))
        .slice(0, 8)
        .map(r => ({ value: r.id, label: r.name || r.id, hint: <span className="shrink-0 font-mono text-[10px] text-muted">{r.id}</span> }))

  const inputCls = 'w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  const labelCls = 'block text-[11px] text-muted'

  return (
    <Modal onClose={onClose} labelledBy="arena-create-title" size="md">
        <div className="modal-header flex items-center gap-2">
          <GitCompare className="h-4 w-4 text-accent" />
          <h3 id="arena-create-title" className="text-sm font-semibold text-foreground">批量创建对比账户</h3>
          <span className="min-w-0 flex-1 text-[10px] text-muted">同本金 · 同费率 · 同跟单参数 — 口径一致由构造保证</span>
          <button onClick={onClose} aria-label="关闭创建账户" className="modal-close"><X className="h-4 w-4" /></button>
        </div>
      <div className="modal-body">

        {/* 对比来源 */}
        <div className="mt-4 rounded-card border border-border bg-base/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex gap-1">
              {(['strategy', 'rule'] as const).map(k => (
                <button key={k} onClick={() => { setQuickKind(k); setQuickId('') }}
                  className={cn('rounded-btn border px-2 py-1 text-[11px] transition-colors',
                    quickKind === k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                  {k === 'strategy' ? '策略' : '监控规则'}
                </button>
              ))}
            </div>
            <div className="min-w-52 flex-1">
              <SuggestInput
                value={quickId}
                onChange={setQuickId}
                options={quickOptions}
                onSelect={opt => {
                  addRow({ name: opt.label, kind: quickKind, matchId: opt.value })
                  setQuickId('')
                }}
                loading={quickKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
                placeholder={quickKind === 'strategy' ? '搜索策略加入对比 (中文名 / ID)' : '搜索监控规则加入对比'}
                className="w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50"
              />
            </div>
            <button
              onClick={() => { const opt = quickOptions[0]; if (opt) { addRow({ name: opt.label, kind: quickKind, matchId: opt.value }); setQuickId('') } }}
              disabled={quickOptions.length === 0}
              className="rounded-btn bg-accent/10 px-2.5 py-1 text-[11px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-40"
            >
              添加
            </button>
            {quickKind === 'strategy' && (
              <button
                onClick={addAllStrategies}
                disabled={(strategiesQ.data?.presets ?? []).length === 0}
                className="rounded-btn border border-border px-2.5 py-1 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-40"
                title="把现有策略各开一个账户 (最多 20 个)"
              >
                全部策略一键加入 ({Math.min((strategiesQ.data?.presets ?? []).length, 20)})
              </button>
            )}
          </div>

          {rows.length === 0 ? (
            <div className="py-6 text-center text-[11px] text-muted">还没有来源 — 上面搜索添加, 或「全部策略一键加入」</div>
          ) : (
            <div className="mt-2 max-h-52 space-y-1 overflow-y-auto">
              {rows.map((r, i) => (
                <div key={r.matchId} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs hover:bg-elevated/40">
                  <span className="w-16 shrink-0 text-[10px] text-muted">{r.kind === 'strategy' ? '跟策略' : '跟规则'}</span>
                  <input
                    value={r.name}
                    onChange={e => setRows(prev => prev.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                    placeholder="账户名 (缺省用 ID)"
                    className="min-w-0 flex-1 rounded-btn border border-border bg-base px-2 py-1 text-xs outline-none focus:border-accent/50"
                  />
                  <span className="w-44 shrink-0 truncate font-mono text-[11px] text-muted" title={r.matchId}>{r.matchId}</span>
                  <button onClick={() => setRows(prev => prev.filter((_, j) => j !== i))} className="shrink-0 rounded p-0.5 text-muted hover:text-danger" title="移除">
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 共享规格 */}
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
          <div>
            <label className={labelCls}>每户初始资金 (元)</label>
            <input type="number" value={cash} onChange={e => setCash(e.target.value)} className={cn(inputCls, 'mt-1')} placeholder="200000" />
          </div>
          <div>
            <label className={labelCls}>账户名前缀 (可选)</label>
            <input value={prefix} onChange={e => setPrefix(e.target.value)} className={cn(inputCls, 'mt-1 font-sans')} placeholder="如: 第一期-" />
          </div>
          <div>
            <label className={labelCls}>方向 (全部来源)</label>
            <div className="mt-1 flex gap-1.5">
              {(['auto', 'buy', 'sell'] as const).map(sd => (
                <button key={sd} onClick={() => setSide(sd)} disabled={sd === 'auto' && hasMonitorSource}
                  aria-pressed={side === sd}
                  className={cn('flex-1 rounded-btn border py-1 text-[11px] font-medium transition-colors',
                    side === sd ? (sd === 'auto' ? 'border-accent/40 bg-accent/10 text-accent' : sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted',
                    'disabled:cursor-not-allowed disabled:opacity-40')}>
                  {AUTO_SIDE_LABEL[sd]}
                </button>
              ))}
            </div>
            <div className="mt-1 text-[10px] text-muted">
              {hasMonitorSource ? '包含监控规则，请选择固定买入或卖出方向。' : '自动：买入信号/入池买入，卖出信号/出池全平。'}
            </div>
          </div>
          <div>
            <label className={labelCls}>仓位模式 (全部来源)</label>
            <div className="mt-1 flex gap-1.5">
              <button onClick={() => setSizeMode('fixed_amount')}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'fixed_amount' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>固定金额</button>
              <button onClick={() => setSizeMode('pct_equity')}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'pct_equity' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>权益 %</button>
            </div>
          </div>
          <div>
            <label className={labelCls}>{side === 'auto' ? '买入' : '每笔'}{sizeMode === 'fixed_amount' ? '金额 (元)' : '占权益 %'}</label>
            <input type="number" value={sizeValue} onChange={e => setSizeValue(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div>
            <label className={labelCls}>订单类型</label>
            <div className="mt-1 flex gap-1">
              {(['next_open', 'close', 'market'] as const).map(t => (
                <button key={t} onClick={() => setOrderType(t)}
                  className={cn('flex-1 rounded-btn border py-1 text-[10px] transition-colors',
                    orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                  {ORDER_TYPE_LABEL[t]}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className={labelCls}>{side === 'auto' ? '买入冷却天数 (同标的)' : '冷却天数 (同标的)'}</label>
            <input type="number" min="0" step="1" value={cooldown} onChange={e => setCooldown(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div>
            <label htmlFor="arena-max-positions" className={labelCls}>最大持仓标的数 (0 不限)</label>
            <input id="arena-max-positions" type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)} className={cn(inputCls, 'mt-1')} />
            <div className="mt-1 text-[10px] text-muted">每户独立计数，含待成交买单；账户上限仍为 50 只。</div>
          </div>
          <div className="grid grid-cols-3 gap-2 md:col-span-2">
            <div>
              <label className={labelCls}>佣金 (万)</label>
              <input type="number" step="0.1" min="0" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
            <div>
              <label className={labelCls}>印花税 (千)</label>
              <input type="number" step="0.1" min="0" value={stampQian} onChange={e => setStampQian(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
            <div>
              <label className={labelCls}>滑点 (bps)</label>
              <input type="number" step="1" min="0" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
          </div>
        </div>

        {m.isError && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{String((m.error as Error).message)}</div>}
        <div className="mt-2 text-[10px] leading-relaxed text-muted">
          创建后各账户立即参与盘中自动跟单 (规则触发 → 下单 → T+1 约束), 每交易日盘后管道统一结算定版净值, 对比榜单自动更新。
        </div>
      </div>
        <div className="modal-footer flex items-center gap-2">
          <button
            onClick={() => valid && m.mutate()}
            disabled={!valid || m.isPending}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-40"
          >
            {m.isPending ? '创建中…' : `创建 ${rows.length || ''} 个账户`.replace('  ', ' ')}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">取消</button>
        </div>
    </Modal>
  )
}

export function Paper() {
  const [view, setView] = useState<'compare' | 'setup'>('compare')
  // 新建账户草稿 id: 非空 = 正在创建。点「单账户」只进入草稿态,
  // 取消/返回即丢弃, 不写任何持久状态。
  const [draftId, setDraftId] = useState<string | null>(null)

  if (view === 'compare') {
    return <CompareView onCreateSingle={() => { setDraftId(genAccountId()); setView('setup') }} />
  }
  return (
    <SetupView
      draftId={draftId}
      setDraftId={setDraftId}
      onBack={() => setView('compare')}
    />
  )
}

/** 新建单账户向导 (从对比页「单账户」进入; 返回/完成都回到对比页) */
function SetupView({ draftId, setDraftId, onBack }: {
  draftId: string | null
  setDraftId: (id: string | null) => void
  onBack: () => void
}) {
  const qc = useQueryClient()
  const accountsQ = useQuery({ queryKey: QK.paperAccounts, queryFn: api.paperAccounts })
  const accounts = accountsQ.data?.accounts ?? []
  const back = () => {
    setDraftId(null)
    onBack()
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="新建虚拟账户"
        subtitle="单账户创建 · 批量对比请用对比页的「批量创建账户」"
        right={
          <button
            onClick={back}
            className="flex items-center gap-1 rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
            title="返回多账户对比"
          >
            <GitCompare className="h-3 w-3" />
            返回对比
          </button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <SetupCard
          accId={draftId ?? 'default'}
          onDone={() => {
            setDraftId(null)
            qc.invalidateQueries({ queryKey: QK.paperAll })
            onBack()
          }}
          onCancel={accounts.length > 0 ? back : undefined}
        />
      </div>
    </div>
  )
}

/** 账户操作面板 (原单账户视图主体): 在对比榜单行展开内渲染, 免跳转。
 *  含总览卡/净值曲线/持仓/订单与成交/下单/跟单规则/费用设置/冻结。 */
function AccountPanel({ acc, name, onDeleted }: { acc: string; name?: string; onDeleted: () => void }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'orders' | 'trades'>('orders')
  const [feeOpen, setFeeOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const overviewQ = useQuery({ queryKey: QK.paperOverview(acc), queryFn: () => api.paperOverview(acc) })
  const ordersQ = useQuery({ queryKey: QK.paperOrders(acc), queryFn: () => api.paperOrders(undefined, acc) })
  const tradesQ = useQuery({ queryKey: QK.paperTrades(acc), queryFn: () => api.paperTrades(acc) })
  const navQ = useQuery({ queryKey: QK.paperNav(acc), queryFn: () => api.paperNav(acc) })
  const statsQ = useQuery({ queryKey: QK.paperStats(acc), queryFn: () => api.paperStats(acc) })
  const autoSkipsQ = useQuery({ queryKey: QK.paperAutoSkips(acc), queryFn: () => api.paperAutoSkips(acc) })

  // 'paper' 前缀兜底失效: 覆盖全部账户的全部查询 (订单变动可能影响净值/统计)
  const invalidateAll = () => qc.invalidateQueries({ queryKey: QK.paperAll })

  const cancelM = useMutation({
    mutationFn: (id: string) => api.paperOrderCancel(id, acc),
    onSuccess: invalidateAll,
  })
  const queueM = useMutation({
    mutationFn: (on: boolean) => api.paperSettings({ queue_limit_orders: on }, acc),
    onSuccess: invalidateAll,
  })
  const exportTradesM = useMutation({
    mutationFn: () => api.paperTradesAll(acc),
    onSuccess: fills => {
      if (fills.length > 0) downloadTradesCsv(acc, fills)
    },
    onError: error => toast(`成交台账导出失败 · ${String((error as Error).message || error)}`, 'error'),
  })
  const deleteM = useMutation({
    mutationFn: () => api.paperDeleteAccount(acc),
    onSuccess: async () => {
      onDeleted()
      await paperAccountChanged(qc, acc, 'deleted')
      toast('模拟账户及关联记录已删除', 'success')
    },
  })

  if (overviewQ.isLoading) {
    return <div className="py-8 text-center text-xs text-muted">加载中…</div>
  }
  // 请求失败与「未开户」分开呈现: 失败给错误态 + 重试, 不伪装成未初始化
  if (overviewQ.isError || !overviewQ.data) {
    return (
      <div className="flex flex-col items-center gap-2 py-8">
        <span className="text-xs text-secondary">账户数据加载失败，请重试</span>
        <button
          type="button"
          onClick={() => overviewQ.refetch()}
          disabled={overviewQ.isFetching}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-accent transition-colors hover:bg-elevated disabled:opacity-50"
        >
          重试
        </button>
      </div>
    )
  }
  const ov = overviewQ.data
  if (!ov?.initialized) {
    return <div className="py-8 text-center text-xs text-muted">账户未初始化</div>
  }

  const holdings = ov.holdings ?? []
  const orders = (ordersQ.data?.orders ?? []).filter(o => o.status === 'pending' || o.status === 'expired' || o.status === 'cancelled').slice(0, 20)
  const trades: PaperFill[] = (tradesQ.data?.fills ?? []).slice(0, 30)
  const nav = navQ.data?.nav ?? []
  const stats = statsQ.data
  const valuation = ov.valuation
  const autoSkips = autoSkipsQ.data?.skips ?? []
  const pnlPct = ov.initial_cash && ov.initial_cash > 0 ? ((ov.total_pnl ?? 0) / ov.initial_cash) * 100 : 0

  return (
    <div>
      {/* 面板头: 账户标识 + 状态徽章 + 费用 + 冻结 */}
      <div className="sticky left-0 mb-3 flex max-w-[calc(100vw-6rem)] flex-wrap items-center gap-2">
        <span className="min-w-0 max-w-full truncate text-sm font-medium" title={ov.account_name || name || acc}>{ov.account_name || name || acc}</span>
        <span className="font-mono text-[10px] text-muted">{acc}</span>
        {ov.status === 'frozen' && <span className="rounded-full bg-warning/15 px-2 py-0.5 text-[10px] text-warning">已冻结</span>}
        {ov.queue_limit_orders && (
          <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[10px] text-accent" title="触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)">
            涨跌停排队
          </span>
        )}
        <button
          onClick={() => setFeeOpen(true)}
          className="flex items-center gap-1 rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
          title={`佣金 ${((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ (最低${ov.fees?.min_commission ?? 5}元) · 印花税 ${ov.fees?.stamp_tax_mode === 'historical' ? '按卖出成交日期' : `${((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰ 仅卖出`} · 额外过户费 ${((ov.fees?.transfer_fee_pct ?? 0) * 10000).toFixed(2)}‱ 双边 · 滑点 ${ov.fees?.slippage_bps ?? 0}‱ — 点击调整`}
        >
          <Settings className="h-3 w-3" />
          修改账户
        </button>
        <button
          onClick={() => { deleteM.reset(); setDeleteOpen(true) }}
          className="flex items-center gap-1 rounded-btn border border-danger/30 px-2 py-0.5 text-[11px] text-danger transition-colors hover:bg-danger/10"
        >
          <Trash2 className="h-3 w-3" />删除账户
        </button>
        <button
          onClick={() => api.paperFreeze(ov.status !== 'frozen', acc).then(invalidateAll)}
          className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-warning/40 hover:text-warning"
        >
          {ov.status === 'frozen' ? '解冻账户' : '冻结账户'}
        </button>
      </div>
      <div>
        {/* 总览卡片 */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="总资产 (虚拟)" value={fmtMoney(ov.total)} icon={Wallet} iconCls="text-accent" />
          <StatCard label="现金" value={fmtMoney(ov.cash)} icon={Banknote} iconCls="text-muted" />
          <StatCard label="持仓市值" value={fmtMoney(ov.market_value)} icon={PieChart} iconCls="text-muted" />
          <StatCard
            label="累计盈亏"
            value={`${(ov.total_pnl ?? 0) >= 0 ? '+' : ''}${fmtMoney(ov.total_pnl)} (${fmtPct(pnlPct / 100)})`}
            valueClass={priceColorClass((ov.total_pnl ?? 0) / (ov.initial_cash || 1))}
            icon={TrendingUp}
            iconCls={priceColorClass((ov.total_pnl ?? 0) / (ov.initial_cash || 1))}
          />
        </div>

        {valuation && <ValuationBadge v={valuation} />}
        <PaperCorporateActions key={acc} account={acc} receivables={ov.receivables} />

        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[1fr_320px]">
          {/* 左列: 净值 + 持仓 + 流水 */}
          <div className="min-w-0 space-y-4">
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="text-sm font-medium">净值曲线 <span className="ml-1 text-[10px] text-muted">按交易日收盘定版</span></div>
              {nav.length > 0 ? <NavChart nav={nav} /> : <div className="py-10 text-center text-xs text-muted">暂无定版净值 — 交易日盘后管道自动结算</div>}
            </div>

            <div className="rounded-card border border-border bg-surface p-4">
              <div className="text-sm font-medium">当前持仓</div>
              {holdings.length === 0 ? (
                <div className="py-8 text-center text-xs text-muted">暂无持仓 — 右侧下单或等自动跟单触发</div>
              ) : (
                <table className="mt-2 w-full text-xs">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="text-left text-[10px] text-muted">
                      <th className="py-1.5 font-normal">代码</th>
                      <th className="py-1.5 text-right font-normal">数量</th>
                      <th className="py-1.5 text-right font-normal">可卖(T+1)</th>
                      <th className="py-1.5 text-right font-normal">成本</th>
                      <th className="py-1.5 text-right font-normal">现价</th>
                      <th className="py-1.5 text-right font-normal">市值</th>
                      <th className="py-1.5 text-right font-normal">盈亏</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {holdings.map(h => (
                      <tr key={h.symbol} className="border-t border-border/50 transition-colors hover:bg-elevated/40">
                        <td className="py-1.5 font-sans">{h.symbol}</td>
                        <td className="py-1.5 text-right">{h.qty}</td>
                        <td className="py-1.5 text-right text-muted">{h.available_qty}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.avg_cost, 3)}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.last_price, 3)}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.market_value, 0)}</td>
                        <td className={cn('py-1.5 text-right', priceColorClass(h.pnl))}>
                          {(h.pnl >= 0 ? '+' : '') + fmtMoney(h.pnl, 0)} ({fmtPct(h.pnl_pct / 100)})
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* 订单 / 成交流水 */}
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="flex items-center gap-2">
                {(['orders', 'trades'] as const).map(t => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={cn('rounded-btn px-2.5 py-1 text-xs transition-colors', tab === t ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                  >
                    {t === 'orders' ? '订单' : '成交台账'}
                  </button>
                ))}
                {tab === 'trades' && (
                  <button
                    onClick={() => exportTradesM.mutate()}
                    disabled={(tradesQ.data?.total ?? 0) === 0 || exportTradesM.isPending}
                    className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
                    title="导出全部成交台账 (CSV, Excel 可直接打开)"
                  >
                    <Download className="h-3 w-3" />
                    {exportTradesM.isPending ? '导出中' : '导出 CSV'}
                  </button>
                )}
                {stats && (
                  <span className="ml-auto text-[11px] text-muted" title="配对片段为 FIFO 卖出与买入批次的配对; 持仓周期为从零建仓到清零的完整周期 (收益率资金加权); 回撤按定版净值序列">
                    配对 {stats.rounds} · 胜率 {stats.win_rate}% · 盈亏比 {stats.profit_loss_ratio ?? '--'} · 均持 {stats.avg_holding_days}天 · 回撤{' '}
                    {stats.max_drawdown != null ? `-${stats.max_drawdown}%` : '--'} ·{' '}
                    <span className={cn('font-mono', priceColorClass(stats.realized_pnl))}>
                      已实现 {stats.realized_pnl >= 0 ? '+' : ''}{fmtMoney(stats.realized_pnl, 0)}
                    </span>
                    {stats.cycles && (
                      <span className="ml-1 border-l border-border/60 pl-1">
                        周期 {stats.cycles.count} · 周期胜率 {stats.cycles.win_rate}% · 资金加权{' '}
                        {stats.cycles.money_weighted_return_pct != null ? `${stats.cycles.money_weighted_return_pct}%` : '--'}
                      </span>
                    )}
                  </span>
                )}
              </div>
              {tab === 'orders' ? (
                <div className="mt-2 space-y-1">
                  {orders.length === 0 && <div className="py-6 text-center text-xs text-muted">暂无订单</div>}
                  {orders.map((o: PaperOrder) => (
                    <div key={o.id} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs hover:bg-elevated/50">
                      <span className={cn('w-8 shrink-0 font-medium', o.side === 'buy' ? 'text-bull' : 'text-bear')}>
                        {o.side === 'buy' ? '买入' : '卖出'}
                      </span>
                      <span className="w-24 shrink-0 font-mono">{o.symbol}</span>
                      <span className="w-24 shrink-0 font-mono text-right" title={o.filled_qty != null ? `委托${o.qty}股，实际成交${o.filled_qty}股` : undefined}>
                        {o.filled_qty != null ? `${o.filled_qty}/${o.qty}` : o.qty}
                      </span>
                      <span className="w-16 shrink-0 text-muted">{ORDER_TYPE_LABEL[o.order_type]}</span>
                      <span className={cn(
                        'w-16 shrink-0 rounded-full px-1.5 py-px text-center text-[10px] leading-4',
                        o.status === 'filled' ? 'bg-bear/10 text-bear'
                          : o.status === 'expired' ? 'bg-warning/10 text-warning'
                          : o.status === 'cancelled' ? 'bg-elevated text-muted'
                          : 'bg-accent/10 text-accent',
                      )}>
                        {STATUS_LABEL[o.status]}
                      </span>
                      {o.status === 'pending' && o.postponed > 0 && (
                        <span className="shrink-0 rounded-full bg-accent/10 px-1.5 py-px text-[10px] leading-4 text-accent" title={`涨跌停排队重试中, 已顺延 ${o.postponed}/${3} 日`}>
                          排队 {o.postponed}/3
                        </span>
                      )}
                      <span className="min-w-0 flex-1 truncate text-[11px] text-muted" title={o.reason ?? undefined}>
                        {o.status === 'filled' && o.fill_price != null ? `@ ${fmtMoney(o.fill_price, 3)}` : o.reason ?? ''}
                      </span>
                      {o.status === 'pending' && (
                        <button onClick={() => cancelM.mutate(o.id)} className="shrink-0 rounded p-0.5 text-muted hover:text-foreground" title="撤单">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2 space-y-1">
                  {trades.length === 0 && <div className="py-6 text-center text-xs text-muted">暂无成交</div>}
                  {trades.map((f: PaperFill) => (
                    <div key={`${f.seq}-${f.order_id ?? f.action_id ?? 'corp'}-${f.stage ?? ''}`} className="flex items-center gap-2 rounded-btn px-2 py-1.5 font-mono text-xs hover:bg-elevated/50">
                      <span className="w-14 shrink-0 text-muted">{f.date}</span>
                      {f.kind === 'corporate_action' ? (
                        <span className="text-accent">{f.symbol} {f.label} {f.qty ? `${f.qty}股 ` : ''}{f.cash_amount ? `¥${f.cash_amount.toFixed(2)}` : ''}</span>
                      ) : f.kind === 'corp_action' ? (
                        <span className="text-accent">除权 ×{f.factor} ({f.symbol}: {f.qty_before} → )</span>
                      ) : (
                        <>
                          <span className={cn('w-8 shrink-0 font-sans font-medium', f.side === 'buy' ? 'text-bull' : 'text-bear')}>
                            {f.side === 'buy' ? '买入' : '卖出'}
                          </span>
                          <span className="w-24 shrink-0">{f.symbol}</span>
                          <span className="w-16 shrink-0 text-right">{f.qty}</span>
                          <span className="w-20 shrink-0 text-right">@ {fmtMoney(f.price, 3)}</span>
                          <span className="w-16 shrink-0 text-right text-muted">费 {fmtMoney(f.fee)}</span>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* 右列: 下单 + 自动跟单 + 策略对比 */}
          <div className="space-y-4">
            <OrderForm acc={acc} onDone={invalidateAll} />
            <AutoRulesPanel key={acc} acc={acc} />
            <AutoSkipsPanel skips={autoSkips} />
            <CandidateCompareCard paper={{
              total_return_pct: nav.length >= 2 && nav[0].nav > 0 ? (nav[nav.length - 1].nav / nav[0].nav - 1) * 100 : null,
              annual_pct: nav.length >= 2 && nav[0].nav > 0
                ? (Math.pow(nav[nav.length - 1].nav / nav[0].nav, 252 / nav.length) - 1) * 100
                : null,
              max_drawdown: stats?.max_drawdown ?? null,
              win_rate: stats?.win_rate ?? null,
              n_trades: tradesQ.data?.execution_total ?? 0,
              profit_loss_ratio: stats?.profit_loss_ratio ?? null,
            }} />
            <div className="rounded-card border border-border/60 bg-base/40 p-3 text-[11px] leading-relaxed text-muted">
              <div className="font-medium text-secondary">撮合口径</div>
              <div className="mt-1">· 即时单: 交易时段按最新快照价 + 滑点成交</div>
              <div>· 次日开盘 / 当日收盘单: 盘后管道按真实开盘/收盘价成交</div>
              <div>· T+1: 当日买入次一交易日可卖</div>
              <div>· 停牌顺延 3 日自动过期; 除权按因子调整数量与成本, 台账留痕</div>
              <div className="mt-2 flex items-center justify-between border-t border-border/40 pt-2">
                <span title="开启后, 触及涨停的买入/触及跌停的卖出不再直接过期, 转次日开盘重试 (最多顺延 3 日)">
                  涨跌停排队次日重试
                </span>
                <button
                  onClick={() => queueM.mutate(!ov.queue_limit_orders)}
                  disabled={queueM.isPending}
                  className={cn(
                    'relative h-4 w-8 shrink-0 rounded-full transition-colors',
                    ov.queue_limit_orders ? 'bg-accent' : 'bg-border',
                  )}
                  aria-label="涨跌停排队次日重试开关"
                >
                  <span className={cn(
                    'absolute top-0.5 h-3 w-3 rounded-full bg-white shadow transition-all',
                    ov.queue_limit_orders ? 'left-[18px]' : 'left-0.5',
                  )} />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
      {feeOpen && ov.fees && (
        <AccountEditModal
          accId={acc}
          name={ov.account_name || name || acc}
          initialCash={ov.initial_cash ?? 0}
          capitalEditable={ov.initial_cash_editable === true}
          fees={ov.fees}
          queue={!!ov.queue_limit_orders}
          onSaved={() => setFeeOpen(false)}
          onClose={() => setFeeOpen(false)}
        />
      )}
      {deleteOpen && (
        <ConfirmDialog
          title="删除模拟账户"
          confirmText="确认删除"
          danger
          pending={deleteM.isPending}
          onClose={() => setDeleteOpen(false)}
          onConfirm={() => deleteM.mutate()}
        >
          <p>确认删除“{ov.account_name || name || acc}”？删除后无法恢复。</p>
          <p className="mt-2">该账户的委托、成交与权益台账、持仓、净值、自动跟单规则、退出待办、风控记录和关联提醒将同步删除。共享策略、监控规则及其他账户保留。</p>
          {deleteM.isError && <p role="alert" className="mt-2 text-danger">{(deleteM.error as Error).message}</p>}
        </ConfirmDialog>
      )}
    </div>
  )
}
