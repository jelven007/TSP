import { useEffect, useMemo, useRef, useState, Suspense, type ReactNode } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useQuoteStream, useQuoteStreamStatus } from '@/lib/useQuoteStream'
import { ToastContainer } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import { TenantSelector } from '@/components/TenantSelector'
import { OpenSourceReleaseNotice } from '@/components/OpenSourceReleaseMonitor'
import {
  usePreferences,
  useQuoteStatus,
  useVersion,
} from '@/lib/useSharedQueries'
import { useUpdateCheck } from '@/lib/updateCheck'
import { useOpenSourceMonitor } from '@/lib/openSourceMonitor'
import { QK } from '@/lib/queryKeys'
import {
  Settings,
  Loader2,
  Tags,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  ChevronDown,
  Sun,
  Moon,
  X,
  WifiOff,
  Menu,
  PanelLeft,
  PanelLeftClose,
  PanelLeftOpen,
  Download,
} from 'lucide-react'
import {
  IconDashboard,
  IconWatchlist,
  IconStrategy,
  IconFactors,
  IconBacktest,
  IconStockFocus,
  IconLadder,
  IconConcept,
  IconIndustry,
  IconFinancials,
  IconMonitor,
  IconRegime,
  IconAlert,
  IconLots,
  IconPaper,
  IconSignals,
  IconReview,
  IconData,
  type BrandIconProps,
} from './BrandIcons'
import { Logo } from './Logo'
import { api } from '@/lib/api'
import { accountStorage as localStorage, hasPermission } from '@/lib/accountSession'
import { cn } from '@/lib/cn'
import { useIsDesktop } from '@/lib/useMediaQuery'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'
import { computeGroupPcts, groupPctColor, groupPctTitle } from '@/lib/watchlistGroupStats'
import { fmtPct } from '@/lib/format'
import { toggleTheme, useTheme } from '@/lib/theme'
import { setCurrentTotal as setAlertTotal, useUnreadAlerts } from '@/lib/monitorBadge'
import { ExtensionSlot } from '@/extensions/ExtensionSlot'
import { getFrontendExtensionNavigation } from '@/extensions/registry'

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色
const BRAND = '#8B5CF6'

// 核心四只指数 — 与后端 index_const.py 单一权威对齐 (前端展示层固定清单)
export const CORE_INDEXES = [
  { symbol: '000001.SH', name: '上证指数', shortName: '上指' },
  { symbol: '399001.SZ', name: '深证成指', shortName: '深成' },
  { symbol: '399006.SZ', name: '创业板指', shortName: '创指' },
  { symbol: '000680.SH', name: '科创综指', shortName: '科综' },
] as const

const nav = [
  { to: '/',                 label: '行情',     icon: IconDashboard },
  { to: '/regime',           label: '环境', icon: IconRegime },
  { to: '/watchlist',        label: '自选',     icon: IconWatchlist },
  { to: '/screener',         label: '策略',     icon: IconStrategy },
  { to: '/factors',          label: '因子',     icon: IconFactors },
  { to: '/backtest',         label: '回测',     icon: IconBacktest },
  { to: '/review',           label: '复盘',     icon: IconReview },
  { to: '/abnormal',         label: '异动', icon: IconAlert },
  { to: '/lots',             label: '持仓提醒', icon: IconLots },
  { to: '/monitor',          label: '监控中心', icon: IconMonitor },
  { to: '/limit-ladder',     label: '连板梯队', icon: IconLadder },
  { to: '/stock-analysis',   label: '个股分析', icon: IconStockFocus },
  { to: '/concept-analysis', label: '概念分析', icon: IconConcept },
  { to: '/industry-analysis', label: '行业分析', icon: IconIndustry },
  { to: '/financials',       label: '财务分析', icon: IconFinancials },
  { to: '/signals',          label: '信号指标',   icon: IconSignals },
  { to: '/paper',            label: '模拟交易',   icon: IconPaper },
  { to: '/data',             label: '数据总览',     icon: IconData },
] as const

/** 亮/暗主题切换 — 状态存 localStorage, 生效见 lib/theme.ts */
function ThemeToggle() {
  const theme = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      onClick={() => toggleTheme()}
      className="flex items-center justify-center rounded-btn p-2 text-foreground/80 transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground cursor-pointer"
      title={dark ? '切换到亮色模式' : '切换到暗色模式'}
    >
      {dark ? <Sun className="h-4 w-4 shrink-0" /> : <Moon className="h-4 w-4 shrink-0" />}
    </button>
  )
}

/** 监控中心未读徽标 — 仅在非监控页且有未读时显示。 */
function MonitorBadge({ active }: { active: boolean }) {
  const unread = useUnreadAlerts()
  // 尊重用户设置: 可在菜单设置里关闭数字提示
  const badgeEnabled = (() => {
    try { return localStorage.getItem('monitor_badge_enabled') !== '0' } catch { return true }
  })()
  if (active || unread <= 0 || !badgeEnabled) return null
  return (
    <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[9px] font-bold text-white animate-pulse">
      {unread > 99 ? '99+' : unread}
    </span>
  )
}

// 侧边栏桌面三态: expanded(9.8rem) / rail(3.5rem 图标条) / hidden(0 + 左缘悬浮按钮)。
// 移动端 (<768px) 不参与三态 — aside 以抽屉呈现 (见 Layout 内 drawerOpen)。
type NavState = 'expanded' | 'rail' | 'hidden'

export function Layout() {
  // ===== 共享 hooks (替代内联 useQuery) =====
  const canManageData = hasPermission('data:manage')
  const canManageSettings = hasPermission('settings:manage')
  const { data: versionData } = useVersion()
  // 更新检查 (单例 store): 启动静默查一次 GitHub Releases, 供左下角版本号徽标
  const update = useUpdateCheck(canManageSettings)
  const hasUpdate = canManageSettings && update.status === 'found'
  const openSourceMonitor = useOpenSourceMonitor({ enabled: canManageSettings, poll: true })
  const { data: prefs } = usePreferences()
  // poll=true: 全局唯一开启条件轮询 (非交易时段 60s 兜底, 交易时段靠 SSE)
  const { data: quoteStatus } = useQuoteStatus({ poll: true })
  const { data: analysisMenus } = useQuery({
    queryKey: QK.analysisMenus,
    queryFn: api.analysisMenus,
  })

  // 自选分组 — 仅当用户开启「显示在侧边栏」时拉取
  const groupsInNav = prefs?.watchlist_groups_in_nav ?? false
  const location = useLocation()
  const { data: watchlistGroupsData } = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
    enabled: groupsInNav,
    staleTime: 60_000,
  })
  const watchlistGroups = watchlistGroupsData?.groups ?? []
  // 自选二级菜单展开状态 — 默认当前在自选页时展开
  const [watchlistNavExpanded, setWatchlistNavExpanded] = useState(location.pathname === '/watchlist')

  // 侧边栏三态 — expanded(9.8rem) / rail(3.5rem 图标条) / hidden(0, 左缘悬浮按钮唤出)。
  // 仅桌面 (≥768px) 参与三态; 移动端 aside 以抽屉呈现, 由 drawerOpen 控制, 恒渲染完整形态。
  // 持久化到 localStorage; 迁移旧两态键 tf-nav-collapsed (收起 → 图标条)。
  const [navState, setNavState] = useState<NavState>(() => {
    try {
      const v = localStorage.getItem('tf-nav-state')
      if (v === 'expanded' || v === 'rail' || v === 'hidden') return v
      return localStorage.getItem('tf-nav-collapsed') === '1' ? 'rail' : 'expanded'
    } catch { return 'expanded' }
  })
  const [drawerOpen, setDrawerOpen] = useState(false)
  const isDesktop = useIsDesktop()
  // 桌面 hidden 态的左缘悬浮按钮: hover 1s 以 overlay 预览 (不挤压主区), 点击固定展开 (push)
  const [overlayPreview, setOverlayPreview] = useState(false)
  const overlayTimer = useRef<number | undefined>(undefined)
  const setNavStatePersist = (s: NavState) => {
    setNavState(s)
    try { localStorage.setItem('tf-nav-state', s) } catch {}
  }
  // 图标条形态仅桌面 rail 态成立 (移动端抽屉与 overlay 预览恒为完整形态)
  const railMode = isDesktop && navState === 'rail'
  // 路由跳转/切回桌面时关抽屉; ESC 同样关闭
  useEffect(() => { setDrawerOpen(false) }, [location.pathname, isDesktop])
  useEffect(() => {
    if (!drawerOpen) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setDrawerOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  // 分组等权平均涨跌幅 — 复用 watchlist/enriched 查询缓存(与自选页同 key,
  // 盘中随 SSE 刷新)。可见性门控: 子菜单实际可见(桌面展开 + 二级菜单展开, 或
  // 移动端抽屉打开) 时才拉取, 收起/隐藏/抽屉关闭下不为隐藏 UI 发请求。
  const sidebarFullyVisible = navState === 'expanded' && (isDesktop || drawerOpen)
  const navGroupPctVisible = groupsInNav && sidebarFullyVisible && watchlistNavExpanded
  const { data: navWatchlist } = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: navGroupPctVisible,
    staleTime: 60_000,
  })
  const { data: navEnriched } = useQuery({
    queryKey: QK.watchlistEnriched(undefined),
    queryFn: () => api.watchlistEnriched(),
    enabled: navGroupPctVisible,
    staleTime: 60_000,
  })
  const navGroupPcts = useMemo(
    () => computeGroupPcts(
      navWatchlist?.symbols ?? [],
      new Map((navEnriched?.rows ?? []).map((r: any) => [r.symbol as string, r])),
    ),
    [navWatchlist, navEnriched],
  )

  // 数据同步状态轮询: 有活跃 job 时「数据」菜单项显示转圈。
  // 标签页隐藏时 TanStack 默认暂停轮询 — 转圈提示只在页面可见时有意义, 不再强制后台轮询
  const { data: pipelineJobs } = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(1),
    enabled: canManageData,
    refetchInterval: (query) => (query.state.data?.active_id ? 2000 : 15000),
  })
  const isDataSyncing = !!pipelineJobs?.active_id

  // 数据同步完成的"瞬时反馈": isDataSyncing 从 true→false 时显示绿色对勾,
  // 闪烁约 3 秒后自动消失。
  const [dataSyncJustDone, setDataSyncJustDone] = useState(false)
  const prevSyncingRef = useRef(false)
  useEffect(() => {
    // 仅在"刚结束"(true→false)且非首次挂载时触发
    if (prevSyncingRef.current && !isDataSyncing) {
      setDataSyncJustDone(true)
      const t = setTimeout(() => setDataSyncJustDone(false), 3000)
      prevSyncingRef.current = isDataSyncing
      return () => clearTimeout(t)
    }
    prevSyncingRef.current = isDataSyncing
  }, [isDataSyncing])

  const navigate = useNavigate()
  const version = versionData?.version
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  // 三态循环切换 (仅桌面): 展开 → 图标条 → 隐藏 → 展开
  const toggleNavCollapsed = () => {
    setNavStatePersist(navState === 'expanded' ? 'rail' : navState === 'rail' ? 'hidden' : 'expanded')
  }
  // SSE: 行情更新时自动刷新相关 queries + 告警通知
  useQuoteStream(realtimeEnabled, prefs?.sse_refresh_pages)
  // 实时 SSE 连接状态 — 断开时底部显示提示, 提示可能漏策略告警
  const streamStatus = useQuoteStreamStatus()

  const isTrading = quoteStatus?.is_trading_hours ?? false

  // 轮询触发记录总数 → 更新监控中心徽标 (每 15 秒; 后台标签页由 SSE 事件驱动, 不轮询)。
  // 非交易时段无新告警, 降为 2 分钟兜底
  const alertsTotalQuery = useQuery({
    queryKey: ['alerts-total'],
    queryFn: () => api.alertsList({ days: 7, limit: 1 }),
    refetchInterval: () => (isTrading ? 15000 : 120000),
    select: (data) => data.total,
  })
  // 只在拿到真实总数时同步徽标 (避免 data=undefined 时传 0 重置 lastSeen)
  const alertsTotal = alertsTotalQuery.data
  useEffect(() => {
    if (alertsTotal != null) setAlertTotal(alertsTotal)
  }, [alertsTotal])

  // 合并内置页面 + 可见的扩展分析菜单
  type NavIcon = (props: BrandIconProps) => ReactNode
  type NavItem = { to: string; label: string; icon: NavIcon; badge?: string }
  const analysisNav: NavItem[] = (analysisMenus?.items ?? [])
    .filter(m => m.visible)
    .map(m => ({ to: `/analysis/${m.id}`, label: m.label, icon: m.icon === 'tags' ? Tags : BarChart3 }))
  const extensionNav: NavItem[] = getFrontendExtensionNavigation().map(item => ({
    to: item.route.path,
    label: item.label,
    icon: item.icon,
    badge: item.badge,
  }))

  const allNav: NavItem[] = [...nav.filter(item => item.to !== '/data' || canManageData), ...analysisNav, ...extensionNav]
  const savedOrder = prefs?.nav_order ?? []

  const navItems = savedOrder.length > 0
    ? (() => {
        const byTo = new Map(allNav.map(n => [n.to, n]))
        const ordered = (savedOrder
          .map(id => byTo.get(id) ?? byTo.get(`/analysis/${id}`))
          .filter(Boolean)) as typeof allNav
        const seen = new Set(ordered.map(n => n.to))
        const merged = [...ordered]
        for (const item of allNav) {
          if (seen.has(item.to)) continue
          // 未保存过排序的新条目: 内置页插回默认位置(排在已保存的默认前驱之后),
          // 分析/扩展菜单仍追加到末尾
          const defaultIndex = nav.findIndex(n => n.to === item.to)
          let anchor = -1
          if (defaultIndex > 0) {
            for (let i = defaultIndex - 1; i >= 0 && anchor < 0; i -= 1) {
              anchor = merged.findIndex(n => n.to === nav[i].to)
            }
          }
          if (anchor >= 0) merged.splice(anchor + 1, 0, item)
          else if (defaultIndex >= 0) merged.unshift(item)
          else merged.push(item)
        }
        return merged
      })()
    : allNav

  const hiddenIds = new Set(prefs?.nav_hidden ?? [])
  const visibleNavItems = navItems.filter(n => !hiddenIds.has(n.to) && !hiddenIds.has(n.to.replace(/^\/analysis\//, '')))

  return (
    <div
      className="h-full grid bg-base text-foreground overflow-hidden transition-[grid-template-columns] duration-200 ease-smooth"
      style={{ gridTemplateColumns: isDesktop && !overlayPreview ? (navState === 'expanded' ? '9.8rem 1fr' : navState === 'rail' ? '3.5rem 1fr' : '0 1fr') : '1fr' }}
    >
      {/* 移动端抽屉遮罩 */}
      {!isDesktop && drawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50"
          onClick={() => setDrawerOpen(false)}
          aria-hidden="true"
        />
      )}
      {/* 移动端汉堡按钮 / 桌面 hidden 态左缘悬浮按钮 (hover 1s overlay 预览, 点击固定展开) */}
      {(!isDesktop || navState === 'hidden') && !overlayPreview && (
        <button
          onClick={() => {
            window.clearTimeout(overlayTimer.current)
            setOverlayPreview(false)
            if (isDesktop) setNavStatePersist('expanded')
            else setDrawerOpen(true)
          }}
          onMouseEnter={() => {
            if (!isDesktop) return
            window.clearTimeout(overlayTimer.current)
            overlayTimer.current = window.setTimeout(() => setOverlayPreview(true), 1000)
          }}
          onMouseLeave={() => window.clearTimeout(overlayTimer.current)}
          className={cn(
            'fixed z-30 rounded-btn border border-border bg-surface/90 text-muted shadow-lg backdrop-blur-sm',
            'hover:text-foreground hover:bg-elevated transition-colors duration-150 ease-smooth',
            isDesktop ? 'left-1.5 top-1/2 -translate-y-1/2 p-2' : 'left-3 top-3 p-2',
          )}
          title={isDesktop ? '展开菜单' : '打开菜单'}
        >
          {isDesktop
            ? <PanelLeftOpen className="h-4 w-4 shrink-0" />
            : <Menu className="h-4 w-4 shrink-0" />}
        </button>
      )}
      <aside
        onMouseLeave={() => { if (overlayPreview) setOverlayPreview(false) }}
        className={cn(
          'bg-surface flex flex-col min-h-0 overflow-hidden',
          isDesktop
            ? cn('h-full', navState === 'hidden' && !overlayPreview ? 'border-r-0' : 'border-r border-border')
            : cn(
                'fixed inset-y-0 left-0 z-50 w-[80vw] max-w-[320px] border-r border-border shadow-2xl',
                'transition-transform duration-200 ease-smooth',
                drawerOpen ? 'translate-x-0' : '-translate-x-full',
              ),
          overlayPreview && 'fixed inset-y-0 left-0 z-50 w-[9.8rem] shadow-2xl border-r border-border',
        )}
      >
        <div className={cn('border-b border-border shrink-0', railMode ? 'px-2 pt-3 pb-2' : 'px-4 pt-4 pb-3 md:px-3')}>
          {/* Brand block — 收起时只显 logo 居中 */}
          <div className={cn('flex', railMode ? 'flex-col items-center gap-2' : 'items-center gap-2')}>
            <Logo
              size={railMode ? 24 : 26}
              className="shrink-0 drop-shadow-[0_0_8px_rgba(139,92,246,0.4)]"
              style={{ color: BRAND }}
            />
            {!railMode && (
              <div
                className="font-bold text-[11px] uppercase tracking-[0.14em] text-foreground whitespace-nowrap"
                style={{ textShadow: `0 0 10px ${BRAND}44` }}
              >
                TSP
              </div>
            )}
            {/* 收起/展开 按钮 (桌面三态循环) / 移动端抽屉关闭按钮 */}
            {isDesktop ? (
              <button
                onClick={toggleNavCollapsed}
                className={cn(
                  'flex items-center rounded-btn text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth',
                  railMode ? 'justify-center p-1.5' : 'ml-auto p-1.5',
                )}
                title={railMode ? '隐藏菜单 (再点击左缘按钮可唤出)' : '收起菜单'}
              >
                {railMode
                  ? <PanelLeft className="h-3.5 w-3.5 shrink-0" />
                  : <PanelLeftClose className="h-3.5 w-3.5 shrink-0" />
                }
              </button>
            ) : (
              <button
                onClick={() => setDrawerOpen(false)}
                className="ml-auto flex items-center rounded-btn p-1.5 text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth"
                title="关闭菜单"
              >
                <X className="h-4 w-4 shrink-0" />
              </button>
            )}
          </div>

        </div>

        <TenantSelector compact={railMode} />
        <nav className="flex-1 min-h-0 overflow-y-auto px-2 py-3 space-y-0.5">
          {visibleNavItems.map(({ to, label, icon: Icon, badge }) => {
            // 「自选」项 — 开启分组侧栏且未整体收起时, 渲染为可展开父项 + 二级分组
            const isWatchlistExpandable = to === '/watchlist' && groupsInNav && !railMode && watchlistGroups.length > 0
            return (
              <div key={to}>
                {isWatchlistExpandable ? (
                  /* 可展开的自选父项 — 点击切换展开, 不直接跳页 */
                  <button
                    onClick={() => setWatchlistNavExpanded(v => !v)}
                    className={cn(
                      'group relative flex w-full items-center gap-3 rounded-btn px-3 py-2 text-sm transition-all duration-150 ease-smooth md:gap-2 md:px-2',
                      location.pathname === '/watchlist'
                        ? 'bg-elevated text-foreground font-medium'
                        : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                    )}
                  >
                    <span
                      className={cn(
                        'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                        location.pathname === '/watchlist' ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                      )}
                    />
                    <Icon className={cn('h-4 w-4 shrink-0 transition-colors', location.pathname === '/watchlist' ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                    <span className="flex-1 whitespace-nowrap text-left">{label}</span>
                    {watchlistNavExpanded
                      ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
                      : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />
                    }
                  </button>
                ) : (
                  /* 普通菜单项 */
                  <NavLink
                    to={to}
                    title={railMode ? label : undefined}
                    className={({ isActive }) =>
                      cn(
                        'group relative flex items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
                        railMode ? 'justify-center px-0 py-2' : 'gap-3 px-3 py-2 md:gap-2 md:px-2',
                        isActive
                          ? 'bg-elevated text-foreground font-medium'
                          : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {/* active 左侧 accent 竖条指示 */}
                        <span
                          className={cn(
                            'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                            isActive ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                          )}
                        />
                        <Icon className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                        {!railMode && <span className="min-w-0 flex-1 truncate">{label}</span>}
                        {!railMode && badge && (
                          <span className="ml-auto inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400 shrink-0">
                            {badge}
                          </span>
                        )}
                        {/* 数据同步状态: 同步中转圈, 刚完成显示绿色对勾闪烁 3 秒 */}
                        {to === '/data' && isDataSyncing && !railMode && (
                          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                        )}
                        {to === '/data' && !isDataSyncing && dataSyncJustDone && !railMode && (
                          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bull animate-pulse" />
                        )}
                        {/* 监控中心徽标: 仅非监控页且有未读时显示 */}
                        {to === '/monitor' && !railMode && <MonitorBadge active={isActive} />}
                      </>
                    )}
                  </NavLink>
                )}

                {/* 自选分组二级子菜单 — 展开时显示 */}
                {isWatchlistExpandable && watchlistNavExpanded && (
                  <div className="mt-0.5 space-y-0.5">
                    <NavLink
                      to="/watchlist"
                      className={({ isActive }) => cn(
                        'flex items-center gap-2 rounded-btn py-1.5 pl-9 pr-3 text-[12px] transition-colors duration-150 ease-smooth md:pl-8 md:pr-2',
                        isActive && !location.search
                          ? 'text-accent font-medium'
                          : 'text-foreground/60 hover:text-foreground hover:bg-elevated/50',
                      )}
                    >
                      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted" />
                      <span>全部</span>
                      {(() => {
                        const info = navGroupPcts['all']
                        return info && info.pct != null ? (
                          <span className={`ml-auto font-mono text-[10px] tabular-nums ${groupPctColor(info.pct)}`} title={groupPctTitle(info)}>
                            {fmtPct(info.pct)}
                          </span>
                        ) : null
                      })()}
                    </NavLink>
                    {watchlistGroups.map(group => {
                      const color = resolveWatchlistGroupColor(group.color)
                      const groupPath = `/watchlist?group=${group.id}`
                      const isGroupActive = location.pathname === '/watchlist' && location.search === `?group=${group.id}`
                      const pctInfo = navGroupPcts[group.id]
                      return (
                        <NavLink
                          key={group.id}
                          to={groupPath}
                          className={cn(
                            'flex items-center gap-2 rounded-btn py-1.5 pl-9 pr-3 text-[12px] transition-colors duration-150 ease-smooth md:pl-8 md:pr-2',
                            isGroupActive
                              ? 'text-accent font-medium'
                              : 'text-foreground/60 hover:text-foreground hover:bg-elevated/50',
                          )}
                        >
                          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${color.dot}`} />
                          <span className="truncate">{group.name}</span>
                          {pctInfo && pctInfo.pct != null && (
                            <span className={`ml-auto font-mono text-[10px] tabular-nums ${groupPctColor(pctInfo.pct)}`} title={groupPctTitle(pctInfo)}>
                              {fmtPct(pctInfo.pct)}
                            </span>
                          )}
                        </NavLink>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
          <ExtensionSlot
            name="layout.navigation.extra"
            context={{ collapsed: railMode, pathname: location.pathname }}
            compact
          />
        </nav>

        <div className={cn('border-t border-border py-3 shrink-0', railMode ? 'px-2 flex flex-col items-center gap-1' : 'px-2')}>
          <div className={railMode ? 'flex flex-col items-center gap-1' : 'flex items-center gap-1 md:gap-0'}>
            <ThemeToggle />
            <NavLink
              to="/settings"
              title={railMode ? '设置' : undefined}
              className={({ isActive }) =>
                cn(
                  'group relative flex items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
                  railMode ? 'justify-center px-0 py-2' : 'min-w-0 flex-1 gap-3 px-3 py-2 md:gap-1 md:px-1',
                  isActive
                    ? 'bg-elevated text-foreground font-medium'
                    : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <span
                    className={cn(
                      'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                      isActive ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                    )}
                  />
                  <Settings className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                  {!railMode && <span className="whitespace-nowrap">设置</span>}
                  {!railMode && version && (
                    <span className="ml-auto flex min-w-0 items-center gap-1.5 font-mono text-[10px] text-muted/50 select-none" title={version}>
                      <span className="truncate">{version}</span>
                      {hasUpdate && update.info && (
                        <span
                          role="button"
                          tabIndex={0}
                          title={`发现新版本 ${update.info.latest}，点击检查更新`}
                          onClick={(e) => {
                            e.preventDefault()
                            e.stopPropagation()
                            navigate('/settings?tab=system&autoupdate=1')
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              e.stopPropagation()
                              navigate('/settings?tab=system&autoupdate=1')
                            }
                          }}
                          className="inline-flex shrink-0 cursor-pointer text-amber-400 transition-transform hover:scale-110 animate-breath"
                        >
                          <Download className="h-3 w-3" />
                        </span>
                      )}
                    </span>
                  )}
                  {railMode && hasUpdate && (
                    <span
                      className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_6px_rgba(59,130,246,0.8)]"
                      title="发现新版本"
                    />
                  )}
                </>
              )}
            </NavLink>
          </div>
        </div>
      </aside>

      <motion.main
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="h-full overflow-auto scrollbar-gutter-stable"
      >
        {streamStatus === 'reconnecting' && (
          <div
            role="status"
            aria-live="polite"
            className="fixed bottom-4 left-1/2 z-[9998] flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-warning/30 bg-warning/10 px-2.5 py-1 text-[11px] font-medium text-warning shadow-lg backdrop-blur-md"
          >
            <WifiOff className="h-3 w-3 shrink-0 animate-pulse" />
            与服务连接已断开 · 正在重连
          </div>
        )}
        {!(location.pathname === '/settings' && new URLSearchParams(location.search).get('tab') === 'system') && (
          <OpenSourceReleaseNotice monitor={openSourceMonitor} />
        )}
        <Suspense
          fallback={
            <div className="flex items-center justify-center py-24">
              <Loader2 className="h-5 w-5 animate-spin text-muted" />
            </div>
          }
        >
          <Outlet />
        </Suspense>
      </motion.main>
      <ToastContainer />
      <AlertToastContainer />
      <AiAnalysisHost />
      <AiReportBubble />
      <StockAnalysisHost />
      <StockAnalysisBubble />

    </div>
  )
}
