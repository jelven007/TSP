import { Modal } from '@/components/Modal'
import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence } from 'framer-motion'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Loader2, Plus, Save, Search, Sparkles, X } from 'lucide-react'
import { api, type CustomSignal, type CustomSignalCondition, type CustomSignalFieldGroup, type LateSessionStrengthParams, type MorningLiquidationParams } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { LATE_SESSION_DEFAULTS, MORNING_LIQUIDATION_DEFAULTS } from '@/lib/customSignals'

interface Props {
  open: boolean
  signal?: CustomSignal | null
  defaultKind?: CustomSignal['kind']
  onClose: () => void
  onSaved?: (signal: CustomSignal) => void
  allowLateSession?: boolean
  allowMorningLiquidation?: boolean
}

// 字符串运算符的中文标签 (数值运算符直接显示符号)
const OP_LABELS: Record<string, string> = { contains: '包含' }

const emptySignal = (kind: CustomSignal['kind'] = 'exit'): CustomSignal => ({
  id: '', name: '', kind, enabled: true,
  conditions: [{ left: 'close', op: '>', right: 'field:ma20', leftDays: 0, rightDays: 0 }],
})

export function CustomSignalDialog({ open, signal, defaultKind = 'exit', onClose, onSaved, allowLateSession = true, allowMorningLiquidation = true }: Props) {
  const qc = useQueryClient()
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions, enabled: open })

  const [draft, setDraft] = useState<CustomSignal>(() => emptySignal(defaultKind))
  const [error, setError] = useState('')

  // AI 生成条件
  const [aiOpen, setAiOpen] = useState(false)
  const [aiDesc, setAiDesc] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')
  const [aiConfigured, setAiConfigured] = useState<boolean | null>(null)
  const checkedAi = useRef(false)

  const fields = options.data?.fields ?? []
  const groups = options.data?.groups
  const maxDays = options.data?.maxDays ?? 60
  const operators = options.data?.operators ?? ['>', '>=', '<', '<=', '==', '!=']
  const stringFields = options.data?.stringFields ?? []
  const stringOperators = options.data?.stringOperators ?? ['contains', '==', '!=']
  const editing = !!signal
  const lateSession = draft.template === 'late_session_strength'
  const morning = draft.template === 'morning_liquidation'
  const specialized = lateSession || morning
  const lateParams = { ...LATE_SESSION_DEFAULTS, ...draft.params as Partial<LateSessionStrengthParams> }
  const morningParams = { ...MORNING_LIQUIDATION_DEFAULTS, ...draft.params as Partial<MorningLiquidationParams> }

  useEffect(() => {
    if (!open) return
    setDraft(signal ? { ...signal, params: signal.params ? { ...signal.params } : undefined,
      conditions: signal.conditions.map(c => ({ ...c })) } : emptySignal(defaultKind))
    setError('')
    setAiOpen(false); setAiDesc(''); setAiError(''); setAiLoading(false)
  }, [open, signal, defaultKind])

  // 打开时检查一次 AI 是否已配置（复用策略构建器逻辑）
  useEffect(() => {
    if (!open || checkedAi.current) return
    checkedAi.current = true
    api.strategyAiStatus()
      .then(s => setAiConfigured(s.configured))
      .catch(() => setAiConfigured(false))
  }, [open])

  const generateByAI = async () => {
    const desc = aiDesc.trim()
    if (!desc) { setAiError('请先描述信号思路'); return }
    setAiLoading(true)
    setAiError('')
    try {
      const res = await api.customSignalsAiGenerate(desc)
      setDraft(d => ({
        ...d,
        name: d.name.trim() ? d.name : res.name,
        conditions: res.conditions.map(c => ({ ...c, leftDays: c.leftDays ?? 0, rightDays: c.rightDays ?? 0 })),
      }))
    } catch (err: any) {
      setAiError(String(err?.message ?? err))
    } finally {
      setAiLoading(false)
    }
  }

  const save = useMutation({
    mutationFn: () => {
      const d = draft
      if (!d.id.trim()) throw new Error('请输入信号标识')
      if (!/^[a-z0-9_]{1,40}$/.test(d.id)) throw new Error('标识仅允许小写字母、数字、下划线（1-40字符）')
      if (!d.name.trim()) throw new Error('请输入信号名称')
      if (!specialized && d.conditions.length === 0) throw new Error('至少需要一个条件')
      if (morning) {
        if (!/^(09|10|11):[0-5]\d:[0-5]\d$/.test(morningParams.time)
          || morningParams.time < '09:30:00' || morningParams.time > '11:30:00') throw new Error('清仓截止时间须为 09:30:00～11:30:00')
        if (!Number.isFinite(morningParams.amplitude_pct) || morningParams.amplitude_pct < 0
          || morningParams.amplitude_pct > 100) throw new Error('振幅阈值须为 0～100%')
      }
      if (lateSession) {
        const p = lateParams
        if (!/^1[34]:[0-5]\d:00$|^15:00:00$/.test(p.time) || p.time < '13:01:00') throw new Error('判断时间须为午后整分钟')
        if (![p.change_min_pct, p.change_max_pct, p.pool_up_min_pct].every(Number.isFinite)
          || p.change_min_pct < 0 || p.change_min_pct > p.change_max_pct || p.change_max_pct > 100
          || p.pool_up_min_pct < 0 || p.pool_up_min_pct > 100) throw new Error('请检查涨幅区间和上涨占比')
        if (!Number.isInteger(p.ma5_1m_count) || p.ma5_1m_count < 2 || p.ma5_1m_count > 120
          || !Number.isInteger(p.ma5_5m_count) || p.ma5_5m_count < 2 || p.ma5_5m_count > 24) throw new Error('MA5 数量须在允许范围内且为整数')
      }
      for (const c of d.conditions) {
        if (!c.left || !c.op || c.right === '') throw new Error('条件填写不完整')
      }
      return api.customSignalSave(lateSession ? { ...d, params: lateParams } : morning ? { ...d, params: morningParams } : d)
    },
    onSuccess: res => {
      qc.invalidateQueries({ queryKey: QK.customSignals })
      onSaved?.(res.signal)
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const updateCond = (idx: number, patch: Partial<CustomSignalCondition>) =>
    setDraft(d => ({ ...d, conditions: d.conditions.map((c, i) => i === idx ? { ...c, ...patch } : c) }))
  // 切换左字段时若跨越 数值↔字符串 类型, 运算符/右值语义不再成立, 一并复位
  const changeLeft = (idx: number, left: string) =>
    setDraft(d => ({
      ...d,
      conditions: d.conditions.map((c, i) => {
        if (i !== idx) return c
        const wasStr = stringFields.includes(c.left)
        const isStr = stringFields.includes(left)
        if (wasStr === isStr) return { ...c, left }
        return isStr
          ? { ...c, left, op: 'contains', right: '', rightDays: 0 }
          : { ...c, left, op: '>', right: '0', rightDays: 0 }
      }),
    }))
  const addCond = () => setDraft(d => ({ ...d, conditions: [...d.conditions, { left: 'close', op: '>', right: '0', leftDays: 0, rightDays: 0 }] }))
  const removeCond = (idx: number) => setDraft(d => ({ ...d, conditions: d.conditions.filter((_, i) => i !== idx) }))

  return (
    <AnimatePresence>
      {open && (
        <Modal onClose={onClose} ariaLabel="自定义信号" size="md">
            <div className="modal-header flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-foreground">{editing ? '编辑自定义信号' : '新建自定义信号'}</h3>
                <p className="mt-1 text-[11px] text-muted">标识保存后不可修改。{morning ? '早盘清仓用于持仓次交易日的出场判断。' : lateSession ? '尾盘走强用于分钟策略，全部条件同时满足时触发。' : '多条件同时满足时触发。'}</p>
              </div>
              <button onClick={onClose} className="modal-close" aria-label="关闭弹窗">
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="modal-body flex-1 overflow-y-auto px-5 py-5 space-y-5">
              {(allowLateSession || allowMorningLiquidation || specialized) && (
                <label className="block space-y-1.5">
                  <span className="text-[11px] text-muted">信号模板</span>
                  <select aria-label="信号模板" value={draft.template ?? 'conditions'} disabled={editing}
                    onChange={e => setDraft(e.target.value === 'late_session_strength'
                      ? { id: draft.id || 'late_session_strength', name: '尾盘走强', kind: 'entry',
                        timeframe: 'intraday', template: 'late_session_strength', enabled: true,
                        conditions: [], params: { ...LATE_SESSION_DEFAULTS } }
                      : e.target.value === 'morning_liquidation'
                        ? { id: draft.id || 'morning_liquidation', name: '早盘清仓', kind: 'exit',
                          timeframe: 'intraday', template: 'morning_liquidation', enabled: true,
                          conditions: [], params: { ...MORNING_LIQUIDATION_DEFAULTS } }
                        : emptySignal(defaultKind))}
                    className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
                    <option value="conditions">普通条件组合</option>
                    {(allowLateSession || lateSession) && <option value="late_session_strength">尾盘走强 · 分钟入场</option>}
                    {(allowMorningLiquidation || morning) && <option value="morning_liquidation">早盘清仓 · 持仓出场</option>}
                  </select>
                </label>
              )}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">信号标识</span>
                  <input
                    value={draft.id}
                    disabled={editing}
                    onChange={e => setDraft(d => ({ ...d, id: e.target.value.replace(/[^a-z0-9_]/g, '') }))}
                    placeholder="如 low_touches_ma5"
                    className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground disabled:opacity-60"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">信号名称</span>
                  <input value={draft.name} onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} placeholder="如 跌至MA5" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                </label>
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">类型</span>
                  <select value={draft.kind} disabled={specialized} onChange={e => setDraft(d => ({ ...d, kind: e.target.value as CustomSignal['kind'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
                    <option value="entry">入场</option>
                    <option value="exit">出场</option>
                    <option value="both">出入通用</option>
                  </select>
                </label>
              </div>

              {morning ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">清仓截止时间（北京时间）</span>
                      <input aria-label="清仓截止时间" type="time" step="1" min="09:30:00" max="11:30:00"
                        value={morningParams.time} onChange={e => setDraft(d => ({ ...d,
                          params: { ...morningParams, time: e.target.value.length === 5 ? `${e.target.value}:00` : e.target.value } }))}
                        className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                    </label>
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">振幅阈值（%）</span>
                      <input aria-label="振幅阈值（%）" type="number" min="0" max="100" step="0.1" value={morningParams.amplitude_pct}
                        onChange={e => setDraft(d => ({ ...d, params: { ...morningParams,
                          amplitude_pct: e.target.value === '' ? NaN : Number(e.target.value) } }))}
                        className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                    </label>
                  </div>
                  <p className="text-[11px] leading-5 text-muted">
                    以每笔持仓买入后的下一个交易日为准，跳过周末和休市日。
                    截止时间前振幅达到或超过阈值即触发清仓；仍未达到时，截止时间一到立即触发。
                    振幅 =（当日至当前最高价 − 最低价）/ 昨收 × 100%。
                    使用新鲜实时行情，已完成分钟 K 线可用于补判；行情缺失时截止清仓仍有效。
                    用于有买入日期的通用分钟策略持仓监控；S01/S02 专用规则与收益回测暂不支持。
                    信号触发后由交易模块处理成交，停牌、跌停或不可卖持仓可能无法成交。
                  </p>
                </div>
              ) : lateSession ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">判断时间（北京时间）</span>
                      <input aria-label="判断时间" type="time" step="60" min="13:01:00" max="15:00:00"
                        value={lateParams.time} onChange={e => setDraft(d => ({ ...d,
                          params: { ...lateParams, time: e.target.value.length === 5 ? `${e.target.value}:00` : e.target.value } }))}
                        className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                    </label>
                    {([
                      ['change_min_pct', '涨幅下限（%）', 0, 100, 0.1],
                      ['change_max_pct', '涨幅上限（%）', 0, 100, 0.1],
                      ['pool_up_min_pct', '股票池上涨占比下限（%）', 0, 100, 0.1],
                      ['ma5_1m_count', '1分钟 MA5 连增数量', 2, 120, 1],
                      ['ma5_5m_count', '5分钟 MA5 连增数量', 2, 24, 1],
                    ] as const).map(([key, label, min, max, step]) => (
                      <label key={key} className="space-y-1.5">
                        <span className="text-[11px] text-muted">{label}</span>
                        <input type="number" aria-label={label} min={min} max={max} step={step} value={lateParams[key]}
                          onChange={e => setDraft(d => ({ ...d, params: { ...lateParams,
                            [key]: e.target.value === '' ? NaN : Number(e.target.value) } }))}
                          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                      </label>
                    ))}
                  </div>
                  <p className="text-[11px] leading-5 text-muted">
                    以上条件为「且」关系。涨幅相对昨收，区间包含上下限；上涨定义为涨幅大于 0。
                    上涨占比使用策略完整的当日股票池。MA5 使用已完成 K 线的收盘价，值相等不算递增，不跨午休或交易日。
                    判断时间支持整分钟；非 5 分钟边界取该时刻前最新完成的 5 分钟 K 线。
                    缺少股票池、价格、昨收或连续 K 线时不触发。用于通用分钟策略，S01/S02 专用执行规则暂不支持追加本触发器；历史回放需指定策略股票池。
                  </p>
                </div>
              ) : <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-muted">条件（多条件为「且」关系）</span>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      onClick={() => setAiOpen(o => !o)}
                      className={`inline-flex items-center gap-1 text-[11px] cursor-pointer transition-colors ${aiOpen ? 'text-amber-400' : 'text-amber-400/80 hover:text-amber-400'}`}
                    >
                      <Sparkles className="h-3 w-3" />AI 生成条件
                    </button>
                    <button onClick={addCond} className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer">
                      <Plus className="h-3 w-3" />添加条件
                    </button>
                  </div>
                </div>
                <div className="space-y-2 rounded-card border border-border/70 bg-base/50 p-3">
                  {draft.conditions.map((c, i) => {
                    const isStr = stringFields.includes(c.left)
                    const condOps = isStr ? stringOperators : operators
                    return (
                    <div key={i} className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[10px] text-muted/60 w-5 text-right shrink-0">{i === 0 ? '当' : '且'}</span>

                      {/* 左操作数: 前N日 + 字段(弹出选择) */}
                      <DaysInput value={c.leftDays ?? 0} max={maxDays} onChange={v => updateCond(i, { leftDays: v })} />
                      <FieldPicker value={c.left} fields={fields} groups={groups} onChange={v => changeLeft(i, v)} />

                      {/* 运算符: 字符串字段为 包含/等于/不等于 */}
                      <select value={c.op} onChange={e => updateCond(i, { op: e.target.value })} className="w-11 h-7 px-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                        {condOps.map(op => <option key={op} value={op}>{OP_LABELS[op] ?? op}</option>)}
                      </select>

                      {/* 右操作数: 字符串字段为文本字面量; 其余为 前N日(仅字段) + 字段/常量(弹出选择) */}
                      {isStr ? (
                        <input type="text" value={c.right} onChange={e => updateCond(i, { right: e.target.value })}
                          placeholder="概念/行业名, 如 AI" maxLength={64}
                          className="flex-1 min-w-0 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50" />
                      ) : (
                        <RightValueInput cond={c} fields={fields} groups={groups} maxDays={maxDays}
                          onChangeRight={v => updateCond(i, { right: v })}
                          onChangeDays={v => updateCond(i, { rightDays: v })} />
                      )}

                      {draft.conditions.length > 1 && (
                        <button onClick={() => removeCond(i)} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </div>
                    )
                  })}
                </div>
                {aiOpen && (
                  <div className="rounded-card border border-amber-400/30 bg-amber-400/5 p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
                      <span className="text-[11px] text-amber-300">描述信号思路，AI 将生成条件组合</span>
                    </div>
                    {aiConfigured === false ? (
                      <div className="text-xs text-amber-400/80">
                        AI 未配置，无法生成信号。{' '}
                        <Link to="/settings?tab=ai" className="underline hover:text-amber-300">去设置页配置 API Key</Link>
                      </div>
                    ) : (
                      <>
                        <textarea
                          value={aiDesc}
                          onChange={e => setAiDesc(e.target.value)}
                          placeholder="例如：收盘价回踩20日均线，且量比≥2 放量"
                          rows={2}
                          className="w-full rounded-btn border border-border bg-base px-3 py-2 text-xs text-foreground focus:outline-none focus:border-amber-400/50 resize-none"
                        />
                        {aiError && <div className="text-xs text-danger">{aiError}</div>}
                        <div className="flex justify-end">
                          <button
                            onClick={generateByAI}
                            disabled={aiLoading}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-amber-500/90 text-white text-xs font-medium disabled:opacity-50 cursor-pointer"
                          >
                            {aiLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                            {aiLoading ? '生成中…' : '生成条件'}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}
                <p className="text-[10px] text-muted/60 px-1">
                  每个操作数左侧的 <span className="text-foreground/70">最新</span> 按钮可点击切换为「前N日」(取 N 个交易日前的值)。例:收盘价(最新) &gt; 收盘价(前1日) = 上涨。带偏移的条件仅盘后/回测生效, 盘中实时跳过。
                </p>
              </div>}

              {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}
            </div>

            <div className="modal-footer flex justify-end gap-2">
              <button onClick={onClose} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs">取消</button>
              <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-amber-500/90 text-white text-xs font-medium disabled:opacity-50">
                <Save className="h-3.5 w-3.5" />保存
              </button>
            </div>
        </Modal>
      )}
    </AnimatePresence>
  )
}

// ── 字段选择器: 搜索 + 分组居中对话框 ───────────────────

export function FieldPicker({ value, fields, groups, onChange }: {
  value: string
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  onChange: (v: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const selectedLabel = fields.find(f => f.key === value)?.label ?? value

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q || !groups) return groups
    return groups
      .map(g => ({ ...g, fields: g.fields.filter(f => f.label.toLowerCase().includes(q) || f.key.toLowerCase().includes(q)) }))
      .filter(g => g.fields.length > 0)
  }, [groups, query])

  const filteredFields = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q || filteredGroups) return fields
    return fields.filter(f => f.label.toLowerCase().includes(q) || f.key.toLowerCase().includes(q))
  }, [fields, query, filteredGroups])

  return (
    <>
      <button
        type="button"
        onClick={() => { setQuery(''); setOpen(true) }}
        className="min-w-[80px] max-w-[180px] h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground text-left hover:border-accent/40 transition-colors cursor-pointer truncate"
      >
        {selectedLabel}
      </button>
      {createPortal(
        <AnimatePresence>
          {open && (
            <Modal onClose={() => setOpen(false)} ariaLabel="选择信号字段" size="sm">
                {/* 标题 + 搜索 */}
                <div className="modal-header space-y-2.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-foreground">选择字段</span>
                    <button onClick={() => setOpen(false)} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground transition-colors">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="flex items-center gap-2 px-2.5 h-8 rounded-btn bg-base border border-border">
                    <Search className="h-3.5 w-3.5 text-muted shrink-0" />
                    <input
                      autoFocus
                      value={query}
                      onChange={e => setQuery(e.target.value)}
                      placeholder="搜索字段…"
                      className="flex-1 bg-transparent text-xs text-foreground focus:outline-none"
                    />
                    {query && <button onClick={() => setQuery('')} className="text-muted hover:text-foreground"><X className="h-3 w-3" /></button>}
                  </div>
                </div>
                {/* 分组列表 */}
                <div className="modal-body flex-1 overflow-y-auto p-2">
                  {filteredGroups ? (
                    filteredGroups.length > 0 ? filteredGroups.map(g => (
                      <div key={g.key} className="mb-1">
                        <div className="px-2 py-1 text-[10px] text-muted/60 font-medium">{g.label}</div>
                        {g.fields.map(f => (
                          <button
                            key={f.key}
                            onClick={() => { onChange(f.key); setOpen(false) }}
                            className={`w-full text-left px-2.5 py-1.5 rounded text-xs transition-colors ${
                              f.key === value ? 'bg-accent/10 text-accent' : 'text-foreground/80 hover:bg-elevated'
                            }`}
                          >
                            {f.label}
                          </button>
                        ))}
                      </div>
                    )) : (
                      <div className="px-3 py-8 text-center text-xs text-muted">无匹配字段</div>
                    )
                  ) : (
                    filteredFields.map(f => (
                      <button
                        key={f.key}
                        onClick={() => { onChange(f.key); setOpen(false) }}
                        className={`w-full text-left px-2.5 py-1.5 rounded text-xs transition-colors ${
                          f.key === value ? 'bg-accent/10 text-accent' : 'text-foreground/80 hover:bg-elevated'
                        }`}
                      >
                        {f.label}
                      </button>
                    ))
                  )}
                </div>
            </Modal>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}

// ── 日期偏移控件: "最新" / "前N日" ───────────────────────

export function DaysInput({ value, max, onChange }: { value: number; max: number; onChange: (v: number) => void }) {
  if (!value) {
    return (
      <button
        type="button"
        onClick={() => onChange(1)}
        title="点击切换为「前N日」(取 N 个交易日前的值)"
        className="h-7 px-2 rounded bg-base border border-border text-[11px] text-muted hover:text-accent hover:border-accent/50 transition-colors shrink-0 cursor-pointer"
      >
        最新
      </button>
    )
  }
  return (
    <div className="flex items-center h-7 rounded bg-base border border-border focus-within:border-accent/50 transition-colors shrink-0">
      <span className="pl-1.5 text-[11px] text-muted select-none">前</span>
      <input
        type="number"
        min={1}
        max={max}
        value={value}
        onChange={e => {
          const raw = e.target.value
          if (raw === '') { onChange(0); return }
          const n = Math.max(1, Math.min(max, parseInt(raw) || 1))
          onChange(n)
        }}
        title={`前 ${value} 个交易日的值`}
        className="w-7 h-full px-0 text-[11px] font-mono text-foreground text-center bg-transparent focus:outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
      />
      <button
        type="button"
        onClick={() => onChange(0)}
        title="切回「最新」"
        className="pr-1.5 pl-0.5 text-[11px] text-muted hover:text-accent transition-colors cursor-pointer"
      >
        日
      </button>
    </div>
  )
}

// ── 右操作数: 字段(弹出) / 常量 切换 ─────────────────────

export function RightValueInput({ cond, fields, groups, maxDays, onChangeRight, onChangeDays }: {
  cond: CustomSignalCondition
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  maxDays: number
  onChangeRight: (v: string) => void
  onChangeDays: (v: number) => void
}) {
  const isField = cond.right.startsWith('field:')
  const fieldValue = isField ? cond.right.slice(6) : ''
  const numValue = isField ? '' : cond.right

  return (
    <div className="flex items-center gap-1 flex-1 min-w-0">
      {isField ? (
        <>
          <DaysInput value={cond.rightDays ?? 0} max={maxDays} onChange={onChangeDays} />
          <FieldPicker value={fieldValue} fields={fields} groups={groups} onChange={v => onChangeRight(`field:${v}`)} />
          <button onClick={() => onChangeRight('0')} title="切换为数字" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer shrink-0">
            <ArrowRight className="h-3 w-3 rotate-90" />
          </button>
        </>
      ) : (
        <>
          {/* 常量无前N日概念, 占位保持与字段模式对齐 */}
          <div className="shrink-0" style={{ width: 44 }} />
          <input type="number" value={numValue} onChange={e => onChangeRight(e.target.value)} step="any"
            className="flex-1 min-w-0 h-7 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
          <button onClick={() => onChangeRight('field:close')} title="切换为字段" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer shrink-0">
            <ArrowRight className="h-3 w-3 -rotate-90" />
          </button>
        </>
      )}
    </div>
  )
}
