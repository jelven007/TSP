import { useEffect, useRef } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { BellDot, Check, ExternalLink, Github, RefreshCw } from 'lucide-react'
import { OPEN_SOURCE_REPO, OPEN_SOURCE_REPO_URL, openSourceReleaseUrl } from '@/lib/api/openSourceRelease'
import { useOpenSourceMonitor } from '@/lib/openSourceMonitor'

type Monitor = ReturnType<typeof useOpenSourceMonitor>

function formatTime(value: string | number): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export function OpenSourceReleaseMonitor() {
  const monitor = useOpenSourceMonitor()
  const [searchParams, setSearchParams] = useSearchParams()
  const rowRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (searchParams.get('opensource') !== '1') return
    const frame = requestAnimationFrame(() => {
      rowRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      const next = new URLSearchParams(searchParams)
      next.delete('opensource')
      setSearchParams(next, { replace: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [searchParams, setSearchParams])

  return (
    <div ref={rowRef} className="mt-4 scroll-mt-24 border-t border-border pt-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h4 className="flex items-center gap-1.5 text-sm font-medium text-foreground">
            <Github className="h-4 w-4 text-secondary" />
            开源版本监控
          </h4>
          <a
            href={OPEN_SOURCE_REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-flex max-w-full items-center gap-1 text-xs text-secondary hover:text-accent"
          >
            <span className="break-all">{OPEN_SOURCE_REPO}</span>
            <ExternalLink className="h-3 w-3 shrink-0" />
          </a>
        </div>
        <button
          type="button"
          onClick={monitor.check}
          disabled={monitor.isChecking}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-btn bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${monitor.isChecking ? 'animate-spin' : ''}`} />
          {monitor.isChecking ? '检查开源版本中…' : '检查开源版本'}
        </button>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-muted">
        首次检查记录监控基准，打开应用期间每小时检查正式 Release，发现后续新发布时单独提醒。
      </p>
      {monitor.latest && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
          <a
            href={openSourceReleaseUrl(monitor.latest)}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-secondary hover:text-accent"
          >
            最近发布
            <span className="break-all font-mono font-medium">{monitor.latest.tag}</span>
            <ExternalLink className="h-3 w-3 shrink-0" />
          </a>
          <span className="text-muted">发布于 {formatTime(monitor.latest.publishedAt)}</span>
        </div>
      )}
      <div role="status" aria-live="polite" className="mt-2 text-xs">
        {monitor.hasNewRelease && monitor.latest ? (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-btn border border-accent/30 bg-accent/10 px-3 py-2">
            <span className="flex min-w-0 items-center gap-1.5 text-accent">
              <BellDot className="h-3.5 w-3.5 shrink-0" />
              开源仓库有新发布 <span className="break-all font-mono font-medium">{monitor.latest.tag}</span>
            </span>
            <div className="flex flex-wrap items-center gap-3">
              <a href={openSourceReleaseUrl(monitor.latest)} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                查看发布说明
              </a>
              <button type="button" onClick={monitor.markRead} className="inline-flex items-center gap-1 text-secondary hover:text-foreground">
                <Check className="h-3 w-3" />
                标记已读
              </button>
            </div>
          </div>
        ) : (
          <span className="text-secondary">
            {monitor.latest
              ? '暂无新的开源发布'
              : monitor.latest === null
                ? '开源仓库尚无正式 Release'
                : monitor.isChecking ? '正在获取开源发布信息…' : '尚未获取开源发布信息'}
          </span>
        )}
      </div>
      {monitor.error && <p role="alert" className="mt-2 text-xs text-warning">{monitor.error.message}</p>}
      <p className="mt-2 text-[11px] text-muted">
        {monitor.checkedAt ? `上次成功检查：${formatTime(monitor.checkedAt)}` : '尚未完成首次检查'}
      </p>
    </div>
  )
}

export function OpenSourceReleaseNotice({ monitor }: { monitor: Monitor }) {
  if (!monitor.hasNewRelease || !monitor.latest) return null
  return (
    <div role="status" aria-live="polite" className="mx-4 mt-4 flex flex-wrap items-center justify-between gap-2 rounded-card border border-accent/30 bg-surface px-4 py-3 text-xs">
      <span className="flex min-w-0 items-center gap-2 text-foreground">
        <Github className="h-4 w-4 shrink-0 text-accent" />
        开源仓库有新发布 <span className="break-all font-mono font-medium">{monitor.latest.tag}</span>
      </span>
      <div className="flex items-center gap-3">
        <Link to="/settings?tab=system&opensource=1" className="text-accent hover:underline">查看开源版本</Link>
        <button type="button" onClick={monitor.markRead} className="text-secondary hover:text-foreground">标记已读</button>
      </div>
    </div>
  )
}
