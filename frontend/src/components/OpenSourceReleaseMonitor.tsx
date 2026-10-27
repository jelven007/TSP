import { useEffect, useRef } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ExternalLink, Github } from 'lucide-react'
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
      </div>
      {monitor.latest && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
          <a
            href={openSourceReleaseUrl(monitor.latest)}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-secondary hover:text-accent"
          >
            最新版本号
            <span className="break-all font-mono font-medium">{monitor.latest.tag}</span>
            <ExternalLink className="h-3 w-3 shrink-0" />
          </a>
          <span className="text-muted">发布时间 {formatTime(monitor.latest.publishedAt)}</span>
        </div>
      )}
      {!monitor.latest && (
        <p role="status" aria-live="polite" className="mt-2 text-xs text-secondary">
          {monitor.latest === null
            ? '开源仓库尚无正式 Release'
            : monitor.isChecking ? '正在获取开源发布信息…' : '尚未获取开源发布信息'}
        </p>
      )}
      {monitor.error && <p role="alert" className="mt-2 text-xs text-warning">{monitor.error.message}</p>}
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
