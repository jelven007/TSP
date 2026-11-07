import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ALL_BOARDS } from './BasicFilterEditor'

export interface StockScope {
  boards?: string[]
  include_custom?: boolean
  watchlist_group_ids?: string[]
  exclude_st?: boolean
  exclude_suspended?: boolean
}

export function StockScopeEditor({ value, onChange, disabled = false }: {
  value: StockScope
  onChange: (next: StockScope) => void
  disabled?: boolean
}) {
  const groups = useQuery({
    queryKey: QK.watchlistGroups, queryFn: api.watchlistGroups,
    enabled: !!value.include_custom,
  })
  return <fieldset disabled={disabled} className="space-y-3">
    <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-secondary">
      {ALL_BOARDS.map(board => <label key={board} className="flex items-center gap-2">
        <input type="checkbox" aria-label={board} checked={(value.boards ?? []).includes(board)}
          onChange={e => onChange({ ...value, boards: e.target.checked
            ? [...(value.boards ?? []), board] : (value.boards ?? []).filter(b => b !== board) })} />{board}
      </label>)}
      <label className="flex items-center gap-2">
        <input type="checkbox" aria-label="自定义" checked={!!value.include_custom}
          onChange={e => onChange({ ...value, include_custom: e.target.checked })} />自定义
      </label>
      <label className="flex items-center gap-2">
        <input type="checkbox" aria-label="剔除 *ST 和 ST" checked={!!value.exclude_st}
          onChange={e => onChange({ ...value, exclude_st: e.target.checked })} />剔除 *ST 和 ST
      </label>
      <label className="flex items-center gap-2">
        <input type="checkbox" aria-label="剔除当日停牌" checked={!!value.exclude_suspended}
          onChange={e => onChange({ ...value, exclude_suspended: e.target.checked })} />剔除当日停牌
      </label>
    </div>
    {value.include_custom && <div className="rounded-lg border border-border/40 bg-base/40 p-3 space-y-2">
      <p className="text-[11px] text-muted">选择自选分组（可多选）</p>
      {groups.isPending ? <p role="status" className="text-xs text-muted">加载自选分组中…</p>
        : groups.isError ? <p role="alert" className="text-xs text-danger">自选分组加载失败 <button onClick={() => groups.refetch()} className="underline">重试</button></p>
        : !groups.data?.groups.length ? <p className="text-xs text-muted">暂无分组，请先在「自选」中新建。</p>
        : <div className="flex flex-wrap gap-x-5 gap-y-2">
          {groups.data.groups.map(group => <label key={group.id} className="flex items-center gap-2 text-xs text-secondary">
            <input type="checkbox" aria-label={group.name} checked={(value.watchlist_group_ids ?? []).includes(group.id)}
              onChange={e => onChange({ ...value, watchlist_group_ids: e.target.checked
                ? [...(value.watchlist_group_ids ?? []), group.id] : (value.watchlist_group_ids ?? []).filter(id => id !== group.id) })} />
            {group.name}
          </label>)}
        </div>}
    </div>}
    <p className="text-[11px] text-muted">所选板块与自选分组取并集；当日股票池为满足股票范围和状态条件的所有股票，流通市值及其他数值条件仅用于策略选股。停牌按当日状态判断，状态未知不视为停牌。</p>
  </fieldset>
}
