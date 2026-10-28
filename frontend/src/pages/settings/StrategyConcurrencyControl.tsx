import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { hasPermission, multitenancyEnabled } from '@/lib/accountSession'
import { QK } from '@/lib/queryKeys'

function memoryLabel(mib: number) {
  return mib >= 1024 ? `${Number((mib / 1024).toFixed(2))} GiB` : `${mib} MiB`
}

function cpuLabel(cpus: number) {
  return Number(cpus.toFixed(2))
}

export function StrategyConcurrencyControl() {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const [validationError, setValidationError] = useState<string | null>(null)
  const allowed = !multitenancyEnabled() || hasPermission('settings:manage')
  const query = useQuery({
    queryKey: QK.strategyConcurrency,
    queryFn: api.strategyConcurrency,
    staleTime: 10_000,
    refetchInterval: 10_000,
  })
  const save = useMutation({
    mutationFn: (value: number | null) => api.updateStrategyConcurrency(value),
    onSuccess: (value) => {
      qc.setQueryData(QK.strategyConcurrency, value)
      setDraft(null)
    },
    onError: () => setDraft(null),
  })
  const data = query.data
  const error = save.error ?? query.error
  const message = error instanceof Error ? error.message : '无法读取或保存策略并发配置'
  if (!data || !data.available) {
    return (
      <div className="text-xs">
        {query.isPending
          ? <p role="status" className="text-muted">正在读取策略资源预算…</p>
          : (
            <div role="alert" className="text-amber-500">
              <p>{data && !data.available ? data.error : message}</p>
              <button type="button" onClick={() => query.refetch()}
                className="mt-2 text-accent hover:underline">重试</button>
            </div>
          )}
      </div>
    )
  }
  if (!allowed || data.editable === false) {
    return <p className="text-sm text-foreground">租户策略并发：1</p>
  }
  const disabled = save.isPending || !data.config_enabled || data.max_concurrency === 0
  const limited = data.strategy_concurrency !== null && data.strategy_concurrency > data.max_concurrency
  const commitDraft = () => {
    if (draft === null || disabled) return
    const value = Number(draft)
    if (!/^\d+$/.test(draft) || !Number.isInteger(value) || value < 1 || value > 999) {
      setValidationError('请输入 1～999 的整数')
      return
    }
    setValidationError(null)
    if (value === data.strategy_concurrency) setDraft(null)
    else save.mutate(value)
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 flex-1">
          <p className="text-[11px] leading-relaxed text-muted">
            管理员首次使用推荐值 {data.recommended_concurrency}，可手动输入 1～999；租户策略并发默认为 1。所有租户共用下方资源预算。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <input id="strategy-concurrency" aria-label="策略并发"
            type="number" min={1} max={999} step={1}
            value={draft ?? data.strategy_concurrency ?? data.recommended_concurrency} disabled={disabled}
            aria-invalid={!!validationError}
            aria-describedby={validationError ? 'strategy-concurrency-error' : undefined}
            onChange={(event) => {
              setDraft(event.target.value)
              setValidationError(null)
              save.reset()
            }}
            onBlur={commitDraft}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur()
            }}
            className="h-8 w-20 rounded-btn border border-border bg-base px-2 text-xs text-foreground disabled:opacity-50" />
          <button type="button" disabled={disabled} onPointerDown={event => event.preventDefault()} onClick={() => {
            setValidationError(null)
            setDraft(null)
            save.mutate(null)
          }} className="text-xs text-accent hover:underline disabled:opacity-50">
            使用推荐值
          </button>
        </div>
      </div>
      <div className="grid gap-x-6 gap-y-1 rounded-btn bg-base/60 p-3 text-[11px] leading-relaxed text-secondary sm:grid-cols-2">
        <p>策略内存预算：{memoryLabel(data.memory_budget_mb)}（整机 {memoryLabel(data.host_memory_mb)} 的 {data.budget_ratio * 100}%）</p>
        <p>策略 CPU 预算：{cpuLabel(data.cpu_budget)} vCPU（整机 {cpuLabel(data.host_cpus)} vCPU 的 {data.budget_ratio * 100}%）</p>
        <p>当前生效：{data.effective_concurrency} · 运行中：{data.active}</p>
        <p>新任务资源上限：{memoryLabel(data.task_memory_mb)} / {cpuLabel(data.task_cpus)} vCPU</p>
      </div>
      <p className="text-[11px] leading-relaxed text-muted">
        推荐值按单任务资源上限估算，不代表压测容量。提高并发会减少每个任务可用资源，可能变慢或因内存不足失败。
        {data.execution_mode === 'in_process' && ' 当前为进程内执行，内存预算用于准入估算。'}
      </p>
      {!data.config_enabled && <p role="alert" className="text-xs text-amber-500">计算服务尚未接入全局策略配置。</p>}
      {limited && <p role="status" className="text-xs text-amber-500">已按当前资源预算及部署上限限制为 {data.effective_concurrency}；可输入更小的值或使用推荐值。</p>}
      {data.max_concurrency === 0 && <p role="alert" className="text-xs text-amber-500">策略资源预算不足，暂时无法运行任务。</p>}
      {validationError && <p id="strategy-concurrency-error" role="alert" className="text-xs text-red-400">{validationError}</p>}
      {error && <p role="alert" className="text-xs text-red-400">{message}</p>}
      {data.pending && <p role="status" className="text-xs text-amber-500">配置已保存，计算服务尚未应用；请检查部署配置。</p>}
      {save.isPending && <p role="status" className="text-xs text-muted">正在保存…</p>}
      {save.isSuccess && !data.pending && <p role="status" className="text-xs text-emerald-500">已保存，新任务按当前配置执行。</p>}
    </div>
  )
}
