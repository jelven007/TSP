import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, BookOpen, LogOut, Wallet, Star, Sun, Moon } from 'lucide-react'
import { Logo } from '@/components/Logo'
import { ApiError } from '@/lib/api/client'
import {
  cellSession, loginCell, workspaceClient,
  type CellStrategy, type CellRule, type Workspace, type WorkspaceClient,
} from '@/lib/cellApi'
import { QK } from '@/lib/queryKeys'

const SESSION_KEY = 'tsp-cell-session-v1'
const inputStyle = 'rounded-md border bg-surface px-3 py-2 text-sm min-w-0 focus:outline-none focus:ring-2 focus:ring-accent/50'
const buttonStyle = 'rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-110 transition'
const secondaryStyle = 'rounded-md border px-3 py-2 text-sm hover:bg-elevated disabled:opacity-40 transition'
const ROLE_LABELS = { owner: '所有者', admin: '管理员', member: '成员', viewer: '只读成员' }
const STATUS_LABELS: Record<string, string> = {
  queued: '排队中', running: '运行中', cancelling: '取消中', succeeded: '完成',
  failed: '失败', cancelled: '已取消', interrupted: '已中断', filled: '已成交', expired: '已过期',
  sending: '投递中', sent: '已投递', dead: '投递失败', submitted: '已下单', skipped: '已跳过',
}
const ERRORS: Record<string, string> = {
  invalid_credentials: '邮箱或密码不正确，或登录已过期',
  login_rate_limited: '登录失败次数过多，请稍后重试',
  permission_denied: '当前角色没有操作权限',
  tenant_access_denied: '你已不属于该工作空间',
  tenant_unavailable: '工作空间已停用或正在迁移',
  wrong_cell: '工作空间地址已变化，请刷新工作空间目录',
  routing_epoch_stale: '工作空间路由已更新，请刷新工作空间目录',
  strategy_version_stale: '策略已被其他成员修改，请刷新后再编辑',
  tenant_job_limit: '工作空间已有运行任务，请等待完成',
  cell_capacity_exceeded: '当前工作空间所在 Cell 的实时容量已满，请减少规则或联系管理员调整分组',
  strategy_requires_watchlist: '请先添加自选证券作为本次策略的股票池',
  market_unavailable: '共享行情暂不可用，请稍后重试',
  quote_not_available: '当前没有可用行情',
  quote_not_tradable: '行情过期、非交易时段或交易信息不完整，暂不能下单',
  paper_insufficient_cash: '可用资金不足，待成交买单已占用资金',
  paper_t1_or_insufficient_position: '可卖数量不足；当天买入需下一交易日卖出',
  paper_lot_size: '下单数量须为 100 股的整数倍',
  paper_account_frozen: '模拟账户已冻结',
  paper_day_expired: '订单已跨过交易日，未成交部分已到期',
  job_authorization_changed: '提交者权限或 Token 已变化，请重新提交任务',
  strategy_limit: '工作空间最多保存 100 个策略',
  monitor_rule_limit: '工作空间最多保存 100 个监控规则',
  idempotency_conflict: '请求编号已用于其他操作，请重新提交',
  strategy_has_jobs: '该策略已有运行记录，暂不能删除',
  email_delivery_not_configured: '管理员尚未配置邮件服务',
  webhook_origin_not_allowed: '该通知地址未获管理员允许',
  notification_admin_required: '仅管理员可配置外发通知',
  notification_connection_failed: '通知连接失败，系统将自动重试',
  notification_http_failed: '通知接收端返回错误',
  notification_smtp_failed: '邮件服务器返回错误',
  notification_version_stale: '通知配置已更新，请刷新后重试',
  automation_version_stale: '自动交易规则已更新，请刷新后重试',
  account_has_automation: '该模拟账户已有自动交易规则',
  automation_changed: '自动交易规则已暂停或修改',
  automation_already_held: '已有该证券持仓或待成交买单',
  automation_position_limit: '已达到最大持仓数量',
  automation_daily_limit: '已达到每日下单上限',
  automation_quantity_unavailable: '预算或可卖持仓不足一手',
  automation_signal_expired: '选股信号已过期',
  automation_budget_exceeded: '成交价格超过每单预算，订单已到期',
}

function errorText(error: unknown) {
  if (error instanceof Error) return ERRORS[error.message] || error.message
  return '操作失败，请重试'
}
function ErrorNotice({ error }: { error: unknown }) {
  return error ? <p role="alert" className="my-3 text-sm text-danger">{errorText(error)}</p> : null
}
function Empty({ children }: { children: ReactNode }) {
  return <p className="py-10 text-center text-sm text-muted">{children}</p>
}
function symbolsFrom(value: string) {
  return value.split(/[\s,，]+/).map(v => v.trim().toUpperCase()).filter(Boolean)
}
function cnDate() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
}

export function CellApp() {
  const [session, setSession] = useState(() => sessionStorage.getItem(SESSION_KEY) || '')
  function signedIn(token: string) {
    sessionStorage.setItem(SESSION_KEY, token)
    setSession(token)
  }
  function signedOut() {
    sessionStorage.removeItem(SESSION_KEY)
    setSession('')
  }
  return session
    ? <Session key={session} token={session} signedOut={signedOut} />
    : <Login signedIn={signedIn} />
}

function Login({ signedIn }: { signedIn: (token: string) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<unknown>(null)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    setError(null)
    try { signedIn((await loginCell(email, password)).token) }
    catch (err) { setError(err) }
    finally { setPending(false) }
  }
  return <main className="min-h-full grid place-items-center bg-base p-6">
    <div className="w-full max-w-sm">
      <div className="mb-9 flex items-center gap-3"><Logo size={32} /><span className="text-xl font-semibold">TSP</span></div>
      <h1 className="text-xl font-semibold">登录工作空间</h1>
      <p className="mt-2 text-sm text-muted">使用你的邮箱账户进入团队工作空间。</p>
      <form onSubmit={submit} className="mt-7 flex flex-col gap-4">
        <label className="flex flex-col gap-2 text-sm">邮箱<input className={inputStyle} type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} /></label>
        <label className="flex flex-col gap-2 text-sm">密码<input className={inputStyle} type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={e => setPassword(e.target.value)} /></label>
        <ErrorNotice error={error} />
        <button className={buttonStyle} disabled={pending}>{pending ? '登录中…' : '登录'}</button>
      </form>
    </div>
  </main>
}

function Session({ token, signedOut }: { token: string; signedOut: () => void }) {
  const [client] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  }))
  useEffect(() => () => { void client.cancelQueries(); client.clear() }, [client])
  return <QueryClientProvider client={client}><WorkspaceShell token={token} signedOut={signedOut} /></QueryClientProvider>
}

function WorkspaceShell({ token, signedOut }: { token: string; signedOut: () => void }) {
  const directory = useQuery({ queryKey: QK.cellDirectory, queryFn: ({ signal }) => cellSession(token).directory(signal), refetchInterval: 30000 })
  const [selected, setSelected] = useState('')
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'))
  const workspace = directory.data?.tenants.find(t => t.id === selected) || directory.data?.tenants[0]
  useEffect(() => {
    if (directory.error instanceof ApiError && directory.error.status === 401) signedOut()
  }, [directory.error, signedOut])
  async function logout() {
    try { await cellSession(token).logout() } finally { signedOut() }
  }
  return <div className="min-h-full bg-base">
    <header className="border-b bg-surface px-4 sm:px-6 py-4 flex flex-wrap items-center gap-4">
      <div className="flex items-center gap-2 mr-2"><Logo size={24} /><span className="font-semibold">TSP</span></div>
      <label className="flex items-center gap-2 text-sm flex-1 min-w-48">
        <span className="text-muted whitespace-nowrap">工作空间</span>
        <select aria-label="切换工作空间" className={`${inputStyle} max-w-xs w-full`} value={workspace?.id || ''} onChange={e => setSelected(e.target.value)}>
          {(directory.data?.tenants || []).map(t => <option key={t.id} value={t.id}>{t.name}{t.status !== 'active' ? '（不可用）' : ''}</option>)}
        </select>
      </label>
      {workspace ? <span className="text-xs text-muted">{ROLE_LABELS[workspace.role]}</span> : null}
      <button type="button" className={secondaryStyle} onClick={() => void directory.refetch()}>刷新目录</button>
      <button type="button" aria-label="切换主题" className={secondaryStyle} onClick={() => {
        document.documentElement.classList.toggle('dark', !dark); setDark(!dark)
      }}>{dark ? <Sun size={16} /> : <Moon size={16} />}</button>
      <button type="button" className={`${secondaryStyle} flex items-center gap-2`} onClick={() => void logout()}><LogOut size={14} />退出</button>
    </header>
    <div className="mx-auto max-w-6xl p-4 sm:p-6">
      <ErrorNotice error={directory.error} />
      {directory.isPending ? <Empty>加载工作空间…</Empty> : !workspace ? <Empty>你尚未加入工作空间，请联系管理员。</Empty>
        : workspace.status !== 'active' ? <Empty>该工作空间当前不可用，请切换工作空间。</Empty>
          : <WorkspaceSurface key={`${workspace.id}:${workspace.routing_epoch}:${workspace.policy_version}:${workspace.api_url}:${workspace.role}`} workspace={workspace} token={token} signedOut={signedOut} />}
    </div>
  </div>
}

type SurfaceProps = { workspace: Workspace; token: string; signedOut: () => void }
function WorkspaceSurface({ workspace, token, signedOut }: SurfaceProps) {
  const activation = useRef(0)
  const [lifetime] = useState(() => new AbortController())
  const [cache] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false }, mutations: { retry: false } },
  }))
  const api = useMemo(() => workspaceClient(workspace, token, lifetime.signal), [workspace, token, lifetime])
  useEffect(() => {
    const active = activation
    const generation = ++active.current
    return () => {
      queueMicrotask(() => {
        if (active.current !== generation) return
        // StrictMode replays setup immediately; defer irreversible abort until real unmount.
        lifetime.abort()
        void cache.cancelQueries()
        cache.clear()
      })
    }
  }, [cache, lifetime])
  return <QueryClientProvider client={cache}>
    <WorkspaceTabs workspace={workspace} api={api} signedOut={signedOut} />
  </QueryClientProvider>
}

type PanelProps = { workspace: Workspace; api: WorkspaceClient; canWrite: boolean }
function WorkspaceTabs({ workspace, api }: { workspace: Workspace; api: WorkspaceClient; signedOut: () => void }) {
  const [tab, setTab] = useState('watchlist')
  const tabs = [{ id: 'watchlist', label: '自选', icon: Star }, { id: 'strategies', label: '策略', icon: BookOpen },
    { id: 'alerts', label: '告警', icon: Bell }, { id: 'paper', label: '模拟盘', icon: Wallet }]
  const props = { workspace, api, canWrite: workspace.role !== 'viewer' }
  return <>
    <nav aria-label="工作空间导航" className="flex gap-1 overflow-x-auto border-b mb-6">
      {tabs.map(({ id, label, icon: Icon }) => <button type="button" key={id} aria-current={tab === id ? 'page' : undefined}
        className={`px-4 py-3 text-sm flex items-center gap-2 border-b-2 transition whitespace-nowrap ${tab === id ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-foreground'}`}
        onClick={() => setTab(id)}><Icon size={16} />{label}</button>)}
    </nav>
    {!props.canWrite ? <p className="mb-4 text-sm text-muted">当前为只读权限，可查看工作空间数据。</p> : null}
    {tab === 'watchlist' ? <WatchlistPanel {...props} /> : tab === 'strategies' ? <StrategyPanel {...props} />
      : tab === 'alerts' ? <AlertPanel {...props} /> : <PaperPanel {...props} />}
  </>
}

function useRefresh(workspace: Workspace) {
  const cache = useQueryClient()
  return () => cache.invalidateQueries({ queryKey: ['cell', workspace.id, workspace.routing_epoch] })
}

function WatchlistPanel({ workspace, api, canWrite }: PanelProps) {
  const [symbols, setSymbols] = useState('')
  const refresh = useRefresh(workspace)
  const list = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'watchlist'), queryFn: ({ signal }) => api.watchlist(signal) })
  const add = useMutation({ mutationFn: () => api.addWatchlist(symbolsFrom(symbols)), onSuccess: () => { setSymbols(''); void refresh() } })
  const remove = useMutation({ mutationFn: (symbol: string) => api.removeWatchlist(symbol), onSuccess: refresh })
  return <section>
    <h1 className="text-xl font-semibold">自选证券</h1>
    <p className="mt-1 text-sm text-muted">策略运行使用当前工作空间的自选作为股票池。</p>
    {canWrite ? <form onSubmit={e => { e.preventDefault(); add.mutate() }} className="my-5 flex flex-wrap gap-2">
      <input aria-label="证券代码" className={`${inputStyle} flex-1 min-w-56`} placeholder="000001.SZ, 600000.SH" required value={symbols} onChange={e => setSymbols(e.target.value)} />
      <button className={buttonStyle} disabled={add.isPending}>添加自选</button>
    </form> : null}
    <ErrorNotice error={list.error || add.error || remove.error} />
    {list.isPending ? <Empty>加载自选…</Empty> : list.data?.symbols.length ? <div className="overflow-x-auto mt-5">
      <table className="w-full text-sm text-left"><thead className="text-muted border-b"><tr><th className="py-3">证券</th><th>备注</th><th>分组</th><th /></tr></thead>
        <tbody>{list.data.symbols.map(row => <tr key={row.symbol} className="border-b"><td className="py-4 font-mono">{row.symbol}</td><td>{row.note || '—'}</td>
          <td>{row.group_ids?.map(id => list.data.groups.find(g => g.id === id)?.name).join('、') || '未分组'}</td>
          <td className="text-right">{canWrite ? <button className={secondaryStyle} disabled={remove.isPending} onClick={() => remove.mutate(row.symbol)}>移除</button> : null}</td></tr>)}</tbody>
      </table>
    </div> : <Empty>暂无自选证券</Empty>}
  </section>
}

function StrategyPanel({ workspace, api, canWrite }: PanelProps) {
  const refresh = useRefresh(workspace)
  const [editing, setEditing] = useState<CellStrategy | null>(null)
  const [name, setName] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [config, setConfig] = useState<Record<string, unknown>>({})
  const [asOf, setAsOf] = useState(cnDate)
  const templates = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'templates'), queryFn: ({ signal }) => api.templates(signal) })
  const strategies = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'strategies'), queryFn: ({ signal }) => api.strategies(signal) })
  const jobs = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'jobs'), queryFn: ({ signal }) => api.jobs(signal), refetchInterval: 3000 })
  const chosen = templateId || templates.data?.templates[0]?.id || ''
  const save = useMutation({
    mutationFn: () => api.saveStrategy({ name, template_id: chosen, overrides: config, version: editing?.version }, editing?.id),
    onSuccess: () => { setEditing(null); setName(''); setConfig({}); void refresh() },
  })
  const run = useMutation({ mutationFn: (id: string) => api.runStrategy(id, asOf, crypto.randomUUID()), onSuccess: refresh })
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelJob(id), onSuccess: refresh })
  const template = templates.data?.templates.find(t => t.id === chosen)
  const params = (config.params || {}) as Record<string, unknown>
  function parameter(id: string, value: unknown) {
    setConfig(current => ({ ...current, params: { ...(current.params as Record<string, unknown> || {}), [id]: value } }))
  }
  return <section>
    <h1 className="text-xl font-semibold">策略</h1><p className="mt-1 text-sm text-muted">选择平台策略并保存工作空间参数，运行结果保留配置版本。</p>
    <ErrorNotice error={templates.error || strategies.error || jobs.error || save.error || run.error || cancel.error} />
    {canWrite ? <form className="my-6 grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); save.mutate() }}>
      <label className="flex flex-col gap-2 text-sm">策略名称<input className={inputStyle} required maxLength={80} value={name} onChange={e => setName(e.target.value)} /></label>
      <label className="flex flex-col gap-2 text-sm">平台策略<select className={inputStyle} value={chosen} onChange={e => { setTemplateId(e.target.value); setConfig({}) }}>
        {templates.data?.templates.map(t => <option key={t.id} value={t.id}>{t.definition.name}</option>)}
      </select></label>
      <p className="sm:col-span-2 text-xs text-muted">{template?.definition.description}</p>
      <label className="sm:col-span-2 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={(config.basic_filter as Record<string, unknown> | undefined)?.enabled !== false}
          onChange={e => setConfig(current => ({ ...current, basic_filter: { ...(current.basic_filter as Record<string, unknown> || {}), enabled: e.target.checked } }))} />
        启用基础过滤</label>
      {template?.definition.params.map(p => <label key={p.id} className={`text-sm ${p.type === 'bool' ? 'flex items-center gap-2' : 'flex flex-col gap-2'}`}>
        {p.type === 'bool' ? <input type="checkbox" checked={Boolean(params[p.id] ?? p.default)} onChange={e => parameter(p.id, e.target.checked)} /> : null}
        {p.label || p.id}
        {p.type === 'select' ? <select className={inputStyle} value={String(params[p.id] ?? p.default)} onChange={e => parameter(p.id, e.target.value)}>
          {p.options?.map(option => <option key={option}>{option}</option>)}</select>
          : p.type !== 'bool' ? <input className={inputStyle} type={['float', 'int'].includes(p.type) ? 'number' : 'text'}
            min={p.min} max={p.max} step={p.type === 'int' ? '1' : 'any'} required
            value={String(params[p.id] ?? p.default ?? '')} onChange={e => parameter(p.id, ['float', 'int'].includes(p.type) ? Number(e.target.value) : e.target.value)} /> : null}
      </label>)}
      <div className="sm:col-span-2 flex gap-2"><button className={buttonStyle} disabled={save.isPending || !chosen}>{editing ? '保存修改' : '创建策略'}</button>
        {editing ? <button type="button" className={secondaryStyle} onClick={() => { setEditing(null); setName(''); setConfig({}) }}>取消编辑</button> : null}</div>
    </form> : null}
    <div className="flex items-center justify-between gap-3 border-b pb-3 mt-5"><h2 className="font-medium">工作空间策略</h2>
      <label className="text-sm flex gap-2 items-center">数据日期<input aria-label="策略数据日期" type="date" className={inputStyle} value={asOf} max={cnDate()} onChange={e => setAsOf(e.target.value)} /></label></div>
    {strategies.isPending ? <Empty>加载策略…</Empty> : strategies.data?.strategies.length ? strategies.data.strategies.map(s =>
      <div key={s.id} className="py-4 border-b flex flex-wrap items-center gap-3"><div className="flex-1"><p className="font-medium">{s.name}</p><p className="text-xs text-muted mt-1">版本 {s.version}</p></div>
        {canWrite ? <><button className={secondaryStyle} onClick={() => { setEditing(s); setName(s.name); setTemplateId(s.template_id); setConfig(s.overrides) }}>编辑</button>
          <button className={buttonStyle} disabled={run.isPending || !asOf} onClick={() => run.mutate(s.id)}>运行</button></> : null}</div>
    ) : <Empty>暂无工作空间策略</Empty>}
    <h2 className="font-medium mt-8 border-b pb-3">运行记录</h2>
    {jobs.data?.jobs.length ? jobs.data.jobs.map(job => <div key={job.id} className="py-4 border-b">
      <div className="flex items-center gap-3"><span className="flex-1 text-sm">{strategies.data?.strategies.find(s => s.id === job.strategy_id)?.name || job.strategy_id}</span>
        <span className={`text-xs ${job.status === 'failed' ? 'text-danger' : 'text-muted'}`}>{STATUS_LABELS[job.status] || job.status}</span>
        {canWrite && ['queued', 'running'].includes(job.status) ? <button className={secondaryStyle} disabled={cancel.isPending} onClick={() => cancel.mutate(job.id)}>取消任务</button> : null}</div>
      {job.error ? <p className="text-xs text-danger mt-2">{errorText(new Error(job.error))}</p> : null}
      {job.result ? <div className="mt-3 text-sm"><p className="text-muted mb-2">命中 {job.result.total} 只</p>
        <div className="flex flex-wrap gap-x-5 gap-y-2">{job.result.rows.map(row => <span key={row.symbol}>{row.name || row.symbol}{row.score != null ? ` · ${row.score.toFixed(1)}` : ''}</span>)}</div></div> : null}
    </div>) : <Empty>尚无运行记录</Empty>}
  </section>
}

function AlertPanel({ workspace, api, canWrite }: PanelProps) {
  const refresh = useRefresh(workspace)
  const [name, setName] = useState('')
  const [symbols, setSymbols] = useState('')
  const [field, setField] = useState<'price' | 'change_pct'>('price')
  const [op, setOp] = useState<CellRule['definition']['op']>('>=')
  const [value, setValue] = useState('10')
  const rules = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'rules'), queryFn: ({ signal }) => api.rules(signal) })
  const inbox = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'alerts'), queryFn: ({ signal }) => api.alerts(signal), refetchInterval: 5000 })
  const save = useMutation({
    mutationFn: () => api.saveRule({ name, symbols: symbolsFrom(symbols), field, op, value: Number(value),
      enabled: true, cooldown_seconds: 300, severity: 'info' }),
    onSuccess: () => { setName(''); setSymbols(''); void refresh() },
  })
  const toggle = useMutation({ mutationFn: (rule: CellRule) => api.saveRule({ ...rule.definition, id: rule.id, enabled: !rule.enabled }), onSuccess: refresh })
  const remove = useMutation({ mutationFn: (id: string) => api.deleteRule(id), onSuccess: refresh })
  const read = useMutation({ mutationFn: (id: string) => api.readAlert(id), onSuccess: refresh })
  return <section>
    <h1 className="text-xl font-semibold">告警</h1><p className="mt-1 text-sm text-muted">规则命中后进入工作空间收件箱，同一证券默认冷却 5 分钟。</p>
    <ErrorNotice error={rules.error || inbox.error || save.error || toggle.error || remove.error || read.error} />
    {canWrite ? <form className="my-6 grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); save.mutate() }}>
      <input aria-label="规则名称" className={inputStyle} placeholder="规则名称" required value={name} onChange={e => setName(e.target.value)} />
      <input aria-label="监控证券" className={inputStyle} placeholder="证券代码，如 000001.SZ" required value={symbols} onChange={e => setSymbols(e.target.value)} />
      <select aria-label="监控字段" className={inputStyle} value={field} onChange={e => setField(e.target.value as typeof field)}><option value="price">价格（元）</option><option value="change_pct">涨跌幅（小数，0.05 为 5%）</option></select>
      <div className="flex gap-2"><select aria-label="比较方式" className={inputStyle} value={op} onChange={e => setOp(e.target.value as typeof op)}>{['>=', '>', '<=', '<'].map(v => <option key={v}>{v}</option>)}</select>
        <input aria-label="告警阈值" type="number" step="any" className={`${inputStyle} flex-1`} required value={value} onChange={e => setValue(e.target.value)} /></div>
      <button className={`${buttonStyle} justify-self-start`} disabled={save.isPending}>创建规则</button>
    </form> : null}
    <h2 className="font-medium border-b pb-3">监控规则</h2>
    {rules.data?.rules.length ? rules.data.rules.map(rule => <div key={rule.id} className="py-4 border-b flex flex-wrap items-center gap-3">
      <div className="flex-1 min-w-40"><p className="text-sm">{rule.definition.name}</p><p className="text-xs text-muted mt-1">{rule.definition.symbols.join('、')} · {rule.definition.field === 'price' ? '价格' : '涨跌幅'} {rule.definition.op} {rule.definition.value}</p></div>
      <span className="text-xs text-muted">{rule.enabled ? '已启用' : '已暂停'}</span>
      {canWrite ? <><button className={secondaryStyle} disabled={toggle.isPending} onClick={() => toggle.mutate(rule)}>{rule.enabled ? '暂停' : '启用'}</button>
        <button className={secondaryStyle} disabled={remove.isPending} onClick={() => remove.mutate(rule.id)}>删除</button></> : null}
    </div>) : <Empty>{rules.isPending ? '加载规则…' : '暂无监控规则'}</Empty>}
    <h2 className="font-medium mt-8 border-b pb-3">收件箱</h2>
    {inbox.data?.alerts.length ? inbox.data.alerts.map(alert => <div key={alert.id} className="py-4 border-b flex gap-4 items-start">
      <div className="flex-1"><p className={`text-sm ${alert.read_at ? 'text-muted' : ''}`}>{alert.event.message}</p>
        <p className="mt-1 text-xs text-muted">{new Date(alert.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</p></div>
      {canWrite && !alert.read_at ? <button className={secondaryStyle} disabled={read.isPending} onClick={() => read.mutate(alert.id)}>标记已读</button> : null}
    </div>) : <Empty>{inbox.isPending ? '加载收件箱…' : '暂无告警记录'}</Empty>}
    <NotificationPanel workspace={workspace} api={api} canWrite={canWrite} />
  </section>
}

function PaperPanel({ workspace, api, canWrite }: PanelProps) {
  const refresh = useRefresh(workspace)
  const [selected, setSelected] = useState('')
  const [name, setName] = useState('')
  const [cash, setCash] = useState('100000')
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('100')
  const accounts = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'accounts'), queryFn: ({ signal }) => api.accounts(signal) })
  const account = accounts.data?.accounts.find(a => a.id === selected) || accounts.data?.accounts[0]
  const overview = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'paper', account?.id),
    queryFn: ({ signal }) => api.overview(account!.id, signal), enabled: !!account, refetchInterval: 5000 })
  const create = useMutation({ mutationFn: () => api.createAccount(name, Number(cash)), onSuccess: () => { setName(''); void refresh() } })
  const order = useMutation({ mutationFn: () => api.order(account!.id, {
    symbol: symbol.trim().toUpperCase(), side, qty: Number(qty), idempotency_key: crypto.randomUUID(),
  }), onSuccess: refresh })
  const freeze = useMutation({ mutationFn: () => api.freeze(account!.id, account!.status === 'active'), onSuccess: refresh })
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelOrder(account!.id, id), onSuccess: refresh })
  return <section>
    <h1 className="text-xl font-semibold">模拟盘</h1><p className="mt-1 text-sm text-muted">虚拟资金，按下单后的新行情撮合，股票遵守 T+1、费用与涨跌停约束。</p>
    <ErrorNotice error={accounts.error || overview.error || create.error || order.error || freeze.error || cancel.error} />
    {canWrite ? <form className="my-5 flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); create.mutate() }}>
      <input aria-label="模拟账户名称" required className={`${inputStyle} flex-1 min-w-40`} placeholder="模拟账户名称" value={name} onChange={e => setName(e.target.value)} />
      <label className="text-sm flex items-center gap-2">本金<input aria-label="初始本金" type="number" min="1" step="0.01" required className={`${inputStyle} w-40`} value={cash} onChange={e => setCash(e.target.value)} /></label>
      <button className={buttonStyle} disabled={create.isPending}>创建账户</button>
    </form> : null}
    {account ? <>
      <div className="flex flex-wrap gap-3 items-center border-b py-4">
        <select aria-label="切换模拟账户" className={`${inputStyle} flex-1 max-w-xs`} value={account.id} onChange={e => setSelected(e.target.value)}>{accounts.data?.accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select>
        <span className="text-sm">现金 <span className="font-mono">{Number(overview.data?.account.cash ?? account.cash).toFixed(2)}</span></span>
        <span className="text-xs text-muted">{account.status === 'active' ? '正常' : '已冻结'}</span>
        {canWrite ? <button className={secondaryStyle} disabled={freeze.isPending} onClick={() => freeze.mutate()}>{account.status === 'active' ? '冻结账户' : '解冻账户'}</button> : null}
      </div>
      {canWrite ? <form className="flex flex-wrap gap-2 py-5" onSubmit={e => { e.preventDefault(); order.mutate() }}>
        <input aria-label="下单证券" required className={`${inputStyle} flex-1 min-w-40`} placeholder="证券代码，如 000001.SZ" value={symbol} onChange={e => setSymbol(e.target.value)} />
        <select aria-label="买卖方向" className={inputStyle} value={side} onChange={e => setSide(e.target.value as typeof side)}><option value="buy">买入</option><option value="sell">卖出</option></select>
        <input aria-label="下单数量" type="number" min="100" step="100" required className={`${inputStyle} w-28`} value={qty} onChange={e => setQty(e.target.value)} />
        <button className={buttonStyle} disabled={order.isPending || account.status !== 'active'}>{order.isPending ? '提交中…' : '提交订单'}</button>
      </form> : null}
      <h2 className="font-medium mt-5 border-b pb-3">持仓</h2>
      {overview.data?.holdings.length ? <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-muted border-b"><tr><th className="py-3">证券</th><th>持仓</th><th>可卖</th><th>成本</th></tr></thead>
        <tbody>{overview.data.holdings.map(pos => <tr key={pos.symbol} className="border-b"><td className="py-3 font-mono">{pos.symbol}</td><td>{pos.qty}</td><td>{pos.available_qty}</td><td>{Number(pos.avg_cost).toFixed(4)}</td></tr>)}</tbody></table></div>
        : <Empty>暂无持仓</Empty>}
      <h2 className="font-medium mt-7 border-b pb-3">订单</h2>
      {overview.data?.orders.length ? overview.data.orders.map(o => <div key={o.id} className="py-3 border-b flex flex-wrap gap-3 items-center text-sm">
        <span className="font-mono">{o.symbol}</span><span>{o.side === 'buy' ? '买入' : '卖出'} {o.qty}股</span><span className="text-muted flex-1">{STATUS_LABELS[o.status] || o.status}</span>
        {o.reason ? <span className="text-xs text-danger">{errorText(new Error(o.reason))}</span> : null}
        {canWrite && o.status === 'queued' ? <button className={secondaryStyle} disabled={cancel.isPending} onClick={() => cancel.mutate(o.id)}>撤单</button> : null}
      </div>) : <Empty>暂无订单</Empty>}
      <h2 className="font-medium mt-7 border-b pb-3">成交台账</h2>
      {overview.data?.fills.length ? overview.data.fills.map(fill => <div key={fill.id} className="py-3 border-b text-sm flex flex-wrap gap-4">
        <span className="font-mono">{fill.symbol}</span><span>{fill.side === 'buy' ? '买入' : '卖出'} {fill.qty}股</span>
        <span>价格 {Number(fill.price).toFixed(4)}</span><span className="text-muted">费用 {Number(fill.fee).toFixed(2)}</span>
      </div>) : <Empty>暂无成交</Empty>}
      <AutomationPanel key={account.id} workspace={workspace} api={api} canWrite={canWrite} accountId={account.id} />
    </> : <Empty>{accounts.isPending ? '加载模拟账户…' : '暂无模拟账户'}</Empty>}
  </section>
}

function NotificationPanel({ workspace, api }: PanelProps) {
  const refresh = useRefresh(workspace)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'email' | 'webhook'>('email')
  const [destination, setDestination] = useState('')
  const admin = ['owner', 'admin'].includes(workspace.role)
  const channels = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'channels'), queryFn: ({ signal }) => api.channels(signal) })
  const deliveries = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'deliveries'), queryFn: ({ signal }) => api.deliveries(signal), refetchInterval: 5000 })
  const save = useMutation({ mutationFn: () => api.saveChannel({ name, kind, destination, enabled: true }),
    onSuccess: () => { setName(''); setDestination(''); void refresh() } })
  const toggle = useMutation({ mutationFn: (channel: NonNullable<typeof channels.data>['channels'][number]) =>
    api.saveChannel({ ...channel, enabled: !channel.enabled }), onSuccess: refresh })
  const retry = useMutation({ mutationFn: (id: string) => api.retryDelivery(id), onSuccess: refresh })
  return <div className="mt-8">
    <h2 className="font-medium border-b pb-3">外发通知</h2>
    <p className="mt-3 text-sm text-muted">启用后将新提醒发送到指定邮箱或通知地址；失败自动重试。</p>
    <ErrorNotice error={channels.error || deliveries.error || save.error || toggle.error || retry.error} />
    {admin ? <form className="my-4 flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); save.mutate() }}>
      <input aria-label="通知渠道名称" className={`${inputStyle} flex-1 min-w-40`} required placeholder="渠道名称" value={name} onChange={e => setName(e.target.value)} />
      <select aria-label="通知类型" className={inputStyle} value={kind} onChange={e => { setKind(e.target.value as typeof kind); setDestination('') }}>
        <option value="email">邮件</option><option value="webhook">Webhook</option></select>
      <input aria-label="通知收件地址" type={kind === 'email' ? 'email' : 'url'} className={`${inputStyle} flex-1 min-w-48`} required
        placeholder={kind === 'email' ? '收件邮箱' : '管理员允许的 HTTPS 地址'} value={destination} onChange={e => setDestination(e.target.value)} />
      <button className={buttonStyle} disabled={save.isPending}>启用通知</button>
    </form> : null}
    {channels.data?.channels.map(channel => <div key={channel.id} className="py-3 border-b flex flex-wrap items-center gap-3 text-sm">
      <span className="font-medium">{channel.name}</span><span className="flex-1 min-w-0 break-all text-muted">{channel.destination}</span>
      <span>{channel.enabled ? '已启用' : '已暂停'}</span>
      {admin ? <button className={secondaryStyle} disabled={toggle.isPending} onClick={() => toggle.mutate(channel)}>{channel.enabled ? '暂停' : '启用'}</button> : null}
    </div>)}
    <h3 className="mt-5 text-sm font-medium">投递记录</h3>
    {deliveries.data?.deliveries.length ? deliveries.data.deliveries.map(delivery => <div key={delivery.id} className="py-3 border-b flex flex-wrap gap-3 items-center text-sm">
      <span className="flex-1">{channels.data?.channels.find(c => c.id === delivery.channel_id)?.name || '通知'}</span>
      <span className="text-muted">{STATUS_LABELS[delivery.status] || delivery.status} · {delivery.attempts} 次</span>
      {delivery.error ? <span className="text-xs text-danger">{errorText(new Error(delivery.error))}</span> : null}
      {admin && delivery.status === 'dead' ? <button className={secondaryStyle} disabled={retry.isPending} onClick={() => retry.mutate(delivery.id)}>重试投递</button> : null}
    </div>) : <Empty>{deliveries.isPending ? '加载投递记录…' : '暂无外发记录'}</Empty>}
  </div>
}

function AutomationPanel({ workspace, api, canWrite, accountId }: PanelProps & { accountId: string }) {
  const refresh = useRefresh(workspace)
  const [editing, setEditing] = useState(false)
  const [strategyId, setStrategyId] = useState('')
  const [runTime, setRunTime] = useState('09:35')
  const [budget, setBudget] = useState('10000')
  const [maxPositions, setMaxPositions] = useState('5')
  const [stopLoss, setStopLoss] = useState('5')
  const [takeProfit, setTakeProfit] = useState('10')
  const [dailyLimit, setDailyLimit] = useState('10')
  const strategies = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'strategies'), queryFn: ({ signal }) => api.strategies(signal) })
  const automations = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'automations'), queryFn: ({ signal }) => api.automations(signal), refetchInterval: 5000 })
  const intents = useQuery({ queryKey: QK.cell(workspace.id, workspace.routing_epoch, 'intents'), queryFn: ({ signal }) => api.intents(signal), refetchInterval: 5000 })
  const current = automations.data?.automations.find(a => a.account_id === accountId)
  const save = useMutation({ mutationFn: () => api.saveAutomation({
    ...(current ? { id: current.id, version: current.version } : {}),
    name: '策略自动交易', strategy_id: strategyId || strategies.data?.strategies[0]?.id || '', account_id: accountId,
    enabled: true, run_time: runTime, budget_per_order: Number(budget), max_positions: Number(maxPositions),
    stop_loss_pct: Number(stopLoss) / 100, take_profit_pct: Number(takeProfit) / 100, max_daily_orders: Number(dailyLimit),
  }), onSuccess: () => { setEditing(false); void refresh() } })
  const toggle = useMutation({ mutationFn: () => api.saveAutomation({ ...current!, run_time: current!.run_time.slice(0, 5), enabled: !current!.enabled }), onSuccess: refresh })
  return <div className="mt-8">
    <h2 className="font-medium border-b pb-3">策略自动交易</h2>
    <p className="mt-3 text-sm text-muted">交易时段每天选股一次，使用上一日数据；按后续行情下单，并监控止盈止损。</p>
    <ErrorNotice error={strategies.error || automations.error || intents.error || save.error || toggle.error} />
    {current && !editing ? <div className="py-4 flex flex-wrap items-center gap-3 text-sm">
      <div className="flex-1"><p>{strategies.data?.strategies.find(s => s.id === current.strategy_id)?.name || current.name} · {current.enabled ? '运行中' : '已暂停'}</p>
        <p className="text-muted text-xs mt-2">{current.run_time.slice(0, 5)} 选股 · 每单 {Number(current.budget_per_order).toFixed(0)} 元 · 最多 {current.max_positions} 只 · 每日 {current.max_daily_orders} 单</p>
        <p className="text-muted text-xs mt-1">止损 {(Number(current.stop_loss_pct) * 100).toFixed(1)}% · 止盈 {(Number(current.take_profit_pct) * 100).toFixed(1)}% · 最近运行 {current.last_run_date || '尚未运行'}</p>
        {current.error ? <p className="text-xs text-danger mt-2">{errorText(new Error(current.error))}</p> : null}</div>
      {canWrite ? <><button className={secondaryStyle} disabled={toggle.isPending} onClick={() => toggle.mutate()}>{current.enabled ? '暂停自动交易' : '恢复自动交易'}</button>
        <button className={secondaryStyle} onClick={() => {
          setStrategyId(current.strategy_id); setRunTime(current.run_time.slice(0, 5)); setBudget(String(current.budget_per_order))
          setMaxPositions(String(current.max_positions)); setStopLoss(String(Number(current.stop_loss_pct) * 100))
          setTakeProfit(String(Number(current.take_profit_pct) * 100)); setDailyLimit(String(current.max_daily_orders)); setEditing(true)
        }}>修改规则</button></> : null}
    </div> : canWrite ? <form className="my-5 grid sm:grid-cols-2 gap-3" onSubmit={e => { e.preventDefault(); save.mutate() }}>
      <label className="text-sm flex flex-col gap-2">选股策略<select aria-label="自动交易策略" className={inputStyle} value={strategyId || strategies.data?.strategies[0]?.id || ''} onChange={e => setStrategyId(e.target.value)}>
        {strategies.data?.strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      <label className="text-sm flex flex-col gap-2">选股时间（北京时间）<input type="time" className={inputStyle} min="09:30" max="14:59" required value={runTime} onChange={e => setRunTime(e.target.value)} /></label>
      <label className="text-sm flex flex-col gap-2">每单预算（元）<input className={inputStyle} type="number" min="1" max="1000000000" required value={budget} onChange={e => setBudget(e.target.value)} /></label>
      <label className="text-sm flex flex-col gap-2">最大持仓证券数<input className={inputStyle} type="number" min="1" max="20" required value={maxPositions} onChange={e => setMaxPositions(e.target.value)} /></label>
      <label className="text-sm flex flex-col gap-2">止损（%）<input className={inputStyle} type="number" min="0.1" max="99" step="0.1" required value={stopLoss} onChange={e => setStopLoss(e.target.value)} /></label>
      <label className="text-sm flex flex-col gap-2">止盈（%）<input className={inputStyle} type="number" min="0.1" max="999" step="0.1" required value={takeProfit} onChange={e => setTakeProfit(e.target.value)} /></label>
      <label className="text-sm flex flex-col gap-2">每日下单上限<input className={inputStyle} type="number" min="1" max="100" required value={dailyLimit} onChange={e => setDailyLimit(e.target.value)} /></label>
      <div className="self-end flex gap-2"><button className={buttonStyle} disabled={save.isPending || !strategies.data?.strategies.length}>{current ? '保存并启用' : '启用模拟自动交易'}</button>
        {editing ? <button type="button" className={secondaryStyle} onClick={() => setEditing(false)}>取消修改</button> : null}</div>
    </form> : null}
    {current ? <><h3 className="text-sm font-medium mt-4">交易信号</h3>
      {intents.data?.intents.filter(i => i.automation_id === current.id).map(intent => <div key={intent.id} className="py-3 border-b flex flex-wrap gap-3 text-sm">
        <span className="font-mono">{intent.symbol}</span><span>{intent.side === 'buy' ? '买入' : '卖出'}</span>
        <span className="text-muted">{STATUS_LABELS[intent.status] || intent.status}</span>
        {intent.reason ? <span className="text-xs text-muted">{errorText(new Error(intent.reason))}</span> : null}
      </div>)}</> : null}
  </div>
}
