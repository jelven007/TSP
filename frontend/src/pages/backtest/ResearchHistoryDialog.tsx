import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Download, History, RotateCcw, Square, Trash2, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api, type ResearchJob } from '@/lib/api'
import { multitenancyEnabled } from '@/lib/accountSession'
import { attachBacktestJob } from '@/lib/backtestTask'
import { attachOptimizeJob } from '@/lib/optimizerTask'
import { attachWalkForwardJob } from '@/lib/walkforwardTask'

const QUERY_KEY = ['research-jobs'] as const
const ACTIVE = new Set(['queued', 'running', 'cancelling'])
const KINDS: Record<string, string> = {
  backtest: '策略回测', optimize: '参数优化', walkforward: '步进优化', mining: '因子挖掘',
  signal_backtest: '信号回测', factor_backtest: '因子回测', factor_batch: '因子批量回测', auto_screen: '因子筛选',
}
const STATES: Record<string, string> = {
  queued: '等待执行', running: '执行中', cancelling: '取消中', succeeded: '已完成',
  succeeded_with_budget_exhausted: '预算内完成', failed: '失败', cancelled: '已取消',
  interrupted: '中断', skipped_prerequisite: '数据条件不足',
}

export function ResearchHistoryDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('all')
  const jobs = useQuery({ queryKey: QUERY_KEY, queryFn: api.researchJobs, refetchInterval: 5000 })
  const originals = useQuery({ queryKey: ['research-originals'], queryFn: api.researchOriginals, enabled: multitenancyEnabled() })
  const cancel = useMutation({ mutationFn: api.researchJobCancel, onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }) })
  const remove = useMutation({ mutationFn: api.researchJobDelete, onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }) })
  const download = useMutation({
    mutationFn: async (id: string) => {
      const data = await api.researchJob(id)
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `research-${id}.json`
      link.click()
      URL.revokeObjectURL(url)
    },
  })
  const downloadOriginal = useMutation({
    mutationFn: async (item: { path: string; sha256: string }) => {
      const blob = await api.researchOriginal(item.path, item.sha256)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = item.path.split('/').at(-1) || 'original'
      link.click()
      URL.revokeObjectURL(url)
    },
  })
  const open = (job: ResearchJob) => {
    if (job.kind === 'backtest') { attachBacktestJob(job.id); navigate('/backtest?tab=strategy') }
    else if (job.kind === 'optimize') { attachOptimizeJob(job.id); navigate('/backtest?tab=robustness&mode=sensitivity') }
    else if (job.kind === 'walkforward') { attachWalkForwardJob(job.id); navigate('/backtest?tab=robustness&mode=walkforward') }
    else if (job.kind === 'mining') navigate(`/factors?tab=mining&run=${encodeURIComponent(job.id)}`)
    onClose()
  }
  const visible = (jobs.data?.items ?? []).filter(job => filter === 'all' || (filter === 'active' ? ACTIVE.has(job.status) : !ACTIVE.has(job.status)))
  const error = jobs.error || originals.error || cancel.error || remove.error || download.error || downloadOriginal.error
  return (
    <Modal onClose={onClose} labelledBy="research-history-title" panelClassName="flex max-h-[85vh] w-[94vw] max-w-5xl flex-col rounded-card border border-border bg-surface shadow-xl">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <History className="h-4 w-4 text-accent" /><h2 id="research-history-title" className="flex-1 text-sm font-medium">研究任务历史</h2>
        <select aria-label="筛选任务状态" value={filter} onChange={event => setFilter(event.target.value)} className="rounded-btn border border-border bg-base px-2 py-1 text-xs"><option value="all">全部</option><option value="active">执行中</option><option value="finished">已结束</option></select>
        <button type="button" aria-label="关闭任务历史" onClick={onClose} className="p-1 text-muted"><X className="h-4 w-4" /></button>
      </div>
      <p className="px-4 pt-3 text-xs text-muted">查看当前租户的任务进度与结果。页面关闭后任务继续执行，可随时回来查看。</p>
      {error && <p role="alert" className="px-4 pt-3 text-xs text-danger">{error.message}</p>}
      <div className="overflow-auto p-4">
        {!visible.length && <p className="py-10 text-center text-sm text-muted">{jobs.isLoading ? '正在加载…' : '暂无研究任务'}</p>}
        {!!visible.length && <table className="w-full min-w-[650px] text-left text-xs"><thead className="border-b border-border text-muted"><tr><th className="py-2">任务</th><th>状态</th><th>创建时间</th><th>来源</th><th className="text-right">操作</th></tr></thead>
          <tbody>{visible.map(job => <tr key={job.id} className="border-b border-border/50">
            <td className="max-w-80 py-3 pr-4"><span className="block font-medium">{KINDS[job.kind] || job.kind}{job.config?.strategy_id ? ` · ${job.config.strategy_id}` : ''}</span><span className="mt-1 block font-mono text-[10px] text-muted">{job.id.slice(0, 12)}</span>{job.error && <span className="mt-1 block text-danger">{job.error}</span>}</td>
            <td className={ACTIVE.has(job.status) ? 'text-accent' : 'text-secondary'}>{STATES[job.status] || job.status}</td>
            <td className="text-muted">{new Date(job.created_at).toLocaleString('zh-CN', { hour12: false })}</td>
            <td className="text-muted">{job.metadata.source === 'scheduled' ? '定时' : job.metadata.source === 'legacy' ? '历史导入' : '手动'}</td>
            <td><div className="flex justify-end gap-2">
              {['backtest', 'optimize', 'walkforward', 'mining'].includes(job.kind) && <button type="button" onClick={() => open(job)} className="inline-flex items-center gap-1 text-accent"><RotateCcw className="h-3 w-3" />查看</button>}
              {!ACTIVE.has(job.status) && <button type="button" aria-label={`导出任务 ${job.id}`} disabled={download.isPending} onClick={() => download.mutate(job.id)} className="p-1 text-secondary"><Download className="h-3.5 w-3.5" /></button>}
              {ACTIVE.has(job.status) ? <button type="button" aria-label={`取消任务 ${job.id}`} disabled={cancel.isPending || job.status === 'cancelling'} onClick={() => cancel.mutate(job.id)} className="p-1 text-danger"><Square className="h-3.5 w-3.5" /></button> : <button type="button" aria-label={`删除任务 ${job.id}`} disabled={remove.isPending} onClick={() => remove.mutate(job.id)} className="p-1 text-muted hover:text-danger"><Trash2 className="h-3.5 w-3.5" /></button>}
            </div></td>
          </tr>)}</tbody>
        </table>}
        {!!originals.data?.items.length && <div className="mt-5 border-t border-border pt-4">
          <h3 className="text-sm font-medium">迁移前的原始文件</h3>
          <p className="mt-1 text-xs text-muted">保留原始报告、记录与产物。只有清单的历史回测可导出原记录，结果以文件中保存的内容为准。</p>
          <ul className="mt-3 space-y-2">{originals.data.items.map(item => <li key={item.path} className="flex items-center gap-3 text-xs">
            <span className="flex-1 break-all text-secondary">{item.path}</span>
            <span className="whitespace-nowrap text-muted">{(item.bytes / 1024).toFixed(1)} KB</span>
            <button type="button" aria-label={`导出原始文件 ${item.path}`} disabled={downloadOriginal.isPending} onClick={() => downloadOriginal.mutate(item)} className="p-1 text-accent"><Download className="h-3.5 w-3.5" /></button>
          </li>)}</ul>
        </div>}
      </div>
    </Modal>
  )
}
