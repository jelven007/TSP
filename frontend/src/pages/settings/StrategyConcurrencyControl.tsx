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
  const allowed = !multitenancyEnabled() || hasPermission('settings:manage')
  const query = useQuery({
    queryKey: QK.strategyConcurrency,
    queryFn: api.strategyConcurrency,
    enabled: allowed,
    staleTime: 10_000,
    refetchInterval: 10_000,
  })
  const save = useMutation({
    mutationFn: (value: number | null) => api.updateStrategyConcurrency(value),
    onSuccess: (value) => qc.setQueryData(QK.strategyConcurrency, value),
  })
  if (!allowed) return null
  const data = query.data
  const error = save.error ?? query.error
  const message = error instanceof Error ? error.message : '无法读取或保存策略并发配置'
  if (!data || !data.available) {
    return (
      <div className="mt-3 border-t border-border pt-4 text-xs">
        <div className="mb-2 text-sm text-foreground">并发策略数</div>
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
  const disabled = save.isPending || !data.config_enabled || data.max_concurrency === 0
  const limited = data.strategy_concurrency !== null && data.strategy_concurrency > data.max_concurrency
  return (
    <div className="mt-3 space-y-3 border-t border-border pt-4">
      <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 flex-1">
          <label htmlFor="strategy-concurrency" className="text-sm text-foreground">并发策略数</label>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            管理员与所有租户共用的全局上限；自动模式使用推荐值，繁忙时等待或提示稍后重试。
          </p>
        </div>
        <select id="strategy-concurrency" aria-label="并发策略数"
          value={data.strategy_concurrency ?? 'auto'} disabled={disabled}
          onChange={(event) => save.mutate(
            event.target.value === 'auto' ? null : Number(event.target.value),
          )}
          className="h-8 max-w-full shrink-0 rounded-btn border border-border bg-base px-2 text-xs text-foreground disabled:opacity-50">
          <option value="auto">自动（推荐 {data.recommended_concurrency}）</option>
          {limited && <option value={data.strategy_concurrency!} disabled>{data.strategy_concurrency}（已保存）</option>}
          {Array.from({ length: data.max_concurrency }, (_, index) => index + 1).map(value => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
      </div>
      <div className="grid gap-x-6 gap-y-1 rounded-btn bg-base/60 p-3 text-[11px] leading-relaxed text-secondary sm:grid-cols-2">
        <p>策略内存预算：{memoryLabel(data.memory_budget_mb)}（整机 {memoryLabel(data.host_memory_mb)} 的 25%）</p>
        <p>策略 CPU 预算：{cpuLabel(data.cpu_budget)} vCPU（整机 {cpuLabel(data.host_cpus)} vCPU 的 25%）</p>
        <p>当前生效：{data.effective_concurrency} · 运行中：{data.active}</p>
        <p>新任务资源上限：{memoryLabel(data.task_memory_mb)} / {cpuLabel(data.task_cpus)} vCPU</p>
      </div>
      <p className="text-[11px] leading-relaxed text-muted">
        推荐值按单任务资源上限估算，不代表压测容量。提高并发会减少每个任务可用资源，可能变慢或因内存不足失败。
        {data.execution_mode === 'in_process' && ' 当前为进程内执行，内存预算用于准入估算。'}
      </p>
      {!data.config_enabled && <p role="alert" className="text-xs text-amber-500">计算服务尚未接入全局策略配置。</p>}
      {limited && <p role="status" className="text-xs text-amber-500">已按当前资源预算限制为 {data.effective_concurrency}；可选择更小的值或恢复自动。</p>}
      {data.max_concurrency === 0 && <p role="alert" className="text-xs text-amber-500">策略资源预算不足，暂时无法运行任务。</p>}
      {error && <p role="alert" className="text-xs text-red-400">{message}</p>}
      {data.pending && <p role="status" className="text-xs text-amber-500">配置已保存，计算服务尚未应用；请检查部署配置。</p>}
      {save.isPending && <p role="status" className="text-xs text-muted">正在保存…</p>}
      {save.isSuccess && !data.pending && <p role="status" className="text-xs text-emerald-500">已保存，新任务按当前配置执行。</p>}
    </div>
  )
}
