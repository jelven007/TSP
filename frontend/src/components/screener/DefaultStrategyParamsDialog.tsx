// 账户在当前浏览器的新建策略模板；批量修改现有策略是独立的次要操作。
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { SlidersHorizontal, RotateCcw, Save, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { storage, type DefaultStrategyBasicFilter } from '@/lib/storage'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { numericBasicFilter, validateBasicFilter, type NumericBasicFilter } from '@/lib/strategyBasicFilter'
import { BasicFilterEditor, ALL_BOARDS } from './BasicFilterEditor'

export const BUILTIN_DEFAULT_BASIC_FILTER: DefaultStrategyBasicFilter = {
  enabled: true, price_min: 5, price_max: 200,
  market_cap_min: null, market_cap_max: null,
  float_cap_min: 30e8, float_cap_max: 1500e8,
  amount_min: null, amount_max: null,
  turnover_min: 1, turnover_max: null, exclude_new_days: 30,
  exclude_st: true, boards: [...ALL_BOARDS],
}

export function loadDefaultBasicFilter(): DefaultStrategyBasicFilter {
  const saved = storage.defaultStrategyBasicFilter.get(null)
  const next = { ...BUILTIN_DEFAULT_BASIC_FILTER, ...saved }
  next.boards = Array.isArray(next.boards) ? next.boards.filter(b => ALL_BOARDS.includes(b)) : [...ALL_BOARDS]
  // 兼容旧浏览器模板；读取/取消不写入配置。
  return next
}

interface Props { show: boolean; onClose: () => void }
type Batch = { targets: { id: string; name: string }[]; values: NumericBasicFilter }

export function DefaultStrategyParamsDialog({ show, onClose }: Props) {
  const qc = useQueryClient()
  const [bf, setBf] = useState(loadDefaultBasicFilter)
  const [selected, setSelected] = useState<string[]>([])
  const [batch, setBatch] = useState<Batch | null>(null)
  const [applying, setApplying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [failures, setFailures] = useState<string[]>([])
  const [result, setResult] = useState('')
  const [error, setError] = useState('')
  const strategiesQuery = useQuery({
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    enabled: show,
  })
  const strategies = (strategiesQuery.data?.presets ?? []).filter(s => !s.asset_types || s.asset_types.includes('stock'))
  const frozen = !!batch

  useEffect(() => {
    if (show) {
      setBf(loadDefaultBasicFilter())
      setSelected([])
      setBatch(null)
      setFailures([])
      setResult('')
      setError('')
      setProgress(0)
    }
  }, [show])
  if (!show) return null

  const validate = () => {
    const problem = validateBasicFilter(bf) ?? (!bf.boards.length ? '请至少选择一个股票板块' : null)
    setError(problem ?? '')
    return !problem
  }
  const save = () => {
    if (!validate()) return
    storage.defaultStrategyBasicFilter.set(bf)
    toast('新建策略模板已保存，仅用于之后新建的策略', 'success')
    onClose()
  }
  const prepareBatch = () => {
    if (!validate()) return
    const targets = strategies.filter(s => selected.includes(s.id)).map(s => ({ id: s.id, name: s.name || s.id }))
    if (targets.length) {
      setBatch({ targets, values: numericBasicFilter(bf) })
      setFailures([])
      setResult('')
    }
  }
  const apply = async () => {
    if (!batch || applying) return
    setApplying(true)
    setProgress(0)
    const failed: string[] = []
    for (let offset = 0; offset < batch.targets.length; offset += 8) {
      const chunk = batch.targets.slice(offset, offset + 8)
      const results = await Promise.allSettled(chunk.map(s => api.strategyPatchConfig(s.id, { basic_filter: batch.values })))
      results.forEach((r, i) => {
        if (r.status === 'rejected') failed.push(`${chunk[i].name}：${r.reason instanceof Error ? r.reason.message : '保存失败'}`)
      })
      setProgress(Math.min(offset + 8, batch.targets.length))
    }
    await qc.invalidateQueries({ queryKey: ['screener-strategies'] })
    await qc.invalidateQueries({ queryKey: ['strategies'] })
    setFailures(failed)
    setResult(`已应用 ${batch.targets.length - failed.length}/${batch.targets.length} 个策略。下次运行生效，历史结果保留。`)
    setBatch(null)
    setApplying(false)
  }

  return <Modal onClose={onClose} closeDisabled={applying} labelledBy="default-strategy-params-title" size="sm">
    <div className="modal-header flex items-center justify-between">
      <div className="flex items-center gap-2">
        <SlidersHorizontal className="h-4 w-4 text-accent" />
        <span id="default-strategy-params-title" className="text-sm font-semibold text-foreground">新建策略模板</span>
      </div>
      <button onClick={onClose} disabled={applying} className="modal-close" aria-label="关闭新建策略模板"><X className="h-4 w-4" /></button>
    </div>
    <div className="modal-body overflow-y-auto px-4 py-4 space-y-5">
      <p className="text-xs text-muted leading-5">保存在当前账户的当前浏览器，供之后新建的自定义和 AI 策略使用。每个现有策略继续使用自身设置。</p>
      <BasicFilterEditor value={bf} disabled={frozen} onChange={next => setBf(prev => ({ ...prev, ...next }))} />
      <fieldset disabled={frozen} className="space-y-3 border-t border-border/50 pt-3">
        <legend className="text-xs text-secondary">新建策略的股票范围</legend>
        <p className="text-[11px] text-muted">板块（至少选择一个）</p>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {ALL_BOARDS.map(board => <label key={board} className="flex items-center gap-1.5 text-xs text-secondary">
            <input type="checkbox" checked={bf.boards.includes(board)} onChange={e => setBf(prev => ({
              ...prev, boards: e.target.checked ? [...prev.boards, board] : prev.boards.filter(b => b !== board),
            }))} />{board}
          </label>)}
        </div>
        <label className="flex items-center gap-2 text-xs text-secondary">
          <input type="checkbox" checked={bf.exclude_st} onChange={e => setBf(prev => ({ ...prev, exclude_st: e.target.checked }))} />
          排除 *ST 和 ST
        </label>
      </fieldset>
      <details className="border-t border-border/50 pt-3">
        <summary className="cursor-pointer text-xs text-secondary">批量应用数值过滤到现有策略</summary>
        <div className="pt-3 space-y-3">
          <p className="text-[11px] text-muted leading-4">只同步数值过滤及启用状态；各策略的股票范围、ST、专属参数、评分和风控保留。此操作不会保存新建模板。</p>
          {strategiesQuery.isPending ? <p role="status" className="text-xs text-muted">加载策略中…</p>
            : strategiesQuery.isError ? <p role="alert" className="text-xs text-danger">策略加载失败 <button onClick={() => strategiesQuery.refetch()} className="underline">重试</button></p>
            : <fieldset disabled={frozen} className="max-h-40 overflow-y-auto space-y-2">
              {strategies.map(s => <label key={s.id} className="flex items-center gap-2 text-xs text-secondary">
                <input type="checkbox" aria-label={s.name || s.id} checked={selected.includes(s.id)}
                  onChange={e => setSelected(prev => e.target.checked ? [...prev, s.id] : prev.filter(id => id !== s.id))} />
                <span className="truncate">{s.name || s.id}</span>
              </label>)}
              {!strategies.length && <p className="text-xs text-muted">暂无可应用策略</p>}
            </fieldset>}
          {!batch ? <button onClick={prepareBatch} disabled={!selected.length}
            className="w-full rounded-btn border border-border px-3 py-2 text-xs text-secondary disabled:opacity-40">
            应用到所选策略（{selected.length} 个）
          </button> : <div className="rounded-lg border border-amber-500/30 p-3 space-y-2">
            <p className="text-xs text-secondary">将更新 {batch.targets.length} 个策略：{batch.targets.map(s => s.name).join('、')}。过滤值已锁定。</p>
            {applying ? <p role="status" className="text-xs text-muted">应用中 {progress}/{batch.targets.length}</p>
              : <div className="flex gap-2">
                <button onClick={() => setBatch(null)} className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary">返回编辑</button>
                <button onClick={apply} className="rounded-btn bg-accent px-3 py-1.5 text-xs text-white">确认应用</button>
              </div>}
          </div>}
          {result && <p role="status" className="text-xs text-secondary">{result}</p>}
          {failures.length > 0 && <div role="alert" className="text-xs text-danger">
            <p>以下策略未成功，请检查后重试：</p>
            <ul className="list-disc pl-4">{failures.map(f => <li key={f}>{f}</li>)}</ul>
          </div>}
        </div>
      </details>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </div>
    <div className="modal-footer flex flex-wrap items-center justify-between gap-2">
      <button disabled={frozen} onClick={() => { setBf(loadBuiltinDefaults()); setError('') }}
        className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1.5 text-xs text-muted disabled:opacity-40">
        <RotateCcw className="h-3 w-3" />恢复模板初始值
      </button>
      <div className="flex gap-2">
        <button disabled={applying} onClick={onClose} className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary">取消</button>
        <button disabled={frozen} onClick={save} className="inline-flex items-center gap-1 rounded-btn bg-accent px-3 py-1.5 text-xs text-white disabled:opacity-40">
          <Save className="h-3 w-3" />保存模板
        </button>
      </div>
    </div>
  </Modal>
}

function loadBuiltinDefaults() {
  return { ...BUILTIN_DEFAULT_BASIC_FILTER, boards: [...ALL_BOARDS] }
}
