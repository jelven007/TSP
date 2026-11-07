import { useState, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Settings2, RotateCcw, Save, ChevronDown, Filter, Star, TrendingUp, Sparkles, Download, Layers, Plus, Trash2 } from 'lucide-react'
import { api, type StrategyDetail, type StrategyParamDef, type CompositeChildInfo, type ScoringDirection, type CustomSignalCondition, type EntryRiskPolicy } from '@/lib/api'
import { toPercentages, normalizeWeights } from '@/lib/weights'
import { BUILTIN_COLUMNS } from '@/lib/watchlist-columns'
import { QK } from '@/lib/queryKeys'
import { SignalPicker } from './SignalPicker'
import { SignalTriggerActions } from '@/components/signals/SignalTriggerActions'
import { Modal } from '@/components/Modal'
import { ScoringEditor } from '@/components/ScoringEditor'
import { OverlayFilterEditor } from './OverlayFilterEditor'
import { StrategyParamGroups } from '@/components/StrategyParamGroups'
import { applyParamValue } from '@/lib/strategyParams'
import { OvernightExecutionSummary } from '@/components/OvernightExecutionSummary'
import { BasicFilterEditor, ALL_BOARDS } from './BasicFilterEditor'
import { validateBasicFilter } from '@/lib/strategyBasicFilter'
import { StockScopeEditor } from './StockScopeEditor'
export { RangeField, ALL_BOARDS } from './BasicFilterEditor'

// 内置列名 → 中文标签
const FIELD_LABEL: Record<string, string> = {}
for (const c of BUILTIN_COLUMNS) {
  if (c.source.type === 'builtin') FIELD_LABEL[c.source.key] = c.label
}
// enriched 列名别名
Object.assign(FIELD_LABEL, {
  change_pct: '涨跌幅', consecutive_limit_ups: '连板',
  momentum_60d: '60D动量', turnover_rate: '换手率',
  rsi_14: 'RSI14', rsi_6: 'RSI6', rsi_24: 'RSI24',
  vol_ratio_5d: '量比', vol_ratio_20d: '20日量比',
  macd_dif: 'MACD-DIF', macd_dea: 'MACD-DEA', macd_hist: 'MACD柱',
  boll_upper: '布林上轨', boll_lower: '布林下轨',
  ma20_bias: 'MA20乖离率',
})

interface Props {
  strategyId: string | null
  onClose: () => void
  onSaved?: (displayLimit: number | null) => void
  onAiModify?: () => void
  onDeleted?: () => void
}

// ===== 可折叠区域 =====
function Section({ icon: Icon, title, accent, defaultOpen = true, children, extra }: {
  icon?: React.ComponentType<{ className?: string }>
  title: string
  accent?: string
  defaultOpen?: boolean
  children: React.ReactNode
  extra?: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-xl border border-border/15 bg-surface/20 overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 py-2 hover:bg-surface/30 transition-colors">
        <button
          onClick={() => setOpen(v => !v)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left cursor-pointer"
        >
          <ChevronDown className={`h-3 w-3 text-muted/40 transition-transform duration-200 ${open ? '' : '-rotate-90'}`} />
          {Icon && <Icon className={`h-3.5 w-3.5 ${accent ?? 'text-muted'}`} />}
          <span className="text-[11px] font-medium text-foreground/70">{title}</span>
        </button>
        {extra && <div className="ml-auto flex items-center gap-1">{extra}</div>}
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div className="px-3.5 pb-3 pt-0.5 space-y-2">
              {children}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


function initializeTargets(bf: Record<string, any>) {
  if (!bf.target_mode) {
    const legacyBoards = bf.boards as string[] | undefined
    bf.target_mode = bf.enabled !== false && legacyBoards?.length && legacyBoards.length < ALL_BOARDS.length ? 'selected' : 'all'
    bf.boards = bf.target_mode === 'selected' ? legacyBoards : []
    bf.include_custom = false
    bf.watchlist_group_ids = []
    bf.exclude_st = bf.enabled !== false && !!bf.exclude_st
  }
  if (bf.target_mode === 'all') bf.boards = [...ALL_BOARDS]
  bf.target_mode = 'selected'
  return bf
}

// 策略参数字段
export function ParamField({ def, value, onChange }: {
  def: StrategyParamDef
  value: any
  onChange: (v: any) => void
}) {
  if (def.type === 'bool') {
    const checked = value === true || value === 'true' || value === 'True'
    return (
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-secondary w-40 shrink-0 text-right">{def.label}</span>
        <button
          type="button"
          onClick={() => onChange(!checked)}
          className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${
            checked ? 'bg-accent' : 'bg-elevated'
          }`}
          aria-pressed={checked}
          aria-label={def.label}
        >
          <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[14px]' : 'translate-x-0.5'
          }`} />
        </button>
      </div>
    )
  }
  if (def.type === 'select' && def.options) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-secondary w-40 shrink-0 text-right">{def.label}</span>
        <select
          aria-label={def.label}
          value={value ?? def.default}
          onChange={e => onChange(e.target.value)}
          className="flex-1 min-w-0 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground focus:outline-none focus:border-accent/50"
        >
          {def.options.map(o => <option key={o} value={o}>{def.option_labels?.[o] ?? o}</option>)}
        </select>
      </div>
    )
  }
  if (def.type === 'string') {
    return (
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-secondary w-40 shrink-0 text-right">{def.label}</span>
        <input
          type="text"
          aria-label={def.label}
          value={value ?? def.default ?? ''}
          onChange={e => onChange(e.target.value)}
          className="flex-1 min-w-0 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground focus:outline-none focus:border-accent/50"
        />
      </div>
    )
  }
  const scale = def.display_scale ?? 1
  const displayed = Number((Number(value ?? def.default) * scale).toPrecision(12))
  return (
    <div className="flex items-center gap-2">
      <span className="text-[11px] text-secondary w-40 shrink-0 text-right">{def.label}</span>
      <input
        type="number"
        aria-label={def.label}
        value={displayed}
        onChange={e => onChange(e.target.value === '' ? def.default : Number(e.target.value) / scale)}
        step={def.step != null ? def.step * scale : 0.1}
        min={def.min != null ? def.min * scale : undefined}
        max={def.max != null ? def.max * scale : undefined}
        className="w-20 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
      />
      {def.unit && <span className="text-[10px] text-muted">{def.unit}</span>}
      {def.min != null && def.max != null && (
        <span className="text-[10px] text-muted">{def.min * scale}~{def.max * scale}</span>
      )}
    </div>
  )
}

function PercentField({ label, value, onChange, max = 50, min = 0 }: {
  label: string
  value: number | null
  onChange: (value: number | null) => void
  max?: number
  min?: number
}) {
  return <label className="flex items-center gap-2 text-xs text-secondary">
    <span className="min-w-0 flex-1">{label}</span>
    <input type="number" aria-label={label} value={value == null ? '' : Number((Math.abs(value) * 100).toPrecision(12))}
      placeholder="关闭" min={min} max={max} step={0.5}
      onChange={e => onChange(e.target.value === '' ? null : Number(e.target.value) / 100)}
      className="w-16 px-1.5 py-1 rounded bg-base border border-border text-xs font-mono text-foreground text-center" />
    <span className="text-[10px] text-muted">%</span>
  </label>
}

export function StrategySettingsDialog({ strategyId, onClose, onSaved, onAiModify, onDeleted }: Props) {
  const qc = useQueryClient()
  const [detail, setDetail] = useState<StrategyDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [restored, setRestored] = useState(false)

  // 编辑状态
  const [strategyName, setStrategyName] = useState('')
  const [strategyDesc, setStrategyDesc] = useState('')
  const [basicFilter, setBasicFilter] = useState<Record<string, any>>({})
  const [params, setParams] = useState<Record<string, any>>({})
  const [scoring, setScoring] = useState<Record<string, number>>({})
  const [scoringDirections, setScoringDirections] = useState<Record<string, ScoringDirection>>({})
  const [overlayFilter, setOverlayFilter] = useState<CustomSignalCondition[]>([])
  const [saveError, setSaveError] = useState('')
  const [stopLoss, setStopLoss] = useState<number | null>(null)
  const [takeProfit, setTakeProfit] = useState<number | null>(null)
  const [trailingStop, setTrailingStop] = useState<number | null>(null)
  const [trailingActivate, setTrailingActivate] = useState<number | null>(null)
  const [trailingDrawdown, setTrailingDrawdown] = useState<number | null>(null)
  const [entryRisk, setEntryRisk] = useState<EntryRiskPolicy>({})
  const [maxHoldDays, setMaxHoldDays] = useState<number | null>(null)
  const [entrySignals, setEntrySignals] = useState<string[]>([])
  const [exitSignals, setExitSignals] = useState<string[]>([])
  const [displayLimit, setDisplayLimit] = useState<number | null>(null)
  // 叠加策略: 子策略列表与权重(composite 专属, 编辑权重后随 override 保存)
  const [compositeChildren, setCompositeChildren] = useState<CompositeChildInfo[]>([])
  // 点击子策略名打开其配置编辑(composite 专属; 子策略必非 composite, 不会再嵌套)
  const [editingChildId, setEditingChildId] = useState<string | null>(null)
  // 可选子策略列表 + 添加面板开关(composite 设置用)
  const [allStrategies, setAllStrategies] = useState<{ id: string; name: string; source?: string }[]>([])
  const [showAddChild, setShowAddChild] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteError, setDeleteError] = useState('')

  const applyDraft = useCallback((d: StrategyDetail) => {
    setStrategyName(d.name ?? '')
    setStrategyDesc(d.description ?? '')
    const bf = initializeTargets({ ...d.basic_filter })
    setBasicFilter(d.source === 'composite' ? bf : { ...bf, enabled: true })
    setParams({ ...d.params_defaults })
    setScoring({ ...d.scoring })
    setScoringDirections({ ...d.scoring_directions })
    setOverlayFilter((d.overlay_filter ?? []).map(c => ({ ...c })))
    setStopLoss(d.stop_loss ?? null)
    setTakeProfit(d.take_profit ?? null)
    setTrailingStop(d.trailing_stop ?? null)
    setTrailingActivate(d.trailing_take_profit_activate ?? null)
    setTrailingDrawdown(d.trailing_take_profit_drawdown ?? null)
    setEntryRisk({ ...d.entry_risk })
    setMaxHoldDays(d.max_hold_days ?? null)
    setEntrySignals([...(d.entry_signals ?? [])])
    setExitSignals([...(d.exit_signals ?? [])])
    setDisplayLimit(d.display_limit ?? null)
    const list = d.composite_children ?? []
    const pcts = toPercentages(list.map(c => c.weight))
    setCompositeChildren(list.map((c, i) => ({ ...c, weight: pcts[i] })))
  }, [])

  // 加载策略详情
  useEffect(() => {
    let cancelled = false
    setDetail(null)
    if (!strategyId) return
    setEditingChildId(null)
    setLoading(true)
    setSaveError('')
    setRestored(false)
    setShowDeleteConfirm(false)
    setShowAddChild(false)
    setAllStrategies([])
    api.strategyGet(strategyId)
      .then(d => {
        if (cancelled) return
        setDetail(d)
        applyDraft(d)
        // composite 策略: 加载全部可选子策略(排除自身和其他 composite)供添加
        if (d.source === 'composite') {
          api.screenerStrategies().then(data => {
            if (!cancelled) setAllStrategies((data.presets ?? []).filter(s => s.id !== strategyId && s.source !== 'composite'))
          }).catch(() => { if (!cancelled) setAllStrategies([]) })
        }
      })
      .catch(() => { if (!cancelled) setDetail(null) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [strategyId, applyDraft])

  // 叠加策略: 滑块百分比口径, 允许总和 ≠100, 保存时自动按比例归一
  const compositeTotal = compositeChildren.reduce((s, c) => s + (c.weight || 0), 0)
  const removeCompositeChild = (id: string) => {
    setCompositeChildren(prev => prev.filter(c => c.id !== id))
  }
  const addCompositeChild = (s: { id: string; name: string; source?: string }) => {
    // 首个子策略独占 100%, 后续默认 10% (与因子编辑口径一致)
    setCompositeChildren(prev => [...prev, { id: s.id, name: s.name, source: s.source ?? '', weight: prev.length === 0 ? 100 : 10 }])
    setShowAddChild(false)
  }

  // 保存
  const handleSave = async () => {
    if (!strategyId || !detail || loading || saving || detail.id !== strategyId) return
    const problem = validateBasicFilter(basicFilter)
    if (problem) { setSaveError(problem); return }
    if (trailingActivate != null && trailingDrawdown != null && Math.abs(trailingDrawdown) > Math.abs(trailingActivate)) {
      setSaveError('移动止盈回撤不能大于启动涨幅'); return
    }
    setSaving(true)
    setSaveError('')
    try {
      await api.strategySaveConfig(strategyId, {
        name: strategyName,
        description: strategyDesc,
        basic_filter: { ...basicFilter, enabled: basicFilter.enabled !== false, data_reference: 'previous_trade_day' },
        params,
        overlay_filter: overlayFilter,
        ...(detail?.source !== 'composite' ? {
          scoring,
          scoring_directions: scoringDirections,
          scoring_replace: true,
        } : {}),
        stop_loss: stopLoss,
        take_profit: takeProfit,
        trailing_stop: trailingStop,
        trailing_take_profit_activate: trailingActivate,
        trailing_take_profit_drawdown: trailingDrawdown,
        entry_risk: entryRisk,
        max_hold_days: maxHoldDays,
        entry_signals: entrySignals,
        exit_signals: exitSignals,
        display_limit: displayLimit,
        // 叠加策略: 子策略权重(composite 专属, 走 override.children 持久化)
        ...(detail?.source === 'composite'
          ? { children: (() => {
              // 滑块百分比 → 归一小数权重再持久化
              const normalized = normalizeWeights(compositeChildren.map(c => c.weight))
              return compositeChildren.map((c, i) => ({ strategy_id: c.id, weight: normalized[i] }))
            })() }
          : {}),
      })
      await qc.invalidateQueries({ queryKey: QK.watchlistGroups })
      await qc.invalidateQueries({ queryKey: ['screener-strategies'] })
      await qc.invalidateQueries({ queryKey: ['strategies'] })
      onSaved?.(displayLimit)
      onClose()
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : '保存失败，请稍后重试')
    } finally {
      setSaving(false)
    }
  }

  // 重置
  const handleReset = () => {
    if (!detail?.definition_defaults) return
    applyDraft({ ...detail, ...detail.definition_defaults })
    setRestored(true)
    setSaveError('')
  }

  const handleDelete = async () => {
    if (!strategyId) return
    setDeleting(true)
    setDeleteError('')
    try {
      await api.strategyDelete(strategyId)
      const removed = {
        predicate: (query: { queryKey: readonly unknown[] }) => {
          const key = query.queryKey
          return ((key[0] === QK.screenerHistory(strategyId)[0]
            || key[0] === QK.screenerHistoryDetail(strategyId, '')[0]
            || key[0] === QK.strategyDetail(strategyId)[0]) && key[1] === strategyId)
            || (key[0] === QK.screenerCached()[0] && key[1] === 'strategy' && key[2] === strategyId)
        },
      }
      await qc.cancelQueries(removed)
      qc.removeQueries(removed)
      await Promise.all([
        ['screener-strategies'], ['strategies'], QK.strategyLinkOptions(),
        QK.watchlistStrategyOptions(), QK.watchlistGroups, QK.researchCandidates, ['research-jobs'],
      ].map(queryKey => qc.invalidateQueries({ queryKey })))
      await qc.invalidateQueries({ predicate: query =>
        query.queryKey[0] === QK.screenerCached()[0] && query.queryKey[1] !== 'strategy',
      })
      onDeleted?.()
      onClose()
      setShowDeleteConfirm(false)
    } catch (e: any) {
      // request() 已弹 toast, 这里再在确认弹窗内显式提示, 并保持弹窗打开让用户知晓删除失败。
      setDeleteError(String(e?.message ?? '删除失败,请重试'))
    } finally { setDeleting(false) }
  }

  const handleDownload = async () => {
    if (!strategyId || !detail || (detail.source !== 'ai' && detail.source !== 'custom')) return
    const src = await api.strategyGetSource(strategyId)
    const blob = new Blob([src.code], { type: 'text/x-python;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${strategyId}.py`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  if (!strategyId) return null

  return (
    <>
    <Modal
      onClose={onClose}
      closeDisabled={saving}
      labelledBy="strategy-settings-title"
      size="xl"
    >
          {/* 标题 */}
          <div className="modal-header flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <Settings2 className="h-4 w-4 text-accent" />
              <span id="strategy-settings-title" className="text-sm font-semibold text-foreground">{detail?.name ?? strategyId}</span>
              {detail && <span className="text-[10px] px-1.5 py-0.5 rounded bg-elevated text-muted">{{ builtin: '内置', custom: '自定义', ai: 'AI', composite: '叠加' }[detail.source] ?? detail.source}</span>}
              <span className="text-[10px] text-muted/40 font-mono">{strategyId}</span>
            </div>
            <div className="flex items-center gap-2">
              {detail && (detail.source === 'ai' || detail.source === 'custom') && (
                <button
                  aria-label="下载策略"
                  title="下载策略"
                  onClick={handleDownload}
                  className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-lg border border-border/60 bg-surface text-xs text-secondary hover:text-accent hover:border-accent/30 transition-colors cursor-pointer"
                >
                  <Download className="h-3.5 w-3.5" />
                  下载策略
                </button>
              )}
              <button aria-label="关闭" disabled={saving} onClick={onClose} className="modal-close"><X className="h-4 w-4 text-muted" /></button>
            </div>
          </div>

          {/* 内容 */}
          <div className="modal-body flex-1 overflow-y-auto px-5 py-5 space-y-5">
            {loading ? (
              <div className="flex items-center justify-center py-16"><div className="w-6 h-6 border-2 border-accent/30 border-t-accent rounded-full animate-spin" /></div>
            ) : detail ? (
              <>
                <p className="text-xs text-muted leading-5">
                  当前策略设置：显示原始定义与已保存修改合并后的值；新建策略模板不影响这里。
                  保存后用于下次运行，历史选股和回测结果保留。
                </p>
                {restored && <p role="status" className="text-xs text-accent">已将策略原始值载入草稿，点击保存设置后生效；关闭可取消。</p>}
                {/* 名称 + 描述 + 显示上限 */}
                <div className="flex flex-wrap items-end gap-4">
                  <div className="flex-1 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-muted/50 uppercase tracking-wider w-8 shrink-0">名称</span>
                      <input type="text" value={strategyName} onChange={e => setStrategyName(e.target.value)}
                        className="flex-1 h-8 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm font-medium text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow" />
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-muted/50 uppercase tracking-wider w-8 shrink-0">描述</span>
                      <input type="text" value={strategyDesc} onChange={e => setStrategyDesc(e.target.value)}
                        className="flex-1 h-8 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow" />
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 pb-0.5 shrink-0">
                    <span className="text-[10px] text-muted/50">显示上限</span>
                    <input type="number" value={displayLimit ?? ''} onChange={e => setDisplayLimit(e.target.value ? Number(e.target.value) : null)} step={1} min={10} max={200} placeholder="不限"
                      className="w-14 h-8 px-1.5 rounded-lg bg-base border border-border/40 text-xs font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                    <span className="text-[10px] text-muted/50">只</span>
                  </div>
                </div>

                {detail.asset_types?.includes('stock') !== false && (
                  <Section icon={Filter} title="股票范围" accent="text-sky-400">
                    <StockScopeEditor value={basicFilter}
                      onChange={next => setBasicFilter(prev => ({ ...prev, ...next }))} />
                  </Section>
                )}

                {detail.source === 'composite' && (
                  <Section icon={Filter} title="入场叠加条件" accent="text-accent">
                    <OverlayFilterEditor value={overlayFilter} onChange={setOverlayFilter} numericOnly />
                  </Section>
                )}

                {/* 叠加策略: 子策略列表 + 权重(替换三列参数, composite 专属) */}
                {detail.source === 'composite' ? (() => {
                  const SRC_LABEL: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI' }
                  const SRC_CLS: Record<string, string> = {
                    builtin: 'border-accent/25 bg-accent/10 text-accent',
                    custom: 'border-amber-400/25 bg-amber-400/10 text-amber-400',
                    ai: 'border-purple-500/25 bg-purple-500/10 text-purple-400',
                  }
                  const selectedIds = new Set(compositeChildren.map(c => c.id))
                  const candidates = allStrategies.filter(s => !selectedIds.has(s.id))
                  return (
                  <div className="rounded-xl border border-teal-500/20 bg-teal-500/5 p-4 space-y-3">
                    <div className="flex items-center gap-2">
                      <Layers className="h-4 w-4 text-teal-400" />
                      <span className="text-sm font-medium text-foreground">子策略与权重</span>
                      <span className="text-[10px] text-muted flex items-center gap-1.5">
                        共 {compositeChildren.length} 个 · 权重
                        <span className={`font-mono ${compositeChildren.length > 0 && compositeTotal !== 100 ? 'text-amber-400' : 'text-emerald-400'}`}>
                          {compositeTotal}%
                        </span>
                        {compositeChildren.length > 0 && compositeTotal !== 100 && (
                          <span className="text-amber-400/60">(保存时自动按比例归一)</span>
                        )}
                      </span>
                      <button onClick={() => setShowAddChild(v => !v)} className="ml-auto inline-flex items-center gap-1 h-6 px-2 rounded-lg border border-teal-500/30 bg-teal-500/10 text-[11px] text-teal-400 hover:bg-teal-500/20">
                        <Plus className="h-3 w-3" />添加
                      </button>
                    </div>
                    {/* 添加子策略面板 */}
                    {showAddChild && (
                      <div className="rounded-lg border border-border bg-base/60 p-2 space-y-1 max-h-48 overflow-y-auto">
                        {candidates.length === 0 ? (
                          <div className="text-[11px] text-muted py-2 text-center">无可添加的策略</div>
                        ) : candidates.map(s => (
                          <button key={s.id} onClick={() => addCompositeChild(s)} className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left hover:bg-teal-500/10">
                            <Plus className="h-3 w-3 shrink-0 text-teal-400" />
                            <span className="flex-1 truncate text-xs text-foreground">{s.name}</span>
                            {s.source && (
                              <span className={`rounded border px-1 text-[8px] ${SRC_CLS[s.source] ?? ''}`}>{SRC_LABEL[s.source] ?? s.source}</span>
                            )}
                          </button>
                        ))}
                      </div>
                    )}
                    {compositeChildren.length === 0 ? (
                      <div className="text-xs text-muted py-4 text-center">暂无子策略, 点击"添加"选择</div>
                    ) : (
                      <div className="space-y-1.5">
                        {compositeChildren.map((c, i) => (
                          <div key={c.id} className="flex items-center gap-2 rounded-lg bg-base/60 px-3 py-2">
                            <span className="text-[10px] text-muted/50 font-mono w-5">{i + 1}</span>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-1.5">
                                <button
                                  type="button"
                                  onClick={() => setEditingChildId(c.id)}
                                  title="点击编辑该子策略的配置"
                                  className="truncate text-left text-xs font-medium text-foreground transition-colors hover:text-accent cursor-pointer"
                                >
                                  {c.name || c.id}
                                </button>
                                {c.source && (
                                  <span className={`rounded border px-1 text-[8px] shrink-0 ${SRC_CLS[c.source] ?? ''}`}>{SRC_LABEL[c.source] ?? c.source}</span>
                                )}
                              </div>
                              <div className="text-[10px] text-muted/50 font-mono">{c.id}</div>
                            </div>
                            <div className="flex items-center gap-1.5 shrink-0">
                              <input
                                type="range"
                                min={0}
                                max={100}
                                step={1}
                                value={c.weight}
                                onChange={e => setCompositeChildren(prev => prev.map((p, j) => j === i ? { ...p, weight: parseInt(e.target.value) || 0 } : p))}
                                className="h-1 w-24 cursor-pointer accent-teal-400"
                                aria-label={`${c.name || c.id}权重`}
                              />
                              <span className="w-9 text-right font-mono text-[10px] text-muted">{Math.round(c.weight)}%</span>
                              <button onClick={() => removeCompositeChild(c.id)} className="text-danger/50 hover:text-danger p-1">
                                <Trash2 className="h-3 w-3" />
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="text-[10px] text-muted/60 pt-1 border-t border-border/30">
                      提示: 权重按相对比例生效, 保存时自动归一; 修改后点底部"保存设置"生效。
                    </div>
                  </div>
                  )
                })()
                : (
                <div className="grid grid-cols-1 md:grid-cols-3 gap-5 items-start">
                  {/* 列1：选股条件 */}
                    <Section icon={Filter} title="数值过滤" accent="text-sky-400">
                      <BasicFilterEditor value={basicFilter} disabled={saving} alwaysEnabled onChange={setBasicFilter} />
                  </Section>

                  {/* 列2：策略参数 */}
                  <div className="space-y-3">
                    {(detail.execution_backend !== 'minute_filter' || !detail.minute_execution) && (
                      <Section icon={Filter} title="入场叠加条件" accent="text-accent">
                        {detail.execution_backend === 'minute_filter' && (
                          <p className="text-[10px] leading-4 text-muted">
                            日线条件按前交易日的已完成日K计算；偏移0表示前交易日，不参与当日股票池，退出不受影响。
                          </p>
                        )}
                        <OverlayFilterEditor value={overlayFilter} onChange={setOverlayFilter}
                          minuteDaily={detail.execution_backend === 'minute_filter'}
                          numericOnly={['matrix_native', 'composite', 'minute_filter'].includes(detail.execution_backend)} />
                      </Section>
                    )}
                    {detail.params.length > 0 ? (
                      <Section icon={Settings2} title="策略参数" accent="text-muted">
                        <StrategyParamGroups strategyId={detail.id} params={detail.params} values={params}>
                          {p => <ParamField key={p.id} def={p} value={params[p.id] ?? p.default}
                            onChange={v => setParams(prev => applyParamValue(detail.params, prev, p.id, v))} />}
                        </StrategyParamGroups>
                      </Section>
                    ) : (
                      <div className="rounded-xl border border-border/15 bg-surface/20 px-3.5 py-4 text-[11px] text-muted/50 text-center">无策略参数</div>
                    )}
                  </div>

                  {/* 列3：评分 + 交易 */}
                  <div className="space-y-3">
                    {['overnight_capacity_v1', 'momentum_capacity_v1'].includes(detail.minute_execution ?? '') ? (
                      <OvernightExecutionSummary params={params} momentum={detail.minute_execution === 'momentum_capacity_v1'} />
                    ) : <>
                    <Section icon={Star} title="评分权重" accent="text-amber-400">
                      {detail.scoring_mode === 'strategy' ? <p className="text-[11px] text-muted">
                        排名由策略生成。s02按ATR14÷昨收降序，排名分数为0～100分，同值按触发涨幅、股票代码排序。
                      </p> : <ScoringEditor
                        key={detail.id}
                        value={scoring}
                        directions={scoringDirections}
                        fallbackLabels={FIELD_LABEL}
                        onChange={(nextScoring, nextDirections) => {
                          setScoring(nextScoring)
                          setScoringDirections(nextDirections)
                        }}
                      />}
                    </Section>
                    <Section icon={TrendingUp} title="交易参数" accent="text-emerald-400">
                      <div className="space-y-2">
                        <p className="text-[11px] text-muted leading-4">统一输入百分数，例如 6 表示 6%；留空关闭。分钟专用策略的退出规则由策略参数管理。</p>
                        <PercentField label="固定止损" value={stopLoss} onChange={v => setStopLoss(v == null ? null : -v)} />
                        <PercentField label="固定止盈" value={takeProfit} onChange={setTakeProfit} min={1} max={500} />
                        <PercentField label="移动止损" value={trailingStop} onChange={v => setTrailingStop(v == null ? null : -v)} min={0.5} />
                        <PercentField label="移动止盈启动" value={trailingActivate} onChange={setTrailingActivate} min={1} max={200} />
                        <PercentField label="移动止盈回撤" value={trailingDrawdown} onChange={setTrailingDrawdown} min={0.5} />
                        <p className="text-[10px] text-muted">移动止盈需同时设置启动和回撤；回撤不得大于启动涨幅。</p>
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] text-secondary w-12 shrink-0">持有</span>
                          <input type="number" aria-label="最长持有" placeholder="关闭" value={maxHoldDays ?? ''} onChange={e => setMaxHoldDays(e.target.value === '' ? null : Number(e.target.value))} step={1} min={1}
                            className="w-16 h-6 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                          <span className="text-[10px] text-muted">天</span>
                        </div>
                        <div className="text-[11px] text-muted pt-1 border-t border-border/10">
                          <span className="text-secondary">入场 </span><span className="text-foreground/70">{entrySignals.length > 0 ? `${entrySignals.length} 个触发器` : '无'}</span>
                          <span className="text-secondary ml-3">出场 </span><span className="text-foreground/70">{exitSignals.length > 0 ? `${exitSignals.length} 个触发器` : '无'}</span>
                        </div>
                        {detail.execution_backend !== 'minute_filter' && (
                          <div className="space-y-2 pt-2 border-t border-border/20">
                            <p className="text-[11px] text-secondary">可选入场风险控制</p>
                            {([
                              ['max_entry_premium_pct', '成交溢价上限 (%)', 100, 100],
                              ['stop_atr_multiple', '信号日 ATR 止损倍数', 1, 10],
                              ['risk_per_trade_pct', '单笔计划风险 / 权益 (%)', 100, 5],
                            ] as const).map(([key, label, scale, max]) => (
                              <label key={key} className="flex items-center justify-between gap-2 text-[11px] text-secondary">
                                <span>{label}</span>
                                <input type="number" aria-label={label} min={0.01} max={max} step={0.1}
                                  placeholder="关闭" value={entryRisk[key] == null ? '' : Number((entryRisk[key]! * scale).toFixed(4))}
                                  onChange={e => setEntryRisk(prev => ({ ...prev, [key]: e.target.value === '' ? null : Number(e.target.value) / scale }))}
                                  className="w-20 h-7 px-1.5 rounded bg-base border border-border text-foreground text-center" />
                              </label>
                            ))}
                            <label className="flex items-center gap-2 text-[11px] text-secondary">
                              <input type="checkbox" checked={entryRisk.stop_signal_low ?? false}
                                onChange={e => setEntryRisk(prev => ({ ...prev, stop_signal_low: e.target.checked }))} />
                              冻结信号日低点作为保护位
                            </label>
                            <p className="text-[10px] leading-4 text-muted">
                              溢价相对信号价；止损取更严格的保护位。风险配仓只缩减仓位，缺参考数据拒绝入场。
                              跳空、T+1及费用可能使实际损失超出计划风险。模拟交易需开启跟随策略风控。
                            </p>
                          </div>
                        )}
                      </div>
                    </Section>

                    <Section
                      icon={TrendingUp}
                      title="入场触发器"
                      accent="text-accent"
                      defaultOpen={false}
                      extra={<SignalTriggerActions selectExisting kind="entry" signals={entrySignals} onChange={setEntrySignals} allowIntraday={detail.execution_backend === 'minute_filter' && !detail.minute_execution} buttonClassName="rounded-md border border-border bg-base p-1 text-muted transition-colors cursor-pointer" iconClassName="h-3 w-3" />}
                    >
                      <SignalPicker signals={entrySignals} onChange={setEntrySignals} kind="entry" options={{ variant: 'dialog', allowIntraday: detail.execution_backend === 'minute_filter' && !detail.minute_execution }} />
                      <div className="text-[10px] leading-4 text-muted/70">任一入场点满足即进入候选。</div>
                    </Section>

                    <Section
                      icon={TrendingUp}
                      title="出场触发器"
                      accent="text-warning"
                      defaultOpen={false}
                      extra={<SignalTriggerActions selectExisting kind="exit" signals={exitSignals} onChange={setExitSignals} allowIntraday={detail.execution_backend === 'minute_filter' && !detail.minute_execution} buttonClassName="rounded-md border border-border bg-base p-1 text-muted transition-colors cursor-pointer" iconClassName="h-3 w-3" />}
                    >
                      <SignalPicker signals={exitSignals} onChange={setExitSignals} kind="exit" options={{ variant: 'dialog', allowIntraday: detail.execution_backend === 'minute_filter' && !detail.minute_execution }} />
                      <div className="text-[10px] leading-4 text-muted/70">任一出场点满足即触发出场。</div>
                    </Section>

                    <div className="rounded-xl border border-amber-400/20 bg-amber-400/[0.04] px-3 py-2 text-[10px] leading-4 text-muted">
                      入场叠加条件对选股、回测和监控统一生效。出场触发器及风控退出独立执行。
                    </div>
                    </>}
                  </div>
                </div>
                )}
              </>
            ) : (
              <div className="flex items-center justify-center py-16 text-sm text-muted">加载失败</div>
            )}
          </div>

          {/* 底部按钮 */}
          {saveError && <p role="alert" className="px-5 py-2 text-xs text-danger">{saveError}</p>}
          <div className="modal-footer flex items-center justify-between">
            <div className="flex items-center gap-2">
              <button onClick={handleReset} disabled={saving || loading || !detail?.definition_defaults}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-border bg-surface text-xs text-secondary hover:text-danger hover:border-danger/30 transition-colors cursor-pointer disabled:opacity-50">
                <RotateCcw className="h-3.5 w-3.5" />恢复策略原始值
              </button>
              {(detail?.source === 'ai' || detail?.source === 'custom' || detail?.source === 'composite') && (
                <button onClick={() => { setDeleteError(''); setShowDeleteConfirm(true) }}
                  className="text-[10px] text-danger hover:text-danger/80 transition-colors">删除策略</button>
              )}
            </div>
            <div className="flex items-center gap-2">
              {onAiModify && (detail?.source === 'ai' || detail?.source === 'custom') && (
                <button onClick={onAiModify}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-amber-400/30 bg-amber-400/8 text-amber-400 text-xs font-medium hover:bg-amber-400/15 transition-colors cursor-pointer">
                  <Sparkles className="h-3.5 w-3.5" />AI 修改
                </button>
              )}
              <button onClick={handleSave} disabled={saving || loading || !detail}
                className="inline-flex items-center gap-1.5 h-8 px-4 rounded-lg bg-accent text-white text-xs font-semibold hover:bg-accent/90 transition-colors cursor-pointer disabled:opacity-50">
                <Save className="h-3.5 w-3.5" />{saving ? '保存中…' : '保存设置'}
              </button>
            </div>
          </div>
    </Modal>

    {/* 删除确认弹窗 — 必须放 Modal 外: Modal 面板有 backdrop-blur (为 fixed 后代建立定位上下文)
        + overflow-hidden, 放里面会导致本应全屏居中的确认框相对面板定位并被裁剪/错位。 */}
    {showDeleteConfirm && (
      <AnimatePresence>
        <Modal onClose={() => setShowDeleteConfirm(false)} ariaLabel="删除策略" size="sm" closeDisabled={deleting} panelClassName="p-5">
            <div className="text-center space-y-3">
              <div className="w-10 h-10 rounded-full bg-danger/10 flex items-center justify-center mx-auto">
                <span className="text-danger text-lg">!</span>
              </div>
              <div>
                <div className="text-sm font-semibold text-foreground">删除策略</div>
                <div className="text-xs text-muted mt-1">确定要删除「{detail?.name ?? strategyId}」吗？</div>
              </div>
              <div className="text-[11px] text-danger/70 bg-danger/[0.04] rounded-lg px-3 py-2 border border-danger/10">
                删除后无法恢复。策略文件、配置、历史选股结果及关联的回测、优化记录和研究候选将永久清除，正在执行的关联任务结果会被丢弃。
              </div>
              {deleteError && (
                <div className="text-[11px] text-danger bg-danger/10 rounded-lg px-3 py-2 border border-danger/20">
                  {deleteError}
                </div>
              )}
              <div className="flex gap-2 pt-2">
                <button onClick={() => setShowDeleteConfirm(false)}
                  className="flex-1 h-8 rounded-lg border border-border text-xs text-secondary hover:text-foreground">取消</button>
                <button onClick={handleDelete} disabled={deleting}
                  className="flex-1 h-8 rounded-lg bg-danger text-white text-xs font-medium hover:bg-danger/90 disabled:opacity-50">
                  {deleting ? '删除中...' : '确认删除'}
                </button>
              </div>
            </div>
        </Modal>
      </AnimatePresence>
    )}

    {/* 子策略配置编辑 — 同删除确认弹窗一样必须放 Modal 外 (面板 backdrop-blur 会为
        fixed 后代建立定位上下文)。渲染在主 Modal 之后, 同 z-50 自然覆盖其上。 */}
    <StrategySettingsDialog
      strategyId={editingChildId}
      onClose={() => setEditingChildId(null)}
      onSaved={() => {
        // 子策略可能改名: 拉最新名称同步到列表 (参数 override 按策略 ID 生效, 无需重建叠加)
        if (!editingChildId) return
        api.strategyGet(editingChildId)
          .then(d => setCompositeChildren(prev =>
            prev.map(c => c.id === editingChildId ? { ...c, name: d.name ?? c.name } : c),
          ))
          .catch(() => {})
      }}
      onDeleted={() => {
        setCompositeChildren(prev => prev.filter(c => c.id !== editingChildId))
        setEditingChildId(null)
      }}
    />
    </>

  )
}
