import { lazy, Suspense, useEffect, useState } from 'react'
import { Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { BookmarkCheck, FlaskConical, History, ShieldCheck } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { api, type ResearchCandidate } from '@/lib/api'
import { accountScope, multitenancyEnabled } from '@/lib/accountSession'
import { QK } from '@/lib/queryKeys'

const ResearchCandidatesDialog = lazy(() => import('./backtest/ResearchCandidatesDialog').then(module => ({ default: module.ResearchCandidatesDialog })))
const RobustnessValidation = lazy(() => import('./backtest/RobustnessValidation').then(module => ({ default: module.RobustnessValidation })))
const StrategyBacktest = lazy(() => import('./backtest/StrategyBacktest').then(module => ({ default: module.StrategyBacktest })))
const ResearchHistoryDialog = lazy(() => import('./backtest/ResearchHistoryDialog').then(module => ({ default: module.ResearchHistoryDialog })))

type Tab = 'strategy' | 'robustness'

const MODES: Record<Tab, { title: string; subtitle: string; icon: typeof FlaskConical }> = {
  strategy: {
    title: '策略',
    subtitle: '现有策略评估与候选沉淀',
    icon: FlaskConical,
  },
  robustness: {
    title: '验证',
    subtitle: '参数敏感性与滚动样本外',
    icon: ShieldCheck,
  },
}

export function Backtest() {
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const requestedTab = searchParams.get('tab')
  const [candidatesOpen, setCandidatesOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const needsDefaults = multitenancyEnabled()
  const defaults = useQuery({
    queryKey: QK.businessInitializationDefaults(accountScope()),
    queryFn: api.businessInitializationDefaults,
    enabled: needsDefaults,
    staleTime: Infinity,
    retry: false,
  })
  // 候选「载入复测」: 弹窗选定 → 关闭弹窗切到策略页 → StrategyBacktest 消费后清空
  const [pendingLoad, setPendingLoad] = useState<ResearchCandidate | null>(null)

  // 跨页「载入复测」: 因子页候选弹窗经 router state 传入 (location.state), 消费后清除防止刷新重复载入
  const stateCandidate = (location.state as { loadCandidate?: ResearchCandidate } | null)?.loadCandidate ?? null
  useEffect(() => {
    if (!stateCandidate) return
    setPendingLoad(stateCandidate)
    navigate({ pathname: location.pathname, search: location.search }, { replace: true })
  }, [stateCandidate, location.pathname, location.search, navigate])

  // 旧链接兼容: 因子已升级为一级路由 /factors, 保留其余参数重定向
  if (requestedTab === 'factor') {
    const next = new URLSearchParams(searchParams)
    next.delete('tab')
    const search = next.toString()
    return <Navigate to={search ? `/factors?${search}` : '/factors'} replace />
  }

  // 旧链接兼容: 挖掘已并入因子页 (/factors?tab=mining), 保留 run/candidate 参数重定向
  if (requestedTab === 'mining') {
    const next = new URLSearchParams(searchParams)
    next.set('tab', 'mining')
    return <Navigate to={`/factors?${next.toString()}`} replace />
  }

  const activeTab: Tab = requestedTab && requestedTab in MODES
    ? requestedTab as Tab
    : 'strategy'

  const changeTab = (tab: Tab) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', tab)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="flex min-h-full flex-col bg-base">
      <PageHeader
        title="回测"
        subtitle={MODES[activeTab].subtitle}
        right={(
          <div className="flex w-full min-w-0 items-center gap-1.5 sm:gap-2 lg:w-auto">
            {multitenancyEnabled() && <button type="button" onClick={() => setHistoryOpen(true)} className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-[11px] text-secondary hover:text-accent"><History className="h-3.5 w-3.5" /><span>任务历史</span></button>}
            <button
              type="button"
              onClick={() => setCandidatesOpen(true)}
              aria-label="打开候选方案"
              title="候选方案"
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-[11px] font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent sm:px-2.5 sm:text-xs"
            >
              <BookmarkCheck className="h-3.5 w-3.5" />
              <span>候选方案</span>
            </button>
            <span className="h-5 w-px shrink-0 bg-border" aria-hidden="true" />
            <nav className="min-w-0 flex-1 overflow-x-auto lg:flex-none" aria-label="回测视图">
              <div className="inline-flex min-w-max items-center gap-0.5 rounded-btn border border-border bg-surface/80 p-0.5">
                {(Object.keys(MODES) as Tab[]).map(tab => {
                  const mode = MODES[tab]
                  const Icon = mode.icon
                  const active = activeTab === tab
                  return (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => changeTab(tab)}
                      aria-current={active ? 'page' : undefined}
                      className={`inline-flex h-7 items-center gap-1 rounded-[5px] px-1.5 text-[11px] font-medium transition-colors sm:gap-1.5 sm:px-2.5 sm:text-xs ${active
                        ? 'bg-accent text-white shadow-sm'
                        : 'text-secondary hover:bg-elevated hover:text-foreground'
                      }`}
                    >
                      <Icon className="hidden h-3.5 w-3.5 sm:block" />
                      {mode.title}
                    </button>
                  )
                })}
              </div>
            </nav>
          </div>
        )}
      />

      <main className="page-content min-h-0 flex-1">
        <Suspense fallback={<div role="status" className="py-8 text-center text-sm text-muted">加载中…</div>}>
        {activeTab === 'strategy' && needsDefaults && defaults.isPending && (
          <div role="status" className="py-8 text-center text-sm text-muted">加载回测默认设置…</div>
        )}
        {activeTab === 'strategy' && needsDefaults && defaults.isError && (
          <div role="alert" className="py-8 text-center text-sm">
            <p>回测默认设置加载失败。</p>
            <button type="button" className="mt-2 text-accent" onClick={() => void defaults.refetch()}>重试</button>
          </div>
        )}
        {activeTab === 'strategy' && (!needsDefaults || defaults.isSuccess) && (
          <StrategyBacktest
            key={accountScope()}
            initializationDefaults={defaults.data?.backtest}
            loadCandidate={pendingLoad}
            onLoadConsumed={() => setPendingLoad(null)}
          />
        )}
        {activeTab === 'robustness' && <RobustnessValidation />}
        </Suspense>
      </main>

      {candidatesOpen && (
        <Suspense fallback={<div role="status" className="text-center text-sm text-muted">加载候选方案…</div>}>
        <ResearchCandidatesDialog
          onClose={() => setCandidatesOpen(false)}
          onLoadStrategy={candidate => {
            setPendingLoad(candidate)
            setCandidatesOpen(false)
            if (activeTab !== 'strategy') changeTab('strategy')
          }}
        />
        </Suspense>
      )}
      {historyOpen && (
        <Suspense fallback={<div role="status" className="text-center text-sm text-muted">加载任务历史…</div>}>
          <ResearchHistoryDialog onClose={() => setHistoryOpen(false)} />
        </Suspense>
      )}
    </div>
  )
}
